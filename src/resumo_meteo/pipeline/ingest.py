"""Orquestracao dos downloads 00Z e registro dos artefatos normalizados."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..core.geometry_loader import load_geometry_catalog
from ..core.observed_aggregation import observed_basin_aggregates_match_contract
from ..core.summary_scope import WEATHER_SUMMARY_BASIN_IDS
from ..ingestion.deterministic import (
    build_model_processed_data,
    ingest_model_precipitation,
    persist_model_precipitation,
)
from ..ingestion.ensemble import (
    build_weather_summary_ensemble_metadata,
    ingest_all_member_precipitation,
    persist_ensemble_ingestion,
)
from ..ingestion.imerg import (
    build_imerg_processed_data,
    ingest_imerg_daily_snapshot,
    ingest_imerg_early_snapshot,
    persist_imerg_snapshot,
)
from ..ingestion.synoptic import (
    build_weather_summary_synoptic_metadata,
    ingest_synoptic_diagnostics,
    persist_synoptic_ingestion,
)
from ..sources.catalog import MODEL_SPECS, WEATHER_SUMMARY_HORIZON_HOURS
from ..sources.nasa import (
    GPM_IMERG_DAILY_PRODUCT_SLUG,
    GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
)
from ..storage.models import ACTIVE_SOURCE_RUN_STATUSES, SourceRun
from ..storage.repository import WeatherStorageRepository, source_run_idempotency_key

logger = logging.getLogger(__name__)
_FAMILIES = {"gfs": "ncep", "gefs": "ncep", "ec": "ecmwf", "ecens": "ecmwf"}
_ACTIVE_SOURCE_LEASE = timedelta(hours=4)
_IMERG_HISTORY_MAX_FAILURES = 2


@dataclass(frozen=True, slots=True)
class SourceIngestionOutcome:
    source_id: str
    reference_date: str
    status: str
    source_run_id: str | None
    reused: bool = False
    error_code: str = ""
    error_message: str = ""
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DailyIngestionReport:
    issue_date: str
    cycle: str
    started_at_utc: str
    finished_at_utc: str
    outcomes: tuple[SourceIngestionOutcome, ...]

    @property
    def complete(self) -> bool:
        return all(
            item.status in {"processed", "reused"}
            or (
                item.source_id == "gpm_imerg"
                and item.status == "partial"
                and item.issues == ("calendar_day_in_progress",)
            )
            for item in self.outcomes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "complete": self.complete,
            "counts": {
                status: sum(item.status == status for item in self.outcomes)
                for status in sorted({item.status for item in self.outcomes})
            },
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _run_datetime(reference_date: date) -> datetime:
    return datetime.combine(reference_date, time.min, tzinfo=UTC)


def _issue_codes(values: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value.get("code") or value.get("message") or "input_quality_issue")
            for value in values
        )
    )


def _is_reusable(
    row: SourceRun,
    *,
    source_id: str,
    artifact_root: Path | None = None,
) -> bool:
    if row.status not in {"processed", "partial"}:
        return False
    metadata = row.normalized_metadata if isinstance(row.normalized_metadata, dict) else {}
    geometry_version = load_geometry_catalog().version
    if source_id in {"gefs", "ecens"}:
        contract = metadata.get("weather_summary_ensemble")
        expected_members = 31 if source_id == "gefs" else 50
        return (
            isinstance(contract, dict)
            and bool(contract.get("json_path"))
            and row.max_lead_h == WEATHER_SUMMARY_HORIZON_HOURS
            and row.member_count == expected_members
            and contract.get("geometry_version") == geometry_version
            and tuple(contract.get("requested_basin_ids") or ()) == WEATHER_SUMMARY_BASIN_IDS
        )
    if source_id == "gpm_imerg":
        aggregates = metadata.get("weather_summary_observed_basins")
        return (
            row.status == "processed"
            and observed_basin_aggregates_match_contract(
                aggregates,
                geometry_version=geometry_version,
                basin_ids=WEATHER_SUMMARY_BASIN_IDS,
            )
        )
    precipitation_quality = metadata.get("quality")
    synoptic = metadata.get("weather_summary_synoptic")
    required_lead = 240 if source_id == "ec" else WEATHER_SUMMARY_HORIZON_HOURS
    grid_artifacts = tuple(
        item
        for item in row.artifacts
        if item.status == "available"
        and item.kind in {"grid_forecast", "normalized_grid"}
    )
    if artifact_root is not None and not any(
        (artifact_root / item.storage_path).is_file() for item in grid_artifacts
    ):
        return False
    return (
        isinstance(precipitation_quality, dict)
        and precipitation_quality.get("status") == "complete"
        and isinstance(synoptic, dict)
        and synoptic.get("status") == "complete"
        and row.max_lead_h == required_lead
        and bool(grid_artifacts)
    )


def _existing_run(
    repository: WeatherStorageRepository,
    *,
    reference_date: date,
    source_id: str,
    source_slug: str,
    product_slug: str,
    cycle: str | None,
) -> SourceRun | None:
    return next(
        (
            row
            for row in repository.source_runs_for_date(reference_date, cycle=None)
            if row.source_id == source_id
            and row.source_slug == source_slug
            and row.product_slug == product_slug
            and row.cycle == cycle
            and _is_reusable(
                row,
                source_id=source_id,
                artifact_root=repository.artifacts.root,
            )
        ),
        None,
    )


def _begin_source_run(
    repository: WeatherStorageRepository,
    *,
    source_id: str,
    source_slug: str,
    product_slug: str,
    reference_date: date,
    data_kind: str,
    family: str | None,
    cycle: str | None,
    force: bool,
) -> tuple[SourceRun, bool]:
    candidates = tuple(
        row
        for row in repository.source_runs_for_date(reference_date, cycle=None)
        if row.source_id == source_id
        and row.source_slug == source_slug
        and row.product_slug == product_slug
        and row.cycle == cycle
    )
    active = next((row for row in candidates if row.status in ACTIVE_SOURCE_RUN_STATUSES), None)
    if active is not None:
        if not force and active.updated_at > _utc_now() - _ACTIVE_SOURCE_LEASE:
            return active, True
        # A crashed worker must not block this source forever. The transition
        # releases the partial unique active identity before the retry insert.
        try:
            repository.transition_source_run(
                active.id,
                "failed",
                status_reason=(
                    "force_replaced_active_ingestion"
                    if force
                    else "stale_ingestion_lease_recovered"
                ),
                error_code=("force_replaced_active_ingestion" if force else "stale_ingestion_lease"),
                error_message=(
                    "Ingestão ativa substituída por uma execução operacional forçada."
                    if force
                    else "Ingestão ativa sem atualização por mais de quatro horas."
                ),
            )
        except Exception:
            logger.info("A lease de %s mudou enquanto era recuperada", source_id)
            current = repository.get_source_run(active.id)
            if current is not None and not force and _is_reusable(
                current,
                source_id=source_id,
                artifact_root=repository.artifacts.root,
            ):
                return current, True
            if current is not None and current.status in ACTIVE_SOURCE_RUN_STATUSES:
                return current, True
    if not force:
        existing = _existing_run(
            repository,
            reference_date=reference_date,
            source_id=source_id,
            source_slug=source_slug,
            product_slug=product_slug,
            cycle=cycle,
        )
        if existing is not None:
            return existing, True
    discriminator = f"attempt-{uuid4().hex}"
    key = source_run_idempotency_key(
        source_id=source_id,
        product_slug=product_slug,
        reference_date=reference_date,
        cycle=cycle,
        discriminator=discriminator,
    )
    row, created = repository.register_source_run(
        source_id=source_id,
        source_slug=source_slug,
        product_slug=product_slug,
        data_kind=data_kind,
        reference_date=reference_date,
        idempotency_key=key,
        family=family,
        cycle=cycle,
        run_datetime_utc=_run_datetime(reference_date) if cycle else None,
        status="pending",
    )
    if not created:
        return row, True
    row = repository.transition_source_run(
        row.id,
        "downloading",
        status_reason="download_started",
        increment_attempt=True,
    )
    return row, False


def _reuse_outcome(
    row: SourceRun, *, source_id: str, reference_date: date
) -> SourceIngestionOutcome:
    reusable = _is_reusable(row, source_id=source_id)
    return SourceIngestionOutcome(
        source_id,
        reference_date.isoformat(),
        "reused" if reusable else "in_progress",
        row.id,
        True,
        issues=() if reusable else ("concurrent_ingestion_in_progress",),
    )


def _media_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".csv.gz") or path.endswith(".gz"):
        return "application/gzip"
    return "application/octet-stream"


def _record_entries(
    repository: WeatherStorageRepository,
    *,
    source_run_id: str,
    entries: Iterable[Mapping[str, object]],
    default_kind: str,
    transient: bool,
    delete_after: datetime | None = None,
) -> None:
    for entry in entries:
        path = str(entry.get("path") or "")
        if not path:
            continue
        details = {
            str(key): value
            for key, value in entry.items()
            if key not in {"path", "sha256", "size_bytes", "kind"}
        }
        metadata = entry.get("metadata")
        if isinstance(metadata, Mapping):
            details.update({str(key): value for key, value in metadata.items()})
        forecast_hour = details.get("end_lead_hour")
        repository.record_artifact(
            source_run_id=source_run_id,
            storage_path=path,
            kind=str(entry.get("kind") or default_kind),
            retention_class="transient" if transient else "normalized",
            delete_after=delete_after if transient else None,
            sha256=str(entry.get("sha256") or ""),
            size_bytes=int(entry["size_bytes"]) if entry.get("size_bytes") is not None else None,
            media_type=_media_type(path),
            forecast_hour=int(forecast_hour) if forecast_hour is not None else None,
            metadata=details,
        )


def _record_single_file(
    repository: WeatherStorageRepository,
    *,
    source_run_id: str,
    relative_path: str,
    kind: str,
) -> None:
    path = repository.resolve_media_file(relative_path)
    import hashlib  # local to avoid hashing utilities depending on the ingestion layer.

    with path.open("rb") as file_obj:
        digest = hashlib.file_digest(file_obj, "sha256").hexdigest()
    repository.record_artifact(
        source_run_id=source_run_id,
        storage_path=relative_path,
        kind=kind,
        retention_class="normalized",
        sha256=digest,
        size_bytes=path.stat().st_size,
        media_type=_media_type(relative_path),
    )


def _failed(
    repository: WeatherStorageRepository,
    *,
    row: SourceRun,
    source_id: str,
    reference_date: date,
    exc: Exception,
    orphan_directories: Iterable[str] = (),
) -> SourceIngestionOutcome:
    code = str(getattr(exc, "code", "ingestion_failed"))
    message = f"{type(exc).__name__}: {exc}"
    for directory in dict.fromkeys(str(value) for value in orphan_directories if value):
        try:
            repository.artifacts.remove_tree(directory, missing_ok=True)
        except (OSError, ValueError) as cleanup_exc:
            logger.error(
                "Falha removendo diretório órfão %s da fonte %s: %s",
                directory,
                source_id,
                cleanup_exc,
            )
    try:
        discarded = repository.discard_source_artifacts(row.id)
        for item in discarded:
            if item.error:
                logger.error(
                    "Falha descartando artefato %s da fonte %s: %s",
                    item.storage_path,
                    source_id,
                    item.error,
                )
    except Exception:
        logger.exception("Falha descartando artefatos da fonte %s", source_id)
    try:
        repository.transition_source_run(
            row.id,
            "failed",
            status_reason="ingestion_failed",
            error_code=code,
            error_message=message,
        )
    except Exception:
        logger.exception("Falha registrando erro da fonte %s", source_id)
    return SourceIngestionOutcome(
        source_id=source_id,
        reference_date=reference_date.isoformat(),
        status="failed",
        source_run_id=row.id,
        error_code=code,
        error_message=message,
    )


def ingest_deterministic_model(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    model: str,
    reference_date: date,
    force: bool = False,
) -> SourceIngestionOutcome:
    if model not in {"gfs", "ec"}:
        raise ValueError("Modelo determinístico deve ser gfs ou ec")
    spec = MODEL_SPECS[model]
    row, reused = _begin_source_run(
        repository,
        source_id=model,
        source_slug=spec.source,
        product_slug=spec.product_slug,
        reference_date=reference_date,
        data_kind="forecast",
        family=_FAMILIES[model],
        cycle="00",
        force=force,
    )
    if reused:
        return _reuse_outcome(row, source_id=model, reference_date=reference_date)
    persisted_directories: list[str] = []
    try:
        # Precipitation and the objective synoptic fields use independent
        # byte-range requests. Running both streams together shortens the
        # critical path without changing either meteorological input.
        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix=f"resumo-{model}"
        ) as deterministic_executor:
            synoptic_future = deterministic_executor.submit(
                ingest_synoptic_diagnostics,
                reference_date=reference_date,
                model=model,
                cycle="00",
                temporary_root=repository.artifacts.staging_root(),
            )
            precipitation = ingest_model_precipitation(
                model=model,
                reference_date=reference_date,
                cycle="00",
                horizon_hours=WEATHER_SUMMARY_HORIZON_HOURS,
                temporary_root=repository.artifacts.staging_root(),
                # The four source families already run concurrently.  Keep the
                # per-source pool deliberately small to avoid throttling NOAA.
                noaa_max_workers=min(max(settings.download_workers, 1), 3) if model == "gfs" else 1,
            )
            try:
                synoptic = synoptic_future.result()
                synoptic_error: Exception | None = None
            except Exception as exc:
                synoptic = None
                synoptic_error = exc
        persisted = persist_model_precipitation(
            precipitation,
            artifact_root=settings.artifact_root,
            run_id=row.id,
        )
        persisted_directories.append(persisted.storage_root)
        processed = build_model_processed_data(precipitation, persisted)
        expires = _utc_now() + timedelta(days=settings.source_retention_days)
        _record_entries(
            repository,
            source_run_id=row.id,
            entries=persisted.files,
            default_kind="grid_forecast",
            transient=True,
            delete_after=expires,
        )
        _record_single_file(
            repository,
            source_run_id=row.id,
            relative_path=persisted.manifest_path,
            kind="precipitation_manifest",
        )
        precipitation_issues = list(_issue_codes(precipitation.quality_issues))
        issues = list(precipitation_issues)
        synoptic_status = "complete"
        try:
            if synoptic_error is not None:
                raise synoptic_error
            if synoptic is None:
                raise RuntimeError("Diagnostico sinotico sem resultado")
            synoptic_artifacts = persist_synoptic_ingestion(
                synoptic,
                media_root=settings.artifact_root,
                run_id=row.id,
            )
            persisted_directories.append(synoptic_artifacts.directory)
            processed["weather_summary_synoptic"] = build_weather_summary_synoptic_metadata(
                synoptic, synoptic_artifacts
            )
            _record_entries(
                repository,
                source_run_id=row.id,
                entries=synoptic_artifacts.files,
                default_kind="synoptic_artifact",
                transient=False,
            )
            synoptic_status = synoptic.status
            issues.extend(_issue_codes(synoptic.quality_issues))
        except Exception as exc:
            synoptic_status = "failed"
            issues.append(f"synoptic_ingestion_failed:{type(exc).__name__}")
            processed["weather_summary_synoptic"] = {
                "schema_version": "weather-summary-synoptic.v3",
                "model": model,
                "status": "failed",
                "quality_issues": [
                    {"code": "synoptic_ingestion_failed", "error_type": type(exc).__name__}
                ],
            }
        # Missing optional synoptic fields (for example CAPE in EC open data)
        # remain audited but do not downgrade a run whose required fields and
        # precipitation contract are complete.
        final_status = (
            "processed"
            if synoptic_status == "complete" and not precipitation_issues
            else "partial"
        )
        repository.transition_source_run(
            row.id,
            "processing",
            status_reason="normalization_complete",
        )
        repository.transition_source_run(
            row.id,
            final_status,
            status_reason="complete" if final_status == "processed" else "quality_partial",
            metadata_updates=processed,
            quality_manifest={"status": final_status, "issues": list(dict.fromkeys(issues))},
            max_lead_h=precipitation.max_forecast_hour,
            processed_at=_utc_now(),
        )
        return SourceIngestionOutcome(
            model,
            reference_date.isoformat(),
            final_status,
            row.id,
            issues=tuple(dict.fromkeys(issues)),
        )
    except Exception as exc:
        return _failed(
            repository,
            row=row,
            source_id=model,
            reference_date=reference_date,
            exc=exc,
            orphan_directories=persisted_directories,
        )


def ingest_ensemble_model(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    model: str,
    reference_date: date,
    force: bool = False,
) -> SourceIngestionOutcome:
    if model not in {"gefs", "ecens"}:
        raise ValueError("Modelo ensemble deve ser gefs ou ecens")
    spec = MODEL_SPECS[model]
    row, reused = _begin_source_run(
        repository,
        source_id=model,
        source_slug=spec.source,
        product_slug=spec.product_slug,
        reference_date=reference_date,
        data_kind="forecast",
        family=_FAMILIES[model],
        cycle="00",
        force=force,
    )
    if reused:
        return _reuse_outcome(row, source_id=model, reference_date=reference_date)
    persisted_directories: list[str] = []
    try:
        result = ingest_all_member_precipitation(
            reference_date=reference_date,
            cycle="00",
            models=(model,),
            gefs_max_workers=min(max(settings.download_workers, 1), 4),
            ecens_max_workers=min(max(settings.download_workers, 1), 3),
            temporary_root=repository.artifacts.staging_root(),
        )
        persisted = persist_ensemble_ingestion(
            result,
            media_root=settings.artifact_root,
            run_id=row.id,
        )
        persisted_directories.append(persisted.directory)
        contract = build_weather_summary_ensemble_metadata(result, persisted, model=model)
        _record_entries(
            repository,
            source_run_id=row.id,
            entries=persisted.files,
            default_kind="ensemble_artifact",
            transient=False,
        )
        summary = result.models[model]
        issues = _issue_codes(result.quality_issues)
        final_status = "processed" if summary.status == "complete" and not issues else "partial"
        repository.transition_source_run(row.id, "processing", status_reason="normalization_complete")
        repository.transition_source_run(
            row.id,
            final_status,
            status_reason="complete" if final_status == "processed" else "quality_partial",
            metadata_updates={
                "reference_date": reference_date.isoformat(),
                "cycle": "00",
                "run_datetime_utc": _run_datetime(reference_date).isoformat().replace("+00:00", "Z"),
                "max_forecast_hour": WEATHER_SUMMARY_HORIZON_HOURS,
                "weather_summary_ensemble": contract,
            },
            quality_manifest={"status": final_status, "issues": list(issues)},
            max_lead_h=WEATHER_SUMMARY_HORIZON_HOURS,
            member_count=summary.available_member_count,
            processed_at=_utc_now(),
        )
        return SourceIngestionOutcome(
            model,
            reference_date.isoformat(),
            final_status,
            row.id,
            issues=issues,
        )
    except Exception as exc:
        return _failed(
            repository,
            row=row,
            source_id=model,
            reference_date=reference_date,
            exc=exc,
            orphan_directories=persisted_directories,
        )


def ingest_imerg_date(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    product_date: date,
    cutoff_utc: datetime | None,
    force: bool = False,
) -> SourceIngestionOutcome:
    is_completed_history_day = cutoff_utc is None
    product_slug = (
        GPM_IMERG_DAILY_PRODUCT_SLUG
        if is_completed_history_day
        else GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG
    )
    row, reused = _begin_source_run(
        repository,
        source_id="gpm_imerg",
        source_slug="nasa",
        product_slug=product_slug,
        reference_date=product_date,
        data_kind="observed",
        family=None,
        cycle=None,
        force=force,
    )
    if reused:
        return _reuse_outcome(row, source_id="gpm_imerg", reference_date=product_date)
    persisted_directories: list[str] = []
    try:
        if is_completed_history_day:
            snapshot = ingest_imerg_daily_snapshot(
                product_date=product_date,
                earthdata_token=settings.nasa_earthdata_token or "",
                temporary_root=repository.artifacts.staging_root(),
            )
        else:
            snapshot = ingest_imerg_early_snapshot(
                product_date=product_date,
                earthdata_token=settings.nasa_earthdata_token or "",
                cutoff_utc=cutoff_utc,
                max_workers=min(max(settings.download_workers, 1), 16),
                temporary_root=repository.artifacts.staging_root(),
            )
        persisted = persist_imerg_snapshot(
            snapshot,
            artifact_root=settings.artifact_root,
            run_id=row.id,
        )
        persisted_directories.append(persisted.storage_root)
        processed = build_imerg_processed_data(snapshot, persisted)
        expires = _utc_now() + timedelta(days=settings.source_retention_days)
        observed_entries = [
            {**dict(entry), "kind": "grid_observed"} for entry in persisted.files
        ]
        _record_entries(
            repository,
            source_run_id=row.id,
            entries=observed_entries,
            default_kind="grid_observed",
            transient=True,
            delete_after=expires,
        )
        _record_single_file(
            repository,
            source_run_id=row.id,
            relative_path=persisted.manifest_path,
            kind="imerg_manifest",
        )
        issues = () if snapshot.period_complete else ("calendar_day_in_progress",)
        final_status = "processed" if snapshot.period_complete else "partial"
        repository.transition_source_run(row.id, "processing", status_reason="normalization_complete")
        repository.transition_source_run(
            row.id,
            final_status,
            status_reason="complete" if snapshot.period_complete else "partial_day_snapshot",
            metadata_updates=processed,
            quality_manifest={"status": final_status, "issues": list(issues)},
            processed_at=_utc_now(),
            valid_start_utc=snapshot.valid_start_utc,
            valid_end_utc=snapshot.valid_end_utc,
            available_at=_utc_now(),
            temporal_resolution=snapshot.temporal_resolution,
            period_complete=snapshot.period_complete,
            coverage_pct=snapshot.coverage_pct,
        )
        return SourceIngestionOutcome(
            "gpm_imerg",
            product_date.isoformat(),
            final_status,
            row.id,
            issues=issues,
        )
    except Exception as exc:
        return _failed(
            repository,
            row=row,
            source_id="gpm_imerg",
            reference_date=product_date,
            exc=exc,
            orphan_directories=persisted_directories,
        )


def ingest_required_inputs(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    issue_date: date,
    cutoff_utc: datetime,
    force: bool = False,
    imerg_history_days: int = 30,
) -> DailyIngestionReport:
    """Ingest all authorized inputs; independent model jobs run concurrently."""

    if settings.cycle != "00":
        raise ValueError("Somente a rodada 00z é autorizada")
    if cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() is None:
        raise ValueError("cutoff_utc deve incluir timezone")
    if not 0 <= int(imerg_history_days) <= 30:
        raise ValueError("imerg_history_days deve estar entre 0 e 30")
    started = _utc_now()
    outcomes: list[SourceIngestionOutcome] = []
    model_functions = {
        "gfs": ingest_deterministic_model,
        "ec": ingest_deterministic_model,
        "gefs": ingest_ensemble_model,
        "ecens": ingest_ensemble_model,
    }
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="resumo-model") as executor:
        futures = {
            executor.submit(
                function,
                repository,
                settings,
                model=model,
                reference_date=issue_date,
                force=force,
            ): model
            for model, function in model_functions.items()
        }

        # A zero value is an explicit operational fallback: skip IMERG rather
        # than allowing a delayed NASA endpoint to block a forecast-only run.
        # The packet records the missing observation source and never invents
        # observed-rain diagnostics.
        if imerg_history_days:
            # Completed history uses one daily granule per date. A small dedicated
            # pool overlaps NASA network latency without competing excessively with
            # the four model-family downloads. Today's 30-minute snapshot runs only
            # after history to keep peak memory and bandwidth bounded.
            history_dates = tuple(
                issue_date - timedelta(days=offset)
                for offset in range(int(imerg_history_days), 0, -1)
            )
            backend = repository.engine.url.get_backend_name()
            # SQLite on a Docker Desktop bind mount is not safe with several
            # concurrent writers even with WAL. PostgreSQL keeps the faster pool;
            # SQLite serializes history persistence deterministically.
            imerg_workers = (
                1
                if backend == "sqlite"
                else min(max(settings.download_workers, 1), 4)
            )
            history_failures = 0
            next_history_index = 0
            with ThreadPoolExecutor(
                max_workers=imerg_workers, thread_name_prefix="resumo-imerg-history"
            ) as imerg_executor:
                pending: dict[object, date] = {}

                def submit_next() -> bool:
                    nonlocal next_history_index
                    if next_history_index >= len(history_dates):
                        return False
                    product_date = history_dates[next_history_index]
                    next_history_index += 1
                    pending[
                        imerg_executor.submit(
                            ingest_imerg_date,
                            repository,
                            settings,
                            product_date=product_date,
                            cutoff_utc=None,
                            force=force,
                        )
                    ] = product_date
                    return True

                while len(pending) < imerg_workers and submit_next():
                    pass
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        product_date = pending.pop(future)
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            outcome = SourceIngestionOutcome(
                                "gpm_imerg",
                                product_date.isoformat(),
                                "failed",
                                None,
                                error_code="unhandled_ingestion_error",
                                error_message=f"{type(exc).__name__}: {exc}",
                            )
                        outcomes.append(outcome)
                        if outcome.status == "failed":
                            history_failures += 1
                    if history_failures >= _IMERG_HISTORY_MAX_FAILURES:
                        # Do not enqueue the remaining month after NASA has
                        # demonstrated that it is unavailable. Running jobs are
                        # allowed to finish so they can clean their staging
                        # directories and close their database transactions.
                        logger.warning(
                            "Circuito IMERG aberto após %s falhas; histórico restante será omitido.",
                            history_failures,
                        )
                        continue
                    while len(pending) < imerg_workers and submit_next():
                        pass

            if history_failures >= _IMERG_HISTORY_MAX_FAILURES:
                for product_date in history_dates[next_history_index:]:
                    outcomes.append(
                        SourceIngestionOutcome(
                            "gpm_imerg",
                            product_date.isoformat(),
                            "skipped",
                            None,
                            error_code="imerg_history_circuit_open",
                            error_message=(
                                "Histórico IMERG omitido após falhas consecutivas da fonte."
                            ),
                        )
                    )
                outcomes.append(
                    SourceIngestionOutcome(
                        "gpm_imerg",
                        issue_date.isoformat(),
                        "skipped",
                        None,
                        error_code="imerg_history_circuit_open",
                        error_message=(
                            "Snapshot IMERG do dia omitido para não atrasar a previsão."
                        ),
                    )
                )
            else:
                outcomes.append(
                    ingest_imerg_date(
                        repository,
                        settings,
                        product_date=issue_date,
                        cutoff_utc=cutoff_utc,
                        force=force,
                    )
                )
        else:
            outcomes.append(
                SourceIngestionOutcome(
                    "gpm_imerg",
                    issue_date.isoformat(),
                    "skipped",
                    None,
                    error_code="imerg_disabled_explicitly",
                    error_message="IMERG omitido por imerg_history_days=0.",
                )
            )
        for future in as_completed(futures):
            model = futures[future]
            try:
                outcomes.append(future.result())
            except Exception as exc:  # individual functions normally convert errors.
                outcomes.append(
                    SourceIngestionOutcome(
                        model,
                        issue_date.isoformat(),
                        "failed",
                        None,
                        error_code="unhandled_ingestion_error",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )
    outcomes.sort(key=lambda item: (item.source_id, item.reference_date))
    return DailyIngestionReport(
        issue_date=issue_date.isoformat(),
        cycle="00",
        started_at_utc=started.isoformat().replace("+00:00", "Z"),
        finished_at_utc=_utc_now().isoformat().replace("+00:00", "Z"),
        outcomes=tuple(outcomes),
    )


__all__ = [
    "DailyIngestionReport",
    "SourceIngestionOutcome",
    "ingest_deterministic_model",
    "ingest_ensemble_model",
    "ingest_imerg_date",
    "ingest_required_inputs",
]
