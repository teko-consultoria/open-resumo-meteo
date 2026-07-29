"""Daily precipitation normalization for GFS, GEFS mean, EC and ECENS mean."""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from resumo_meteo.ingestion.artifacts import managed_workdir, sha256_path
from resumo_meteo.ingestion.grib import (
    GribPrecipitationField,
    read_precipitation_field,
    subtract_precipitation_fields,
    sum_precipitation_fields,
)
from resumo_meteo.sources.catalog import (
    EDITORIAL_CYCLE,
    WEATHER_SUMMARY_HORIZON_HOURS,
    ModelSourceSpec,
    get_model_spec,
)
from resumo_meteo.sources.ecmwf import ECMWFOpenDataPrecipitationClient
from resumo_meteo.sources.noaa import NOAAPrecipitationClient

SCHEMA_VERSION = "normalized-daily-precipitation.v1"
STORAGE_SCHEMA_VERSION = 1
MAX_NORMALIZED_ROWS = 500_000


@dataclass(frozen=True, slots=True)
class DailyPrecipitationGrid:
    model: str
    run_date: date
    forecast_date: date
    start_lead_hour: int
    end_lead_hour: int
    field: GribPrecipitationField
    source_references: tuple[str, ...]
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelPrecipitationIngestion:
    model: str
    family: str
    source: str
    product_slug: str
    label: str
    reference_date: date
    cycle: str
    max_forecast_hour: int
    generated_at_utc: str
    daily_grids: tuple[DailyPrecipitationGrid, ...]
    status: str
    quality_issues: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.cycle != EDITORIAL_CYCLE:
            raise ValueError("O resumo diario aceita somente a rodada 00z")


@dataclass(frozen=True, slots=True)
class PersistedModelPrecipitation:
    storage_root: str
    manifest_path: str
    files: tuple[dict[str, object], ...]

    def complete_storage(self, *, source: str, product_slug: str) -> dict[str, object]:
        return {
            "schema_version": STORAGE_SCHEMA_VERSION,
            "format": "csv.gz",
            "storage_root": self.storage_root,
            "source": source,
            "product": {"slug": product_slug},
            "manifest_path": self.manifest_path,
            "files": list(self.files),
            "file_counts": {
                "total": len(self.files),
                "grid_forecast": len(self.files),
                "aggregated_points": 0,
                "satellite_points": 0,
                "unrecognized": 0,
            },
            "row_counts": {
                "grid_forecast": sum(int(item.get("row_count") or 0) for item in self.files),
                "aggregated_points": 0,
                "satellite_points": 0,
            },
            "raw_grib_persisted": False,
        }


def _field_summary(field: GribPrecipitationField) -> dict[str, object]:
    values = field.values_mm
    finite = values[np.isfinite(values)]
    if not finite.size:
        return {"point_count": 0, "min": None, "max": None, "avg": None}
    return {
        "point_count": int(finite.size),
        "min": float(finite.min()),
        "max": float(finite.max()),
        "avg": float(finite.mean()),
    }


def _noaa_daily_grids(
    spec: ModelSourceSpec,
    *,
    reference_date: date,
    cycle: str,
    end_hours: Sequence[int],
    work_dir: Path,
    client: NOAAPrecipitationClient,
    max_workers: int = 1,
) -> tuple[DailyPrecipitationGrid, ...]:
    def one_day(day_index: int, daily_end_hour: int) -> DailyPrecipitationGrid:
        # requests.Session is not shared across workers.
        day_client = client if max_workers == 1 else NOAAPrecipitationClient(
            timeout=client.timeout,
            max_attempts=client.max_attempts,
            retry_backoff_seconds=client.retry_backoff_seconds,
            max_retry_delay_seconds=client.max_retry_delay_seconds,
        )
        interval_fields: list[GribPrecipitationField] = []
        references: list[str] = []
        for interval_end_hour in range(daily_end_hour - 18, daily_end_hour + 1, 6):
            interval_start_hour = max(0, interval_end_hour - 6)
            selection = day_client.select_apcp_interval(
                spec.product_slug,
                reference_date,
                cycle=cycle,
                interval_start_hour=interval_start_hour,
                interval_end_hour=interval_end_hour,
            )
            path = work_dir / (
                f"{spec.model}-{interval_start_hour:03d}-{interval_end_hour:03d}.grib2"
            )
            try:
                day_client.download_selection(selection, path)
                interval_fields.append(
                    read_precipitation_field(path, source_slug=spec.source)
                )
            finally:
                path.unlink(missing_ok=True)
            references.extend((selection.index_url, selection.grib_url))
        daily = sum_precipitation_fields(interval_fields)
        try:
            return DailyPrecipitationGrid(
                model=spec.model,
                run_date=reference_date,
                forecast_date=reference_date + timedelta(days=day_index),
                start_lead_hour=daily_end_hour - 24,
                end_lead_hour=daily_end_hour,
                field=daily,
                source_references=tuple(references),
                metadata={
                    "accumulation_hours": 24,
                    "source_message_count": sum(item.message_count for item in interval_fields),
                    "summary": _field_summary(daily),
                    "deaccumulation_method": "native_6h_apcp_sum",
                },
            )
        finally:
            if day_client is not client:
                day_client.session.close()

    indexed = tuple(enumerate(end_hours, start=1))
    if max_workers == 1:
        return tuple(one_day(day, end) for day, end in indexed)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(indexed)), thread_name_prefix="gfs-apcp") as executor:
        return tuple(executor.map(lambda item: one_day(*item), indexed))


def _ecmwf_daily_grids(
    spec: ModelSourceSpec,
    *,
    reference_date: date,
    cycle: str,
    end_hours: Sequence[int],
    work_dir: Path,
    client: ECMWFOpenDataPrecipitationClient,
) -> tuple[DailyPrecipitationGrid, ...]:
    grids: list[DailyPrecipitationGrid] = []
    previous: GribPrecipitationField | None = None
    for day_index, daily_end_hour in enumerate(end_hours, start=1):
        records = client.select_tp_messages(
            spec.product_slug,
            reference_date,
            cycle=cycle,
            forecast_hour=daily_end_hour,
        )
        grib_url = client.grib_url(
            spec.product_slug,
            reference_date,
            cycle=cycle,
            forecast_hour=daily_end_hour,
        )
        index_url = client.index_url(
            spec.product_slug,
            reference_date,
            cycle=cycle,
            forecast_hour=daily_end_hour,
        )
        path = work_dir / f"{spec.model}-tp-f{daily_end_hour:03d}.grib2"
        try:
            client.download_messages(records, grib_url=grib_url, output_path=path)
            cumulative = read_precipitation_field(
                path,
                source_slug=spec.source,
                aggregation="mean" if spec.ensemble else "sum",
            )
        finally:
            path.unlink(missing_ok=True)
        daily = subtract_precipitation_fields(cumulative, previous)
        previous = cumulative
        grids.append(
            DailyPrecipitationGrid(
                model=spec.model,
                run_date=reference_date,
                forecast_date=reference_date + timedelta(days=day_index),
                start_lead_hour=daily_end_hour - 24,
                end_lead_hour=daily_end_hour,
                field=daily,
                source_references=(index_url, grib_url),
                metadata={
                    "accumulation_hours": 24,
                    "source_message_count": cumulative.message_count,
                    "ensemble_members": cumulative.message_count if spec.ensemble else None,
                    "summary": _field_summary(daily),
                    "deaccumulation_method": "cumulative_difference",
                },
            )
        )
    return tuple(grids)


def ingest_model_precipitation(
    *,
    model: str,
    reference_date: date,
    cycle: str = EDITORIAL_CYCLE,
    horizon_hours: int = WEATHER_SUMMARY_HORIZON_HOURS,
    temporary_root: str | Path | None = None,
    noaa_client: NOAAPrecipitationClient | None = None,
    noaa_max_workers: int = 1,
    ecmwf_client: ECMWFOpenDataPrecipitationClient | None = None,
    generated_at_utc: datetime | None = None,
) -> ModelPrecipitationIngestion:
    if isinstance(reference_date, datetime) or not isinstance(reference_date, date):
        raise TypeError("reference_date deve ser date")
    if str(cycle).zfill(2) != EDITORIAL_CYCLE:
        raise ValueError("O resumo diario aceita somente a rodada 00z")
    spec = get_model_spec(model)
    requested_horizon = min(int(horizon_hours), spec.max_forecast_hour)
    end_hours = spec.daily_end_hours(horizon_hours=requested_horizon)
    if not end_hours:
        raise ValueError("Horizonte deve incluir ao menos 24 horas")
    owns_client = False
    with managed_workdir(temporary_root=temporary_root, prefix=f"{spec.model}-precip-") as work:
        try:
            if spec.source == "noaa":
                client = noaa_client or NOAAPrecipitationClient()
                owns_client = noaa_client is None
                grids = _noaa_daily_grids(
                    spec,
                    reference_date=reference_date,
                    cycle=EDITORIAL_CYCLE,
                    end_hours=end_hours,
                    work_dir=work,
                    client=client,
                    max_workers=max(1, int(noaa_max_workers)),
                )
            else:
                client = ecmwf_client or ECMWFOpenDataPrecipitationClient()
                owns_client = ecmwf_client is None
                grids = _ecmwf_daily_grids(
                    spec,
                    reference_date=reference_date,
                    cycle=EDITORIAL_CYCLE,
                    end_hours=end_hours,
                    work_dir=work,
                    client=client,
                )
        finally:
            if owns_client:
                close = getattr(getattr(client, "session", None), "close", None)
                if callable(close):
                    close()
    generated = generated_at_utc or datetime.now(UTC)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at_utc deve ter timezone explicito")
    return ModelPrecipitationIngestion(
        model=spec.model,
        family=spec.family,
        source=spec.source,
        product_slug=spec.product_slug,
        label=spec.label,
        reference_date=reference_date,
        cycle=EDITORIAL_CYCLE,
        max_forecast_hour=end_hours[-1],
        generated_at_utc=generated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        daily_grids=grids,
        status="complete",
    )


def _csv_gzip_bytes(grid: DailyPrecipitationGrid) -> bytes:
    columns = (
        "run_date",
        "forecast_date",
        "longitude",
        "latitude",
        "source_coordinate_1",
        "source_coordinate_2",
        "precipitation_mm",
    )
    binary = io.BytesIO()
    with gzip.GzipFile(fileobj=binary, mode="wb", mtime=0) as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(columns)
            row_count = 0
            for longitude, latitude, precipitation in grid.field.iter_rows():
                if row_count >= MAX_NORMALIZED_ROWS:
                    raise ValueError("Grade normalizada excede limite de linhas")
                if not all(math.isfinite(value) for value in (longitude, latitude, precipitation)):
                    continue
                writer.writerow(
                    (
                        grid.run_date.isoformat(),
                        grid.forecast_date.isoformat(),
                        longitude,
                        latitude,
                        longitude,
                        latitude,
                        precipitation,
                    )
                )
                row_count += 1
    return binary.getvalue()


def persist_model_precipitation(
    result: ModelPrecipitationIngestion,
    *,
    artifact_root: str | Path,
    run_id: str,
) -> PersistedModelPrecipitation:
    if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in run_id):
        raise ValueError("run_id deve ser identificador seguro")
    root = Path(artifact_root).resolve()
    target = (
        root
        / "processed_weather"
        / result.source
        / result.product_slug
        / result.reference_date.strftime("%Y/%m/%d")
        / f"{result.cycle}z"
        / run_id
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Ingestao de precipitacao ja existe: {target}")
    staging_root = root / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".tmp", dir=staging_root))
    try:
        files: list[dict[str, object]] = []
        data_dir = temporary / "data"
        data_dir.mkdir()
        for index, grid in enumerate(result.daily_grids, start=1):
            filename = f"{index:06d}-grid-{result.model}-f{grid.end_lead_hour:03d}.csv.gz"
            path = data_dir / filename
            path.write_bytes(_csv_gzip_bytes(grid))
            row_count = int(grid.field.point_count)
            values = grid.field.values_mm
            finite = values[np.isfinite(values)]
            future_path = target / "data" / filename
            files.append(
                {
                    "kind": "grid_forecast",
                    "source_path": f"{result.model}-daily-{grid.start_lead_hour:03d}-{grid.end_lead_hour:03d}h.grib2",
                    "filename": filename,
                    "model_prefix": result.label,
                    "run_date": grid.run_date.isoformat(),
                    "forecast_date": grid.forecast_date.isoformat(),
                    "columns": [
                        "run_date", "forecast_date", "longitude", "latitude",
                        "source_coordinate_1", "source_coordinate_2", "precipitation_mm",
                    ],
                    "row_count": row_count,
                    "invalid_row_count": 0,
                    "coordinate_order": "longitude_latitude",
                    "bounds": {
                        "longitude": {"min": float(grid.field.longitudes.min()), "max": float(grid.field.longitudes.max())},
                        "latitude": {"min": float(grid.field.latitudes.min()), "max": float(grid.field.latitudes.max())},
                    },
                    "precipitation_mm": {
                        "min": float(finite.min()) if finite.size else None,
                        "max": float(finite.max()) if finite.size else None,
                        "avg": float(finite.mean()) if finite.size else None,
                    },
                    "path": future_path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_path(path),
                    "metadata": {
                        **dict(grid.metadata),
                        "source_references": list(grid.source_references),
                        "start_lead_hour": grid.start_lead_hour,
                        "end_lead_hour": grid.end_lead_hour,
                    },
                }
            )
        manifest = {
            "schema_version": STORAGE_SCHEMA_VERSION,
            "normalized_schema_version": SCHEMA_VERSION,
            "format": "csv.gz",
            "storage_root": target.relative_to(root).as_posix(),
            "source": result.source,
            "product": {"slug": result.product_slug},
            "source_metadata": {
                "model": result.model,
                "family": result.family,
                "reference_date": result.reference_date.isoformat(),
                "cycle": result.cycle,
                "max_forecast_hour": result.max_forecast_hour,
            },
            "raw_grib_persisted": False,
            "files": files,
        }
        manifest_path = temporary / "manifest.json"
        manifest["manifest_path"] = (target / manifest_path.name).relative_to(root).as_posix()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PersistedModelPrecipitation(
        storage_root=target.relative_to(root).as_posix(),
        manifest_path=(target / "manifest.json").relative_to(root).as_posix(),
        files=tuple(files),
    )


def build_model_processed_data(
    result: ModelPrecipitationIngestion,
    artifacts: PersistedModelPrecipitation,
) -> dict[str, object]:
    complete_storage = artifacts.complete_storage(
        source=result.source,
        product_slug=result.product_slug,
    )
    return {
        "processor": "public_precipitation_forecast",
        "schema_version": 1,
        "normalized_schema_version": SCHEMA_VERSION,
        "source": {
            "source_slug": result.source,
            "model_label": result.label,
        },
        "product_slug": result.product_slug,
        "model_key": result.model,
        "model_label": result.label,
        "family": result.family,
        "reference_date": result.reference_date.isoformat(),
        "cycle": result.cycle,
        "run_datetime_utc": datetime.combine(
            result.reference_date, datetime.min.time(), tzinfo=UTC
        ).isoformat().replace("+00:00", "Z"),
        "max_forecast_hour": result.max_forecast_hour,
        "forecast_dates": [item.forecast_date.isoformat() for item in result.daily_grids],
        "forecast_start_date": result.daily_grids[0].forecast_date.isoformat(),
        "forecast_end_date": result.daily_grids[-1].forecast_date.isoformat(),
        "horizon_days": len(result.daily_grids),
        "complete_storage": complete_storage,
        "grid_files": list(artifacts.files),
        "quality": {
            "status": result.status,
            "issues": [dict(item) for item in result.quality_issues],
        },
    }


__all__ = [
    "DailyPrecipitationGrid",
    "ModelPrecipitationIngestion",
    "PersistedModelPrecipitation",
    "build_model_processed_data",
    "ingest_model_precipitation",
    "persist_model_precipitation",
]
