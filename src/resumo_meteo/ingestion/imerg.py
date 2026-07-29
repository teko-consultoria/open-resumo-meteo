"""NASA IMERG Early ingestion for the observed-rain antecedent condition."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np

from resumo_meteo.core.geometry_loader import GeometryCatalog, load_geometry_catalog
from resumo_meteo.core.observed_aggregation import aggregate_observed_rows_by_basin
from resumo_meteo.core.summary_scope import WEATHER_SUMMARY_BASIN_IDS
from resumo_meteo.ingestion.artifacts import managed_workdir, sha256_path
from resumo_meteo.sources.nasa import (
    GPM_IMERG_DAILY_PRODUCT_SLUG,
    GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
    NASAGranule,
    NASAObservedWeatherClient,
    NASAObservedWeatherUnavailable,
)

SCHEMA_VERSION = "gpm-imerg-half-hour-snapshot.v1"
DAILY_SCHEMA_VERSION = "gpm-imerg-daily-snapshot.v1"
LON_MIN = -75.5
LON_MAX = -32.0
LAT_MIN = -35.0
LAT_MAX = 6.5


@dataclass(frozen=True, slots=True)
class IMERGSnapshot:
    product_date: date
    snapshot_id: str
    valid_start_utc: datetime
    valid_end_utc: datetime
    interval_count: int
    expected_interval_count: int
    coverage_pct: float
    period_complete: bool
    granule_ids: tuple[str, ...]
    rows: tuple[tuple[float, float, float], ...]
    reader_summary: Mapping[str, object]
    weather_summary_observed_basins: Mapping[str, object] | None
    generated_at_utc: str
    product_slug: str = GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG
    collection_short_name: str = "GPM_3IMERGHHE"
    temporal_resolution: str = "30_minutes"
    run_label: str = "early"
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class PersistedIMERGSnapshot:
    storage_root: str
    manifest_path: str
    files: tuple[dict[str, object], ...]
    product_slug: str = GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG

    def complete_storage(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "format": "csv.gz",
            "storage_root": self.storage_root,
            "source": "nasa",
            "product": {"slug": self.product_slug},
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
            "raw_hdf_persisted": False,
        }


def _import_h5py():
    try:
        import h5py
    except ModuleNotFoundError as exc:
        raise RuntimeError("h5py nao instalado; necessario para NASA IMERG") from exc
    return h5py


def _iter_hdf_datasets(group, prefix=""):
    for key, value in group.items():
        path = f"{prefix}/{key}" if prefix else str(key)
        if hasattr(value, "shape") and hasattr(value, "dtype"):
            yield path, value
        elif hasattr(value, "items"):
            yield from _iter_hdf_datasets(value, path)


def _select_dataset(h5, names: tuple[str, ...]):
    candidates = []
    normalized_names = tuple(name.lower() for name in names)
    for path, dataset in _iter_hdf_datasets(h5):
        lower_path = path.lower()
        basename = lower_path.rsplit("/", 1)[-1]
        if basename in normalized_names:
            return path, dataset
        if any(name in lower_path for name in normalized_names):
            candidates.append((path, dataset))
    if candidates:
        return candidates[0]
    raise ValueError(f"Dataset HDF nao encontrado: {names}")


def _dataset_values(dataset) -> np.ndarray:
    data = np.squeeze(np.array(dataset[()])).astype(float, copy=False)
    attrs = getattr(dataset, "attrs", {})
    fill_value = attrs.get("_FillValue", attrs.get("missing_value"))
    if fill_value is not None:
        fill = float(np.array(fill_value).squeeze())
        data = np.where(np.isclose(data, fill), np.nan, data)
    scale = attrs.get("scale_factor", 1.0)
    offset = attrs.get("add_offset", 0.0)
    try:
        data = data * float(np.array(scale).squeeze()) + float(np.array(offset).squeeze())
    except (TypeError, ValueError):
        pass
    return data


def _grid_value(
    data: np.ndarray,
    *,
    lat_index: int,
    lon_index: int,
    lat_count: int,
    lon_count: int,
) -> float:
    if data.shape == (lat_count, lon_count):
        return float(data[lat_index, lon_index])
    if data.shape == (lon_count, lat_count):
        return float(data[lon_index, lat_index])
    raise ValueError(
        f"Formato de grid HDF nao suportado: {data.shape} para lat={lat_count}, lon={lon_count}"
    )


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "avg": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
    }


def read_gpm_imerg_rows(
    path: str | Path,
    *,
    accumulation_hours: float = 0.5,
    source_rate_period_hours: float = 1.0,
    source_units: str = "mm h-1",
) -> tuple[list[tuple[float, float, float]], dict[str, object]]:
    """Read one IMERG rate grid and convert it to interval millimetres."""

    duration = float(accumulation_hours)
    rate_period = float(source_rate_period_hours)
    if not 0 < duration <= 24:
        raise ValueError("accumulation_hours IMERG deve estar entre zero e 24")
    if not 0 < rate_period <= 24:
        raise ValueError("source_rate_period_hours IMERG deve estar entre zero e 24")
    h5py = _import_h5py()
    with h5py.File(path, "r") as h5:
        _lon_path, lon_dataset = _select_dataset(h5, ("lon", "longitude"))
        _lat_path, lat_dataset = _select_dataset(h5, ("lat", "latitude"))
        longitudes = np.squeeze(np.array(lon_dataset[()], dtype=float))
        latitudes = np.squeeze(np.array(lat_dataset[()], dtype=float))
        dataset_path, dataset = _select_dataset(h5, ("precipitation", "precipitationcal"))
        data = _dataset_values(dataset)
    lon_indices = [
        index for index, value in enumerate(longitudes) if LON_MIN <= float(value) <= LON_MAX
    ]
    lat_indices = [
        index for index, value in enumerate(latitudes) if LAT_MIN <= float(value) <= LAT_MAX
    ]
    rows: list[tuple[float, float, float]] = []
    values: list[float] = []
    invalid = 0
    for lat_index in lat_indices:
        latitude = float(latitudes[lat_index])
        for lon_index in lon_indices:
            longitude = float(longitudes[lon_index])
            rate = _grid_value(
                data,
                lat_index=lat_index,
                lon_index=lon_index,
                lat_count=len(latitudes),
                lon_count=len(longitudes),
            )
            if not math.isfinite(rate) or rate < 0:
                invalid += 1
                continue
            value = rate * duration / rate_period
            rows.append((longitude, latitude, value))
            values.append(value)
    if not rows:
        raise ValueError(f"Granulo IMERG sem pontos validos no dominio: {path}")
    return rows, {
        "reader": "h5py",
        "dataset_path": dataset_path,
        "grid_shape": list(data.shape),
        "longitude_count": len(longitudes),
        "latitude_count": len(latitudes),
        "candidate_point_count": len(lon_indices) * len(lat_indices),
        "invalid_row_count": invalid,
        "source_units": source_units,
        "source_rate_period_hours": rate_period,
        "accumulation_hours": duration,
        "precipitation_mm": _stats(values),
    }


def _coordinate_key(longitude: float, latitude: float) -> tuple[float, float]:
    return round(float(longitude), 8), round(float(latitude), 8)


def accumulate_imerg_granules(
    downloads: Sequence[tuple[NASAGranule, Path]],
) -> tuple[tuple[tuple[float, float, float], ...], dict[str, object]]:
    accumulated: dict[tuple[float, float], float] | None = None
    dropped_cells = 0
    readers: list[dict[str, object]] = []
    for granule, path in downloads:
        rows, summary = read_gpm_imerg_rows(path, accumulation_hours=0.5)
        current = {_coordinate_key(lon, lat): value for lon, lat, value in rows}
        if len(current) != len(rows):
            raise ValueError("Granulo IMERG possui coordenadas duplicadas")
        if accumulated is None:
            accumulated = current
        else:
            common = set(accumulated).intersection(current)
            dropped_cells += len(accumulated) - len(common)
            accumulated = {key: accumulated[key] + current[key] for key in common}
        readers.append(
            {
                "concept_id": granule.concept_id,
                "start_utc": granule.start_utc.isoformat().replace("+00:00", "Z") if granule.start_utc else None,
                "end_utc": granule.end_utc.isoformat().replace("+00:00", "Z") if granule.end_utc else None,
                "reader": summary,
            }
        )
    if not accumulated:
        raise ValueError("Snapshot IMERG nao produziu grade acumulada")
    rows = tuple((lon, lat, accumulated[(lon, lat)]) for lon, lat in sorted(accumulated))
    return rows, {
        "reader": "gpm_imerg_half_hour",
        "source_units": "mm h-1",
        "interval_accumulation_hours": 0.5,
        "downloaded_granule_count": len(downloads),
        "dropped_cell_count": dropped_cells,
        "precipitation_mm": _stats([item[2] for item in rows]),
        "download_readers": readers,
    }


def _selected_granules(
    granules: Iterable[NASAGranule],
    *,
    product_date: date,
    cutoff_utc: datetime | None,
) -> tuple[NASAGranule, ...]:
    cutoff = cutoff_utc.astimezone(UTC) if cutoff_utc else None
    selected = tuple(
        sorted(
            (
                item
                for item in granules
                if item.start_utc is not None
                and item.end_utc is not None
                and item.start_utc.astimezone(UTC).date() == product_date
                and item.end_utc > item.start_utc
                and item.end_utc - item.start_utc <= timedelta(minutes=30)
                and (cutoff is None or item.end_utc.astimezone(UTC) <= cutoff)
            ),
            key=lambda item: (item.start_utc, item.concept_id),
        )
    )
    if not selected:
        raise NASAObservedWeatherUnavailable(
            f"Nenhum granulo IMERG Early disponivel em {product_date.isoformat()}"
        )
    ids = [item.concept_id for item in selected]
    starts = [item.start_utc for item in selected]
    if len(ids) != len(set(ids)) or len(starts) != len(set(starts)):
        raise ValueError("CMR retornou granulos IMERG duplicados")
    return selected


def ingest_imerg_early_snapshot(
    *,
    product_date: date,
    earthdata_token: str,
    cutoff_utc: datetime | None = None,
    client: NASAObservedWeatherClient | None = None,
    max_workers: int = 4,
    temporary_root: str | Path | None = None,
    geometry_catalog: GeometryCatalog | None = None,
    aggregate_basins: bool = True,
    generated_at_utc: datetime | None = None,
) -> IMERGSnapshot:
    if isinstance(product_date, datetime) or not isinstance(product_date, date):
        raise TypeError("product_date deve ser date")
    if not 1 <= int(max_workers) <= 16:
        raise ValueError("max_workers deve estar entre 1 e 16")
    source_client = client or NASAObservedWeatherClient(earthdata_token=earthdata_token)
    if not source_client.is_configured():
        raise NASAObservedWeatherUnavailable("NASA_EARTHDATA_TOKEN nao configurado")
    owns_client = client is None
    try:
        granules = _selected_granules(
            source_client.find_granules(GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG, product_date),
            product_date=product_date,
            cutoff_utc=cutoff_utc,
        )
        with managed_workdir(temporary_root=temporary_root, prefix="imerg-early-") as work:
            def download_one(index_item: tuple[int, NASAGranule]):
                index, granule = index_item
                suffix = Path(granule.url.split("?", 1)[0]).suffix or ".HDF5"
                path = work / f"{index:03d}-{granule.concept_id}{suffix}"
                worker_client = source_client
                isolated = False
                if owns_client and int(max_workers) > 1:
                    worker_client = NASAObservedWeatherClient(
                        cmr_url=source_client.cmr_url,
                        earthdata_token=source_client.earthdata_token,
                        timeout=source_client.timeout,
                        allowed_hosts=source_client.allowed_hosts,
                    )
                    isolated = True
                try:
                    worker_client.download_granule(granule, path)
                    return granule, path
                finally:
                    if isolated:
                        worker_client.session.close()

            indexed = tuple(enumerate(granules, start=1))
            worker_count = min(int(max_workers), len(indexed))
            if worker_count > 1:
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="imerg") as executor:
                    downloads = tuple(executor.map(download_one, indexed))
            else:
                downloads = tuple(download_one(item) for item in indexed)
            rows, reader_summary = accumulate_imerg_granules(downloads)
    finally:
        if owns_client:
            source_client.session.close()

    valid_start = granules[0].start_utc.astimezone(UTC)
    valid_end = max(item.end_utc.astimezone(UTC) for item in granules)
    expected_intervals = 48
    day_end = datetime.combine(product_date + timedelta(days=1), time.min, tzinfo=UTC)
    period_complete = (
        valid_start == datetime.combine(product_date, time.min, tzinfo=UTC)
        and valid_end >= day_end - timedelta(seconds=1)
        and len(granules) >= expected_intervals
    )
    coverage = min(100.0, 100.0 * len(granules) / expected_intervals)
    digest_payload = "\n".join(
        f"{item.concept_id}|{item.start_utc.isoformat()}|{item.end_utc.isoformat()}"
        for item in granules
    ).encode("utf-8")
    snapshot_id = hashlib.sha256(digest_payload).hexdigest()
    observed_basins = None
    if aggregate_basins:
        catalog = geometry_catalog or load_geometry_catalog()
        observed_basins = aggregate_observed_rows_by_basin(
            rows,
            observation_date=product_date,
            geometry_catalog=catalog,
            valid_start_utc=valid_start,
            valid_end_utc=valid_end,
            temporal_resolution="30_minutes",
            period_complete=period_complete,
            basin_ids=WEATHER_SUMMARY_BASIN_IDS,
        )
    generated = generated_at_utc or datetime.now(UTC)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at_utc deve ter timezone explicito")
    return IMERGSnapshot(
        product_date=product_date,
        snapshot_id=snapshot_id,
        valid_start_utc=valid_start,
        valid_end_utc=valid_end,
        interval_count=len(granules),
        expected_interval_count=expected_intervals,
        coverage_pct=coverage,
        period_complete=period_complete,
        granule_ids=tuple(item.concept_id for item in granules),
        rows=rows,
        reader_summary=reader_summary,
        weather_summary_observed_basins=observed_basins,
        generated_at_utc=generated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    )


def ingest_imerg_daily_snapshot(
    *,
    product_date: date,
    earthdata_token: str,
    client: NASAObservedWeatherClient | None = None,
    temporary_root: str | Path | None = None,
    geometry_catalog: GeometryCatalog | None = None,
    aggregate_basins: bool = True,
    generated_at_utc: datetime | None = None,
) -> IMERGSnapshot:
    """Ingest one completed IMERG daily granule for antecedent history.

    Historical days use GPM_3IMERGDL (one roughly 30 MB granule) instead of
    downloading the 48 half-hourly Early granules. The daily field is in
    mm/day, so its numeric value is already the 24-hour accumulation.
    """

    if isinstance(product_date, datetime) or not isinstance(product_date, date):
        raise TypeError("product_date deve ser date")
    source_client = client or NASAObservedWeatherClient(earthdata_token=earthdata_token)
    if not source_client.is_configured():
        raise NASAObservedWeatherUnavailable("NASA_EARTHDATA_TOKEN nao configurado")
    owns_client = client is None
    try:
        granules = tuple(
            item
            for item in source_client.find_granules(
                GPM_IMERG_DAILY_PRODUCT_SLUG, product_date
            )
            if item.product_date == product_date
        )
        if not granules:
            raise NASAObservedWeatherUnavailable(
                f"Nenhum granulo IMERG diario disponivel em {product_date.isoformat()}"
            )
        granule = granules[-1]
        with managed_workdir(
            temporary_root=temporary_root, prefix="imerg-daily-"
        ) as work:
            suffix = Path(granule.url.split("?", 1)[0]).suffix or ".HDF5"
            path = work / f"daily-{granule.concept_id}{suffix}"
            source_client.download_granule(granule, path)
            rows_list, reader_summary = read_gpm_imerg_rows(
                path,
                accumulation_hours=24,
                source_rate_period_hours=24,
                source_units="mm day-1",
            )
            rows = tuple(rows_list)
    finally:
        if owns_client:
            source_client.session.close()

    valid_start = datetime.combine(product_date, time.min, tzinfo=UTC)
    valid_end = valid_start + timedelta(days=1)
    observed_basins = None
    if aggregate_basins:
        catalog = geometry_catalog or load_geometry_catalog()
        observed_basins = aggregate_observed_rows_by_basin(
            rows,
            observation_date=product_date,
            geometry_catalog=catalog,
            valid_start_utc=valid_start,
            valid_end_utc=valid_end,
            temporal_resolution="daily",
            period_complete=True,
            basin_ids=WEATHER_SUMMARY_BASIN_IDS,
        )
    generated = generated_at_utc or datetime.now(UTC)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at_utc deve ter timezone explicito")
    snapshot_id = hashlib.sha256(
        f"{granule.concept_id}|{product_date.isoformat()}|daily".encode()
    ).hexdigest()
    return IMERGSnapshot(
        product_date=product_date,
        snapshot_id=snapshot_id,
        valid_start_utc=valid_start,
        valid_end_utc=valid_end,
        interval_count=1,
        expected_interval_count=1,
        coverage_pct=100.0,
        period_complete=True,
        granule_ids=(granule.concept_id,),
        rows=rows,
        reader_summary={
            **dict(reader_summary),
            "reader": "gpm_imerg_daily",
            "downloaded_granule_count": 1,
        },
        weather_summary_observed_basins=observed_basins,
        generated_at_utc=generated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        product_slug=GPM_IMERG_DAILY_PRODUCT_SLUG,
        collection_short_name="GPM_3IMERGDL",
        temporal_resolution="daily",
        run_label="daily",
        schema_version=DAILY_SCHEMA_VERSION,
    )


def _csv_gzip_bytes(snapshot: IMERGSnapshot) -> bytes:
    binary = io.BytesIO()
    with gzip.GzipFile(fileobj=binary, mode="wb", mtime=0) as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(
                (
                    "run_date", "forecast_date", "longitude", "latitude",
                    "source_coordinate_1", "source_coordinate_2", "precipitation_mm",
                )
            )
            for longitude, latitude, precipitation in snapshot.rows:
                writer.writerow(
                    (
                        snapshot.product_date.isoformat(),
                        snapshot.product_date.isoformat(),
                        longitude,
                        latitude,
                        longitude,
                        latitude,
                        precipitation,
                    )
                )
    return binary.getvalue()


def persist_imerg_snapshot(
    snapshot: IMERGSnapshot,
    *,
    artifact_root: str | Path,
    run_id: str,
) -> PersistedIMERGSnapshot:
    if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in run_id):
        raise ValueError("run_id deve ser identificador seguro")
    root = Path(artifact_root).resolve()
    target = (
        root
        / "processed_weather"
        / "nasa"
        / snapshot.product_slug
        / snapshot.product_date.strftime("%Y/%m/%d")
        / run_id
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Snapshot IMERG ja existe: {target}")
    staging_root = root / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".tmp", dir=staging_root))
    try:
        data_dir = temporary / "data"
        data_dir.mkdir()
        filename = f"000001-grid-imerg-{snapshot.snapshot_id[:12]}.csv.gz"
        data_path = data_dir / filename
        data_path.write_bytes(_csv_gzip_bytes(snapshot))
        values = [row[2] for row in snapshot.rows]
        file_entry = {
            "kind": "grid_forecast",
            "source_path": (
                f"{snapshot.collection_short_name}-{snapshot.product_date:%Y%m%d}-"
                f"{snapshot.snapshot_id[:12]}"
            ),
            "filename": filename,
            "model_prefix": (
                "GPM IMERG Early"
                if snapshot.temporal_resolution == "30_minutes"
                else "GPM IMERG Daily"
            ),
            "run_date": snapshot.product_date.isoformat(),
            "forecast_date": snapshot.product_date.isoformat(),
            "columns": [
                "run_date", "forecast_date", "longitude", "latitude",
                "source_coordinate_1", "source_coordinate_2", "precipitation_mm",
            ],
            "row_count": len(snapshot.rows),
            "invalid_row_count": 0,
            "coordinate_order": "longitude_latitude",
            "bounds": {
                "longitude": {"min": min(item[0] for item in snapshot.rows), "max": max(item[0] for item in snapshot.rows)},
                "latitude": {"min": min(item[1] for item in snapshot.rows), "max": max(item[1] for item in snapshot.rows)},
            },
            "precipitation_mm": _stats(values),
            "path": (target / "data" / filename).relative_to(root).as_posix(),
            "size_bytes": data_path.stat().st_size,
            "sha256": sha256_path(data_path),
            "metadata": {
                "temporal_resolution": snapshot.temporal_resolution,
                "valid_start_utc": snapshot.valid_start_utc.isoformat().replace("+00:00", "Z"),
                "valid_end_utc": snapshot.valid_end_utc.isoformat().replace("+00:00", "Z"),
                "period_complete": snapshot.period_complete,
                "coverage_pct": snapshot.coverage_pct,
                "reader_summary": dict(snapshot.reader_summary),
            },
        }
        manifest = {
            "schema_version": 1,
            "snapshot_schema_version": snapshot.schema_version,
            "format": "csv.gz",
            "storage_root": target.relative_to(root).as_posix(),
            "source": "nasa",
            "product": {"slug": snapshot.product_slug},
            "raw_hdf_persisted": False,
            "files": [file_entry],
            "manifest_path": (target / "manifest.json").relative_to(root).as_posix(),
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PersistedIMERGSnapshot(
        storage_root=target.relative_to(root).as_posix(),
        manifest_path=(target / "manifest.json").relative_to(root).as_posix(),
        files=(file_entry,),
        product_slug=snapshot.product_slug,
    )


def build_imerg_processed_data(
    snapshot: IMERGSnapshot,
    artifacts: PersistedIMERGSnapshot,
) -> dict[str, object]:
    snapshot_contract = {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "collection_short_name": snapshot.collection_short_name,
        "collection_version": "07",
        "run": snapshot.run_label,
        "product_date": snapshot.product_date.isoformat(),
        "valid_start_utc": snapshot.valid_start_utc.isoformat().replace("+00:00", "Z"),
        "valid_end_utc": snapshot.valid_end_utc.isoformat().replace("+00:00", "Z"),
        "temporal_resolution": snapshot.temporal_resolution,
        "interval_count": snapshot.interval_count,
        "expected_interval_count": snapshot.expected_interval_count,
        "coverage_pct": snapshot.coverage_pct,
        "period_complete": snapshot.period_complete,
        "granule_ids": list(snapshot.granule_ids),
        "reused_granule_count": 0,
        "downloaded_granule_count": snapshot.interval_count,
        "raw_hdf_persisted": False,
    }
    result = {
        "processor": "observed_weather",
        "schema_version": 1,
        "source": {
            "source_slug": "nasa",
            "source_label": "NASA",
            "source_footnote": (
                f"NASA GES DISC {snapshot.collection_short_name} V07"
            ),
            "nominal_resolution": "0.1 degree",
        },
        "product_slug": snapshot.product_slug,
        "model_label": (
            "GPM IMERG Early"
            if snapshot.temporal_resolution == "30_minutes"
            else "GPM IMERG Daily"
        ),
        "reference_date": snapshot.product_date.isoformat(),
        "run_date": snapshot.product_date.isoformat(),
        "forecast_dates": [snapshot.product_date.isoformat()],
        "forecast_start_date": snapshot.product_date.isoformat(),
        "forecast_end_date": snapshot.product_date.isoformat(),
        "horizon_days": 1,
        "observation_kind": "precipitation",
        "variable_key": "precipitation_mm",
        "observation_temporal_resolution": snapshot.temporal_resolution,
        "complete_storage": artifacts.complete_storage(),
        "grid_files": list(artifacts.files),
        "imerg_snapshot": snapshot_contract,
        "reader_summary": dict(snapshot.reader_summary),
    }
    if snapshot.weather_summary_observed_basins is not None:
        result["weather_summary_observed_basins"] = dict(
            snapshot.weather_summary_observed_basins
        )
    if snapshot.temporal_resolution == "30_minutes":
        result["imerg_half_hour_snapshot"] = snapshot_contract
    else:
        result["imerg_daily_snapshot"] = snapshot_contract
    return result


__all__ = [
    "IMERGSnapshot",
    "PersistedIMERGSnapshot",
    "accumulate_imerg_granules",
    "build_imerg_processed_data",
    "ingest_imerg_daily_snapshot",
    "ingest_imerg_early_snapshot",
    "persist_imerg_snapshot",
    "read_gpm_imerg_rows",
]
