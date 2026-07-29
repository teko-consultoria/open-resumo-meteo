"""Standalone repository for normalized inputs and daily summary audit data."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, and_, desc, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .artifacts import (
    ArtifactStore,
    TemporaryDirectoryCleanupResult,
    normalize_artifact_path,
    validate_sha256,
)
from .contracts import (
    ArtifactProduct,
    ModelInputRecord,
    ObservedIMERGInputRecord,
    SelectionSource,
)
from .database import create_database_engine, create_session_factory, run_migrations, session_scope
from .models import (
    ACTIVE_SOURCE_RUN_STATUSES,
    ARTIFACT_STATUSES,
    RETENTION_CLASSES,
    SOURCE_RUN_STATUSES,
    SUMMARY_RUN_STATUSES,
    Artifact,
    AuditEvent,
    SourceRun,
    SummaryRun,
    utc_now,
)

SUMMARY_CYCLE = "00"
MODEL_SOURCES = {
    "gfs": ("noaa", "ncep"),
    "gefs": ("noaa", "ncep"),
    "ec": ("ecmwf", "ecmwf"),
    "ecens": ("ecmwf", "ecmwf"),
}
EXPECTED_ENSEMBLE_MEMBERS = {"gefs": 31, "ecens": 50}
GRID_ARTIFACT_KINDS = {"grid_forecast", "grid_observed", "normalized_grid"}

SOURCE_TRANSITIONS = {
    "pending": {"downloading", "processing", "failed", "skipped"},
    "downloading": {"downloaded", "processing", "failed", "partial"},
    "downloaded": {"processing", "failed"},
    "processing": {"processed", "partial", "failed"},
    "partial": {"downloading", "processing", "processed", "failed"},
    "failed": {"downloading", "processing", "failed", "skipped"},
    "processed": {"processing", "processed"},
    "skipped": {"pending", "downloading", "skipped"},
}

SUMMARY_TRANSITIONS = {
    "pending": {"building_packet", "generating", "blocked", "failed"},
    "building_packet": {"generating", "blocked", "failed"},
    "generating": {"generating", "completed", "failed", "blocked"},
    "blocked": {"building_packet", "generating", "blocked"},
    "failed": {"building_packet", "generating", "failed"},
    "completed": {"completed", "generating"},
}


class IdempotencyConflict(ValueError):
    pass


class InvalidStatusTransition(ValueError):
    pass


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else ""


def source_run_idempotency_key(
    *,
    source_id: str,
    product_slug: str,
    reference_date: date,
    cycle: str | None,
    discriminator: str = "",
) -> str:
    raw = "|".join(
        (
            source_id.strip().lower(),
            product_slug.strip().lower(),
            reference_date.isoformat(),
            cycle or "",
            discriminator,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CleanupResult:
    artifact_id: str
    storage_path: str
    status: str
    bytes_reclaimed: int
    error: str = ""


class WeatherStorageRepository:
    """SQLAlchemy-backed metadata store and packet-builder input repository."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        engine: Engine | None = None,
        artifact_root: str | Path,
        max_source_age_hours: int = 42,
        bootstrap: bool = False,
    ):
        if engine is None:
            if database_url is None:
                raise ValueError("database_url or engine is required")
            engine = create_database_engine(database_url)
        self.engine = engine
        self.sessions: sessionmaker[Session] = create_session_factory(engine)
        self.artifacts = ArtifactStore(artifact_root)
        self.max_source_age_hours = max(int(max_source_age_hours), 1)
        if bootstrap:
            self.bootstrap()

    def bootstrap(self) -> tuple[str, ...]:
        self.artifacts.ensure_root()
        return run_migrations(self.engine)

    def get_source_run(self, source_run_id: str) -> SourceRun | None:
        with self.sessions() as session:
            return session.scalar(
                select(SourceRun)
                .options(selectinload(SourceRun.artifacts))
                .where(SourceRun.id == source_run_id)
            )

    def get_summary_run(self, summary_run_id: str) -> SummaryRun | None:
        with self.sessions() as session:
            return session.get(SummaryRun, summary_run_id)

    def summary_runs_for_date(self, issue_date: date) -> tuple[SummaryRun, ...]:
        """Return newest-first audit runs for one reader-facing issue date."""

        with self.sessions() as session:
            return tuple(
                session.scalars(
                    select(SummaryRun)
                    .where(SummaryRun.issue_date == issue_date)
                    .order_by(desc(SummaryRun.created_at), desc(SummaryRun.id))
                )
            )

    def source_runs_for_date(
        self, reference_date: date, *, cycle: str | None = SUMMARY_CYCLE
    ) -> tuple[SourceRun, ...]:
        """Return source runs for CLI status, including their artifact metadata."""

        with self.sessions() as session:
            query = (
                select(SourceRun)
                .options(selectinload(SourceRun.artifacts))
                .where(SourceRun.reference_date == reference_date)
                .order_by(
                    SourceRun.source_id, desc(SourceRun.processed_at), desc(SourceRun.created_at)
                )
            )
            if cycle is not None:
                # Observations have no model cycle and remain useful in the
                # same daily status response.
                query = query.where(or_(SourceRun.cycle == cycle, SourceRun.cycle.is_(None)))
            return tuple(session.scalars(query).unique())

    def source_artifacts(self, source_run_id: str) -> tuple[Artifact, ...]:
        with self.sessions() as session:
            return tuple(
                session.scalars(
                    select(Artifact)
                    .where(Artifact.source_run_id == source_run_id)
                    .order_by(Artifact.created_at, Artifact.storage_path)
                )
            )

    # -- source lifecycle -------------------------------------------------
    def register_source_run(
        self,
        *,
        source_id: str,
        source_slug: str,
        product_slug: str,
        data_kind: str,
        reference_date: date,
        idempotency_key: str | None = None,
        family: str | None = None,
        cycle: str | None = None,
        run_datetime_utc: datetime | None = None,
        valid_start_utc: datetime | None = None,
        valid_end_utc: datetime | None = None,
        available_at: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
        normalized_metadata: Mapping[str, Any] | None = None,
        quality_manifest: Mapping[str, Any] | None = None,
        status: str = "pending",
        max_lead_h: int | None = None,
        member_count: int | None = None,
        temporal_resolution: str | None = None,
        period_complete: bool | None = None,
        coverage_pct: float | None = None,
        raw_cleanup_after: datetime | None = None,
    ) -> tuple[SourceRun, bool]:
        key = idempotency_key or source_run_idempotency_key(
            source_id=source_id,
            product_slug=product_slug,
            reference_date=reference_date,
            cycle=cycle,
        )
        if not key.strip():
            raise ValueError("idempotency_key cannot be empty")
        if status not in SOURCE_RUN_STATUSES:
            raise ValueError(f"invalid source run status: {status}")
        timestamps = {
            "run_datetime_utc": run_datetime_utc,
            "valid_start_utc": valid_start_utc,
            "valid_end_utc": valid_end_utc,
            "available_at": available_at,
            "raw_cleanup_after": raw_cleanup_after,
        }
        timestamps = {name: as_utc(value) if value else None for name, value in timestamps.items()}
        logical_identity = and_(
            SourceRun.source_id == source_id,
            SourceRun.source_slug == source_slug,
            SourceRun.product_slug == product_slug,
            SourceRun.reference_date == reference_date,
            SourceRun.cycle.is_(None) if cycle is None else SourceRun.cycle == cycle,
        )
        try:
            with session_scope(self.sessions) as session:
                existing = session.scalar(
                    select(SourceRun).where(SourceRun.idempotency_key == key)
                )
                if existing is not None:
                    identity = (
                        existing.source_id,
                        existing.source_slug,
                        existing.product_slug,
                        existing.reference_date,
                        existing.cycle,
                    )
                    requested = (
                        source_id,
                        source_slug,
                        product_slug,
                        reference_date,
                        cycle,
                    )
                    if identity != requested:
                        raise IdempotencyConflict(
                            f"idempotency key already belongs to {identity}, not {requested}"
                        )
                    return existing, False
                run = SourceRun(
                    idempotency_key=key,
                    source_id=source_id,
                    source_slug=source_slug,
                    product_slug=product_slug,
                    data_kind=data_kind,
                    family=family,
                    cycle=cycle,
                    reference_date=reference_date,
                    status=status,
                    payload=dict(payload or {}),
                    normalized_metadata=dict(normalized_metadata or {}),
                    quality_manifest=dict(quality_manifest or {}),
                    max_lead_h=max_lead_h,
                    member_count=member_count,
                    temporal_resolution=temporal_resolution,
                    period_complete=period_complete,
                    coverage_pct=coverage_pct,
                    **timestamps,
                )
                if status in {
                    "downloading",
                    "downloaded",
                    "processing",
                    "processed",
                    "partial",
                    "failed",
                }:
                    run.started_at = utc_now()
                if status in {"processed", "partial"}:
                    run.processed_at = available_at or utc_now()
                session.add(run)
                session.flush()
                self._audit(
                    session,
                    entity_type="source_run",
                    entity_id=run.id,
                    event_type="registered",
                    to_status=status,
                    details={
                        "source_id": source_id,
                        "reference_date": reference_date.isoformat(),
                    },
                )
                return run, True
        except IntegrityError:
            # A concurrent worker may own the partial unique active identity.
            with self.sessions() as session:
                existing = session.scalar(
                    select(SourceRun)
                    .where(
                        logical_identity,
                        SourceRun.status.in_(ACTIVE_SOURCE_RUN_STATUSES),
                    )
                    .order_by(desc(SourceRun.created_at))
                    .limit(1)
                )
                if existing is None:
                    existing = session.scalar(
                        select(SourceRun).where(SourceRun.idempotency_key == key)
                    )
                if existing is None:
                    raise
                return existing, False

    def transition_source_run(
        self,
        source_run_id: str,
        status: str,
        *,
        status_reason: str = "",
        error_code: str = "",
        error_message: str = "",
        metadata_updates: Mapping[str, Any] | None = None,
        quality_manifest: Mapping[str, Any] | None = None,
        max_lead_h: int | None = None,
        member_count: int | None = None,
        processed_at: datetime | None = None,
        valid_start_utc: datetime | None = None,
        valid_end_utc: datetime | None = None,
        available_at: datetime | None = None,
        temporal_resolution: str | None = None,
        period_complete: bool | None = None,
        coverage_pct: float | None = None,
        increment_attempt: bool = False,
    ) -> SourceRun:
        if status not in SOURCE_RUN_STATUSES:
            raise ValueError(f"invalid source run status: {status}")
        with session_scope(self.sessions) as session:
            run = session.get(SourceRun, source_run_id)
            if run is None:
                raise KeyError(source_run_id)
            previous = run.status
            if status not in SOURCE_TRANSITIONS[previous]:
                raise InvalidStatusTransition(
                    f"source run cannot transition {previous} -> {status}"
                )
            run.status = status
            run.status_reason = status_reason
            run.error_code = error_code
            run.error_message = error_message
            if increment_attempt:
                run.attempt_count += 1
            if metadata_updates:
                run.normalized_metadata = {**run.normalized_metadata, **dict(metadata_updates)}
            if quality_manifest is not None:
                run.quality_manifest = dict(quality_manifest)
            if max_lead_h is not None:
                run.max_lead_h = max_lead_h
            if member_count is not None:
                run.member_count = member_count
            if valid_start_utc is not None:
                run.valid_start_utc = as_utc(valid_start_utc)
            if valid_end_utc is not None:
                run.valid_end_utc = as_utc(valid_end_utc)
            if available_at is not None:
                run.available_at = as_utc(available_at)
            if temporal_resolution is not None:
                run.temporal_resolution = str(temporal_resolution)
            if period_complete is not None:
                run.period_complete = bool(period_complete)
            if coverage_pct is not None:
                value = float(coverage_pct)
                if not 0 <= value <= 100:
                    raise ValueError("coverage_pct must be between 0 and 100")
                run.coverage_pct = value
            if run.started_at is None and status not in {"pending", "skipped"}:
                run.started_at = utc_now()
            if status in {"processed", "partial"}:
                run.processed_at = as_utc(processed_at) if processed_at else utc_now()
            self._audit(
                session,
                entity_type="source_run",
                entity_id=run.id,
                event_type="status_changed",
                from_status=previous,
                to_status=status,
                details={"reason": status_reason, "error_code": error_code},
            )
            session.flush()
            return run

    def record_artifact(
        self,
        *,
        storage_path: str,
        kind: str,
        source_run_id: str | None = None,
        retention_class: str = "normalized",
        sha256: str = "",
        size_bytes: int | None = None,
        media_type: str = "application/octet-stream",
        status: str = "available",
        valid_start_utc: datetime | None = None,
        valid_end_utc: datetime | None = None,
        forecast_hour: int | None = None,
        member_id: str | None = None,
        variable: str | None = None,
        level: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        delete_after: datetime | None = None,
    ) -> tuple[Artifact, bool]:
        path = normalize_artifact_path(storage_path)
        digest = validate_sha256(sha256)
        if retention_class not in RETENTION_CLASSES:
            raise ValueError(f"invalid retention class: {retention_class}")
        if status not in ARTIFACT_STATUSES:
            raise ValueError(f"invalid artifact status: {status}")
        if retention_class != "transient" and delete_after is not None:
            raise ValueError("delete_after is allowed only for transient artifacts")
        if retention_class == "transient" and delete_after is None:
            raise ValueError("transient artifacts require delete_after")
        with session_scope(self.sessions) as session:
            if source_run_id and session.get(SourceRun, source_run_id) is None:
                raise KeyError(source_run_id)
            existing = session.scalar(select(Artifact).where(Artifact.storage_path == path))
            if existing is not None:
                identity = (existing.source_run_id, existing.kind)
                requested = (source_run_id, kind)
                if identity != requested or (
                    digest and existing.sha256 and digest != existing.sha256
                ):
                    raise IdempotencyConflict(
                        f"artifact path already registered with different identity: {path}"
                    )
                return existing, False
            artifact = Artifact(
                source_run_id=source_run_id,
                kind=kind,
                storage_path=path,
                media_type=media_type,
                sha256=digest,
                size_bytes=size_bytes,
                status=status,
                retention_class=retention_class,
                valid_start_utc=as_utc(valid_start_utc) if valid_start_utc else None,
                valid_end_utc=as_utc(valid_end_utc) if valid_end_utc else None,
                forecast_hour=forecast_hour,
                member_id=member_id,
                variable=variable,
                level=level,
                details=dict(metadata or {}),
                delete_after=as_utc(delete_after) if delete_after else None,
            )
            session.add(artifact)
            session.flush()
            self._audit(
                session,
                entity_type="artifact",
                entity_id=artifact.id,
                event_type="registered",
                to_status=status,
                details={"path": path, "retention_class": retention_class},
            )
            return artifact, True

    # -- packet-builder read interface -----------------------------------
    def select_model_inputs(
        self, *, cutoff_utc: datetime, target_reference_date: date | None = None
    ) -> tuple[dict[str, ModelInputRecord], dict[str, Any]]:
        cutoff = as_utc(cutoff_utc)
        with self.sessions() as session:
            query = (
                select(SourceRun)
                .options(selectinload(SourceRun.artifacts))
                .where(
                    SourceRun.source_id.in_(MODEL_SOURCES),
                    SourceRun.data_kind == "forecast",
                    SourceRun.cycle == SUMMARY_CYCLE,
                    SourceRun.status.in_(("processed", "partial")),
                    SourceRun.processed_at.is_not(None),
                    SourceRun.processed_at <= cutoff,
                    or_(SourceRun.run_datetime_utc.is_(None), SourceRun.run_datetime_utc <= cutoff),
                )
                .order_by(desc(SourceRun.processed_at), desc(SourceRun.created_at))
            )
            if target_reference_date is not None:
                query = query.where(SourceRun.reference_date == target_reference_date)
            rows = list(session.scalars(query).unique())

            authorized = [
                row
                for row in rows
                if row.source_slug == MODEL_SOURCES[row.source_id][0]
                and (
                    target_reference_date is not None
                    or (
                        row.run_datetime_utc is not None
                        and row.run_datetime_utc
                        >= cutoff - timedelta(hours=self.max_source_age_hours)
                    )
                )
            ]
            reference = self._select_reference_date(authorized)
            selected_rows: dict[str, SourceRun] = {}
            for row in authorized:
                if row.reference_date != reference:
                    continue
                current = selected_rows.get(row.source_id)
                if current is None or self._model_candidate_preference(
                    row
                ) > self._model_candidate_preference(current):
                    selected_rows[row.source_id] = row

            records: dict[str, ModelInputRecord] = {}
            sources: list[SelectionSource] = []
            for model_key, (source_slug, family) in MODEL_SOURCES.items():
                row = selected_rows.get(model_key)
                if row is None:
                    sources.append(
                        SelectionSource(
                            source_id=model_key,
                            category="forecast_model",
                            status="missing",
                            required=False,
                            source_name=source_slug.upper(),
                            issues=("missing_00z_run",),
                        )
                    )
                    continue
                grid_files = self._grid_files(row)
                compact = self._has_compact_ensemble(row)
                issues = list(self._declared_issues(row))
                status = "partial" if row.status == "partial" else "complete"
                if not grid_files and not compact:
                    issues.append("missing_normalized_grid_files")
                    status = "partial"
                required_lead = 240 if model_key == "ec" else 336
                max_lead_h = int(row.max_lead_h or 0)
                if max_lead_h < required_lead:
                    issues.append(f"max_lead_below_{required_lead}h")
                    status = "partial"
                if model_key == "ec" and max_lead_h > 240:
                    issues.append("ec_lead_above_240h")
                    status = "partial"
                expected_members = EXPECTED_ENSEMBLE_MEMBERS.get(model_key)
                if expected_members is not None and row.member_count != expected_members:
                    issues.append(
                        f"ensemble_members_{row.member_count}_expected_{expected_members}"
                    )
                    status = "partial"
                if row.run_datetime_utc is None:
                    issues.append("missing_run_datetime")
                    status = "partial"
                    continue
                product = self._product_facade(row, grid_files)
                records[model_key] = ModelInputRecord(
                    model_key=model_key,
                    family=family,
                    product_data=product,
                    reference_date=row.reference_date,
                    run_datetime_utc=row.run_datetime_utc,
                    max_lead_h=max_lead_h,
                    member_count=row.member_count,
                    grid_files=grid_files,
                    status=status,
                    issues=tuple(dict.fromkeys(issues)),
                )
                sources.append(
                    SelectionSource(
                        source_id=model_key,
                        category="forecast_model",
                        status=status,
                        required=False,
                        source_name=source_slug.upper(),
                        init_time=_iso(row.run_datetime_utc),
                        collected_at=_iso(row.processed_at),
                        product_data_id=row.id,
                        details={
                            "cycle": SUMMARY_CYCLE,
                            "product_slug": row.product_slug,
                            "max_lead_h": max_lead_h,
                            "member_count": row.member_count,
                            "grid_file_count": len(grid_files),
                        },
                        issues=tuple(dict.fromkeys(issues)),
                    )
                )
        manifest = self._selection_manifest(sources, cutoff)
        manifest.update(
            {
                "selected_reference_date": reference.isoformat() if reference else "",
                "target_reference_date": target_reference_date.isoformat()
                if target_reference_date
                else "",
                "required_cycle": SUMMARY_CYCLE,
                "authorized_models": list(MODEL_SOURCES),
            }
        )
        return records, manifest

    def select_imerg_input(
        self, *, cutoff_utc: datetime
    ) -> tuple[ObservedIMERGInputRecord | None, SelectionSource]:
        cutoff = as_utc(cutoff_utc)
        rows = self._imerg_rows(cutoff)
        records = [
            record
            for row in rows
            if (record := self._imerg_record(row))
            and record.data_end_utc
            and record.data_end_utc <= cutoff
        ]
        if not records:
            return None, self._missing_imerg_source("missing_processed_imerg")
        record = max(
            records,
            key=lambda item: (
                item.data_end_utc or datetime.min.replace(tzinfo=UTC),
                item.temporal_resolution == "30_minutes",
                item.product_data.processed_at or datetime.min.replace(tzinfo=UTC),
            ),
        )
        latency_h = (
            max((cutoff - record.data_end_utc).total_seconds() / 3600, 0.0)
            if record.data_end_utc
            else None
        )
        issues = list(record.issues)
        status = record.status
        if latency_h is not None and latency_h > 12:
            status = "delayed"
            issues.append("observation_older_than_12h")
            record = self._replace_imerg(record, status=status, issues=tuple(dict.fromkeys(issues)))
        source = SelectionSource(
            source_id="gpm_imerg",
            category="observed_precipitation",
            status=status,
            required=False,
            source_name=(
                "NASA GPM IMERG Early 30 min"
                if record.temporal_resolution == "30_minutes"
                else "NASA GPM IMERG Late diario"
            ),
            collected_at=_iso(record.product_data.processed_at),
            valid_start=_iso(record.data_start_utc),
            valid_end=_iso(record.data_end_utc),
            latency_minutes=round(latency_h * 60) if latency_h is not None else None,
            product_data_id=record.product_data.id,
            details={
                "product_slug": record.product_data.product_slug,
                "grid_file_count": len(record.grid_files),
                "temporal_resolution": record.temporal_resolution,
                "period_complete": record.period_complete,
                "coverage_pct": record.coverage_pct,
            },
            issues=tuple(dict.fromkeys(issues)),
        )
        return record, source

    def select_imerg_history(
        self, *, cutoff_utc: datetime, days: int = 30, end_date: date | None = None
    ) -> tuple[tuple[ObservedIMERGInputRecord, ...], SelectionSource]:
        if days <= 0 or days > 30:
            raise ValueError("days must be between 1 and 30")
        cutoff = as_utc(cutoff_utc)
        rows = self._imerg_rows(cutoff, end_date=end_date)
        by_date: dict[date, ObservedIMERGInputRecord] = {}
        for row in rows:
            record = self._imerg_record(row)
            if (
                record is None
                or not record.period_complete
                or (end_date is not None and record.observation_date > end_date)
                or record.data_end_utc is None
                or record.data_end_utc > cutoff
            ):
                continue
            current = by_date.get(record.observation_date)
            if current is None or self._imerg_preference(record) > self._imerg_preference(current):
                by_date[record.observation_date] = record
        records = tuple(sorted(by_date.values(), key=lambda item: item.observation_date)[-days:])
        if not records:
            return (), self._missing_imerg_source("missing_processed_imerg_history")
        latest = records[-1]
        latest_end = latest.data_end_utc
        latency_h = max((cutoff - latest_end).total_seconds() / 3600, 0.0) if latest_end else 0.0
        issues: list[str] = []
        status = "complete"
        if latency_h > 30:
            status = "delayed"
            issues.append("observation_older_than_30h")
        if len(records) < days or any(record.status != "complete" for record in records):
            if status == "complete":
                status = "partial"
            issues.append(f"history_days_{len(records)}_expected_{days}")
        source = SelectionSource(
            source_id="gpm_imerg",
            category="observed_precipitation",
            status=status,
            required=False,
            source_name="NASA GPM IMERG",
            collected_at=_iso(latest.product_data.processed_at),
            valid_start=_iso(records[0].data_start_utc),
            valid_end=_iso(latest_end),
            latency_minutes=round(latency_h * 60),
            product_data_id=latest.product_data.id,
            details={
                "product_slugs": sorted({record.product_data.product_slug for record in records}),
                "history_day_count": len(records),
                "requested_end_date": end_date.isoformat() if end_date else "",
                "temporal_resolution": "calendar_day_aggregates",
                "preferred_product_slug": "gpm-imerg-early-half-hourly",
            },
            issues=tuple(issues),
        )
        return records, source

    def resolve_media_file(self, relative_path: str, *, expected_sha256: str = "") -> Path:
        return self.artifacts.verify(relative_path, expected_sha256=expected_sha256)

    resolve_artifact = resolve_media_file

    # -- summary lifecycle ------------------------------------------------
    def create_or_get_summary_run(
        self,
        *,
        issue_date: date,
        issue_time: datetime,
        cutoff_utc: datetime,
        input_fingerprint: str,
        schema_version: str,
        prompt_version: str,
        llm_provider: str,
        llm_model: str,
        status: str = "pending",
        analysis_packet: Mapping[str, Any] | None = None,
        source_manifest: Mapping[str, Any] | None = None,
        quality_manifest: Mapping[str, Any] | None = None,
    ) -> tuple[SummaryRun, bool]:
        if status not in SUMMARY_RUN_STATUSES:
            raise ValueError(f"invalid summary run status: {status}")
        identity = and_(
            SummaryRun.issue_date == issue_date,
            SummaryRun.input_fingerprint == input_fingerprint,
            SummaryRun.schema_version == schema_version,
            SummaryRun.prompt_version == prompt_version,
            SummaryRun.llm_provider == llm_provider,
            SummaryRun.llm_model == llm_model,
        )
        try:
            with session_scope(self.sessions) as session:
                existing = session.scalar(select(SummaryRun).where(identity).with_for_update())
                if existing is not None:
                    return existing, False
                run = SummaryRun(
                    issue_date=issue_date,
                    issue_time=as_utc(issue_time),
                    cutoff_utc=as_utc(cutoff_utc),
                    input_fingerprint=input_fingerprint,
                    schema_version=schema_version,
                    prompt_version=prompt_version,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                    status=status,
                    analysis_packet=dict(analysis_packet or {}),
                    source_manifest=dict(source_manifest or {}),
                    quality_manifest=dict(quality_manifest or {}),
                )
                session.add(run)
                session.flush()
                self._audit(
                    session,
                    entity_type="summary_run",
                    entity_id=run.id,
                    event_type="registered",
                    to_status=status,
                    details={"issue_date": issue_date.isoformat()},
                )
                return run, True
        except IntegrityError:
            # A concurrent worker may have inserted the same unique identity
            # between our SELECT and INSERT. Resolve the winner idempotently.
            with self.sessions() as session:
                existing = session.scalar(select(SummaryRun).where(identity))
                if existing is None:
                    raise
                return existing, False

    def claim_summary_run_for_generation(
        self,
        summary_run_id: str,
        *,
        analysis_packet: Mapping[str, Any],
        source_manifest: Mapping[str, Any],
        quality_manifest: Mapping[str, Any],
        force: bool = False,
        stale_after: timedelta = timedelta(minutes=30),
        now_utc: datetime | None = None,
    ) -> tuple[SummaryRun, bool]:
        """Atomically claim the sole right to call the editorial model.

        SQLite ignores ``SELECT FOR UPDATE``. The compare-and-set includes the
        observed status and update timestamp, so only one concurrent worker can
        move (or refresh a stale) run to ``generating``.
        """

        now = as_utc(now_utc or utc_now())
        with session_scope(self.sessions) as session:
            observed = session.get(SummaryRun, summary_run_id)
            if observed is None:
                raise KeyError(summary_run_id)
            previous = observed.status
            previous_updated_at = observed.updated_at
            stale = previous == "generating" and previous_updated_at <= now - stale_after
            claimable = previous in {"pending", "blocked", "failed"} or stale
            if not claimable:
                return observed, False

            statement = (
                update(SummaryRun)
                .where(
                    SummaryRun.id == summary_run_id,
                    SummaryRun.status == previous,
                    SummaryRun.updated_at == previous_updated_at,
                )
                .values(
                    status="generating",
                    error_code="",
                    error_message="",
                    finished_at=None,
                    analysis_packet=dict(analysis_packet),
                    source_manifest=dict(source_manifest),
                    quality_manifest=dict(quality_manifest),
                    updated_at=now,
                )
            )
            won = session.execute(statement).rowcount == 1
            session.expire_all()
            current = session.get(SummaryRun, summary_run_id)
            if current is None:
                raise KeyError(summary_run_id)
            if not won:
                return current, False
            self._audit(
                session,
                entity_type="summary_run",
                entity_id=current.id,
                event_type="generation_claimed",
                from_status=previous,
                to_status="generating",
                details={"force": force, "stale_recovered": stale},
            )
            session.flush()
            return current, True

    def completed_summary_for_date(self, issue_date: date) -> SummaryRun | None:
        with self.sessions() as session:
            return session.scalar(
                select(SummaryRun)
                .where(SummaryRun.issue_date == issue_date, SummaryRun.status == "completed")
                .order_by(desc(SummaryRun.published_at), desc(SummaryRun.created_at))
                .limit(1)
            )

    def transition_summary_run(
        self,
        summary_run_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
        analysis_packet: Mapping[str, Any] | None = None,
        source_manifest: Mapping[str, Any] | None = None,
        quality_manifest: Mapping[str, Any] | None = None,
        generation_metadata: Mapping[str, Any] | None = None,
        validation_errors: Iterable[Mapping[str, Any]] | None = None,
        output_text: str | None = None,
        output_path: str | None = None,
        audit_manifest_path: str | None = None,
        output_sha256: str | None = None,
        llm_response_id: str | None = None,
        increment_attempt: bool = False,
        published_at: datetime | None = None,
    ) -> SummaryRun:
        if status not in SUMMARY_RUN_STATUSES:
            raise ValueError(f"invalid summary run status: {status}")
        with session_scope(self.sessions) as session:
            run = session.get(SummaryRun, summary_run_id)
            if run is None:
                raise KeyError(summary_run_id)
            previous = run.status
            if status not in SUMMARY_TRANSITIONS[previous]:
                raise InvalidStatusTransition(
                    f"summary run cannot transition {previous} -> {status}"
                )
            run.status = status
            run.error_code = error_code
            run.error_message = error_message
            for attr, value in (
                ("analysis_packet", analysis_packet),
                ("source_manifest", source_manifest),
                ("quality_manifest", quality_manifest),
                ("generation_metadata", generation_metadata),
            ):
                if value is not None:
                    setattr(run, attr, dict(value))
            if validation_errors is not None:
                run.validation_errors = [dict(item) for item in validation_errors]
            if output_text is not None:
                run.output_text = output_text
            if output_path is not None:
                run.output_path = normalize_artifact_path(output_path) if output_path else ""
            if audit_manifest_path is not None:
                run.audit_manifest_path = (
                    normalize_artifact_path(audit_manifest_path) if audit_manifest_path else ""
                )
            if output_sha256 is not None:
                run.output_sha256 = validate_sha256(output_sha256)
            if llm_response_id is not None:
                run.llm_response_id = llm_response_id
            if increment_attempt:
                run.generation_attempts += 1
            if status in {"completed", "blocked", "failed"}:
                run.finished_at = utc_now()
            if published_at is not None:
                run.published_at = as_utc(published_at)
            self._audit(
                session,
                entity_type="summary_run",
                entity_id=run.id,
                event_type="status_changed",
                from_status=previous,
                to_status=status,
                details={"error_code": error_code},
            )
            session.flush()
            return run

    # -- retention and audit ---------------------------------------------
    def expire_transient_artifacts_through(
        self,
        reference_date: date,
        *,
        now_utc: datetime | None = None,
    ) -> int:
        """Make disposable source grids eligible for cleanup after publication.

        Compact ensemble/synoptic contracts, basin aggregates, manifests and
        every publication/audit artifact remain untouched.
        """

        now = as_utc(now_utc or utc_now())
        with session_scope(self.sessions) as session:
            artifacts = list(
                session.scalars(
                    select(Artifact)
                    .join(SourceRun, Artifact.source_run_id == SourceRun.id)
                    .where(
                        Artifact.retention_class == "transient",
                        Artifact.status == "available",
                        SourceRun.reference_date <= reference_date,
                        SourceRun.status.in_(("processed", "partial")),
                        SourceRun.processed_at.is_not(None),
                    )
                    .with_for_update()
                )
            )
            for artifact in artifacts:
                artifact.delete_after = now
            return len(artifacts)

    def cleanup_expired_artifacts(
        self, *, now_utc: datetime | None = None, dry_run: bool = False, limit: int = 1000
    ) -> tuple[CleanupResult, ...]:
        now = as_utc(now_utc or utc_now())
        results: list[CleanupResult] = []
        with session_scope(self.sessions) as session:
            artifacts = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.retention_class == "transient",
                        Artifact.status == "available",
                        Artifact.delete_after.is_not(None),
                        Artifact.delete_after <= now,
                    )
                    .order_by(Artifact.delete_after, Artifact.created_at)
                    .limit(max(int(limit), 1))
                    .with_for_update()
                )
            )
            touched_runs: set[str] = set()
            for artifact in artifacts:
                try:
                    removed = (
                        False
                        if dry_run
                        else self.artifacts.unlink(artifact.storage_path, missing_ok=True)
                    )
                except (OSError, ValueError) as exc:
                    results.append(
                        CleanupResult(
                            artifact_id=artifact.id,
                            storage_path=artifact.storage_path,
                            status="error",
                            bytes_reclaimed=0,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                reclaimed = int(artifact.size_bytes or 0) if removed or dry_run else 0
                results.append(
                    CleanupResult(
                        artifact_id=artifact.id,
                        storage_path=artifact.storage_path,
                        status="would_delete"
                        if dry_run
                        else ("deleted" if removed else "already_missing"),
                        bytes_reclaimed=reclaimed,
                    )
                )
                if dry_run:
                    continue
                artifact.status = "deleted"
                artifact.deleted_at = now
                if artifact.source_run_id:
                    touched_runs.add(artifact.source_run_id)
                self._audit(
                    session,
                    entity_type="artifact",
                    entity_id=artifact.id,
                    event_type="cleaned",
                    from_status="available",
                    to_status="deleted",
                    details={"path": artifact.storage_path, "bytes_reclaimed": reclaimed},
                )
            if not dry_run:
                session.flush()
                for source_run_id in touched_runs:
                    remaining = session.scalar(
                        select(Artifact.id)
                        .where(
                            Artifact.source_run_id == source_run_id,
                            Artifact.retention_class == "transient",
                            Artifact.status == "available",
                        )
                        .limit(1)
                    )
                    if remaining is None:
                        run = session.get(SourceRun, source_run_id)
                        if run is not None:
                            run.raw_cleaned_at = now
        return tuple(results)

    def cleanup_stale_staging_directories(
        self,
        *,
        older_than: timedelta = timedelta(hours=24),
        now_utc: datetime | None = None,
        dry_run: bool = False,
    ) -> tuple[TemporaryDirectoryCleanupResult, ...]:
        """Clean interrupted staging work without touching registered artifacts."""

        return self.artifacts.cleanup_stale_staging_directories(
            older_than=older_than,
            now_utc=now_utc,
            dry_run=dry_run,
        )

    def discard_source_artifacts(self, source_run_id: str) -> tuple[CleanupResult, ...]:
        """Delete every available artifact of a failed source attempt.

        This is intentionally broader than normal retention cleanup: a source
        which failed before reaching ``processed`` must not leave a mixture of
        compact manifests and large normalized grids behind.
        """

        now = utc_now()
        results: list[CleanupResult] = []
        with session_scope(self.sessions) as session:
            if session.get(SourceRun, source_run_id) is None:
                raise KeyError(source_run_id)
            artifacts = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.source_run_id == source_run_id,
                        Artifact.status == "available",
                    )
                    .with_for_update()
                )
            )
            for artifact in artifacts:
                try:
                    removed = self.artifacts.unlink(artifact.storage_path, missing_ok=True)
                except (OSError, ValueError) as exc:
                    results.append(
                        CleanupResult(
                            artifact_id=artifact.id,
                            storage_path=artifact.storage_path,
                            status="error",
                            bytes_reclaimed=0,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                reclaimed = int(artifact.size_bytes or 0) if removed else 0
                artifact.status = "deleted"
                artifact.deleted_at = now
                self._audit(
                    session,
                    entity_type="artifact",
                    entity_id=artifact.id,
                    event_type="discarded_after_source_failure",
                    from_status="available",
                    to_status="deleted",
                    details={"path": artifact.storage_path, "bytes_reclaimed": reclaimed},
                )
                results.append(
                    CleanupResult(
                        artifact_id=artifact.id,
                        storage_path=artifact.storage_path,
                        status="deleted" if removed else "already_missing",
                        bytes_reclaimed=reclaimed,
                    )
                )
        return tuple(results)

    def audit_events(self, *, entity_type: str, entity_id: str) -> tuple[AuditEvent, ...]:
        with self.sessions() as session:
            return tuple(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.entity_type == entity_type, AuditEvent.entity_id == entity_id)
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            )

    # -- internal helpers -------------------------------------------------
    @staticmethod
    def _audit(
        session: Session,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                details=dict(details or {}),
            )
        )

    @staticmethod
    def _select_reference_date(rows: Iterable[SourceRun]) -> date | None:
        by_date: dict[date, set[str]] = {}
        for row in rows:
            by_date.setdefault(row.reference_date, set()).add(row.source_id)
        if not by_date:
            return None

        def score(item: tuple[date, set[str]]):
            reference, models = item
            families = {MODEL_SOURCES[model][1] for model in models}
            return len(families) == 2, len(models), reference

        return max(by_date.items(), key=score)[0]

    def _model_candidate_preference(self, row: SourceRun) -> tuple[int, int, int, datetime]:
        """Prefer usable complete runs; recency only breaks equal-quality ties."""

        usable = bool(self._grid_files(row) or self._has_compact_ensemble(row))
        required_lead = 240 if row.source_id == "ec" else 336
        complete = (
            row.status == "processed"
            and usable
            and int(row.max_lead_h or 0) == required_lead
            and (
                row.source_id not in EXPECTED_ENSEMBLE_MEMBERS
                or row.member_count == EXPECTED_ENSEMBLE_MEMBERS[row.source_id]
            )
        )
        return (
            int(complete),
            int(usable),
            int(row.status == "processed"),
            row.processed_at or datetime.min.replace(tzinfo=UTC),
        )

    @staticmethod
    def _declared_issues(row: SourceRun) -> tuple[str, ...]:
        values = (
            row.quality_manifest.get("issues", []) if isinstance(row.quality_manifest, dict) else []
        )
        return tuple(str(value) for value in values if str(value))

    @staticmethod
    def _has_compact_ensemble(row: SourceRun) -> bool:
        contract = row.normalized_metadata.get("weather_summary_ensemble")
        return (
            row.source_id in EXPECTED_ENSEMBLE_MEMBERS
            and isinstance(contract, dict)
            and bool(contract.get("manifest_path") or contract.get("json_path"))
        )

    @staticmethod
    def _artifact_dict(artifact: Artifact) -> dict[str, Any]:
        result = dict(artifact.details or {})
        result.update(
            {
                "kind": artifact.kind,
                "path": artifact.storage_path,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
        )
        for key, value in (
            ("forecast_hour", artifact.forecast_hour),
            ("member_id", artifact.member_id),
            ("variable", artifact.variable),
            ("level", artifact.level),
        ):
            if value is not None:
                result[key] = value
        return result

    def _grid_files(self, row: SourceRun) -> tuple[dict[str, Any], ...]:
        return tuple(
            self._artifact_dict(item)
            for item in sorted(
                row.artifacts, key=lambda value: (value.forecast_hour or -1, value.storage_path)
            )
            if item.status == "available" and item.kind in GRID_ARTIFACT_KINDS
        )

    def _product_facade(
        self, row: SourceRun, grid_files: tuple[dict[str, Any], ...] | None = None
    ) -> ArtifactProduct:
        files = grid_files if grid_files is not None else self._grid_files(row)
        processed = dict(row.normalized_metadata or {})
        processed.update(
            {
                "reference_date": row.reference_date.isoformat(),
                "cycle": row.cycle or "",
                "run_datetime_utc": _iso(row.run_datetime_utc),
                "max_forecast_hour": row.max_lead_h or 0,
                "complete_storage": {"files": list(files)},
            }
        )
        payload = dict(row.payload or {})
        if row.run_datetime_utc:
            payload.setdefault(
                "forecast_run",
                {
                    "reference_date": row.reference_date.isoformat(),
                    "cycle": row.cycle or "",
                    "run_datetime_utc": _iso(row.run_datetime_utc),
                },
            )
        return ArtifactProduct(
            id=row.id,
            source_slug=row.source_slug,
            product_slug=row.product_slug,
            processed_at=row.processed_at,
            processed_data=processed,
            payload=payload,
            data_produto=row.reference_date.isoformat(),
        )

    @staticmethod
    def _selection_manifest(sources: Iterable[SelectionSource], cutoff: datetime) -> dict[str, Any]:
        values = tuple(sources)
        return {
            "cutoff_utc": _iso(cutoff),
            "sources": [value.as_dict() for value in values],
            "counts": {
                status: sum(value.status == status for value in values)
                for status in ("complete", "partial", "delayed", "missing", "failed")
            },
        }

    def _imerg_rows(self, cutoff: datetime, *, end_date: date | None = None) -> list[SourceRun]:
        with self.sessions() as session:
            query = (
                select(SourceRun)
                .options(selectinload(SourceRun.artifacts))
                .where(
                    SourceRun.source_id == "gpm_imerg",
                    SourceRun.source_slug == "nasa",
                    SourceRun.data_kind == "observed",
                    SourceRun.status.in_(("processed", "partial")),
                    SourceRun.processed_at.is_not(None),
                    SourceRun.processed_at <= cutoff,
                )
                .order_by(
                    desc(SourceRun.reference_date),
                    desc(SourceRun.processed_at),
                    desc(SourceRun.created_at),
                )
            )
            if end_date is not None:
                query = query.where(SourceRun.reference_date <= end_date)
            return list(session.scalars(query).unique())

    def _imerg_record(self, row: SourceRun) -> ObservedIMERGInputRecord:
        grids = self._grid_files(row)
        issues = list(self._declared_issues(row))
        status = "partial" if row.status == "partial" else "complete"
        basin_aggregates = row.normalized_metadata.get("weather_summary_observed_basins")
        if not isinstance(basin_aggregates, dict):
            basin_aggregates = None
        if not grids and basin_aggregates is None:
            status = "partial"
            issues.append("missing_normalized_grid_files")
        start = row.valid_start_utc or datetime.combine(
            row.reference_date, datetime.min.time(), tzinfo=UTC
        )
        end = row.valid_end_utc or start + timedelta(days=1)
        temporal = row.temporal_resolution or "daily"
        complete = True if row.period_complete is None else row.period_complete
        if temporal == "30_minutes" and not complete:
            issues.append("calendar_day_in_progress")
        return ObservedIMERGInputRecord(
            product_data=self._product_facade(row, grids),
            observation_date=row.reference_date,
            grid_files=grids,
            status=status,
            issues=tuple(dict.fromkeys(issues)),
            data_start_utc=start,
            data_end_utc=end,
            temporal_resolution=temporal,
            period_complete=complete,
            coverage_pct=row.coverage_pct,
            basin_aggregates=basin_aggregates,
        )

    @staticmethod
    def _replace_imerg(
        value: ObservedIMERGInputRecord, *, status: str, issues: tuple[str, ...]
    ) -> ObservedIMERGInputRecord:
        return ObservedIMERGInputRecord(
            product_data=value.product_data,
            observation_date=value.observation_date,
            grid_files=value.grid_files,
            status=status,
            issues=issues,
            data_start_utc=value.data_start_utc,
            data_end_utc=value.data_end_utc,
            temporal_resolution=value.temporal_resolution,
            period_complete=value.period_complete,
            coverage_pct=value.coverage_pct,
            basin_aggregates=value.basin_aggregates,
        )

    @staticmethod
    def _imerg_preference(value: ObservedIMERGInputRecord) -> tuple[int, int, datetime]:
        return (
            int(value.status == "complete"),
            int(value.temporal_resolution == "30_minutes"),
            value.product_data.processed_at or datetime.min.replace(tzinfo=UTC),
        )

    @staticmethod
    def _missing_imerg_source(issue: str) -> SelectionSource:
        return SelectionSource(
            source_id="gpm_imerg",
            category="observed_precipitation",
            status="missing",
            required=False,
            source_name="NASA GPM IMERG",
            issues=(issue,),
        )
