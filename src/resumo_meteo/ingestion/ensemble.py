"""All-member 00 UTC precipitation ingestion for the internal weather summary.

The module deliberately stops at compact, derived basin/window records. GRIB
messages are downloaded by byte range into a temporary directory and removed
before the result is returned or persisted. No ensemble is silently collapsed
to its mean: every output row retains the source member identifier.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from threading import RLock

import numpy as np

from resumo_meteo.core.basins import (
    DEFAULT_BASIN_REGISTRY,
    BasinRegistry,
    validate_canonical_basin_ids,
)
from resumo_meteo.core.constants import (
    ForecastModel,
    model_family,
    normalize_model_id,
    require_editorial_cycle,
)
from resumo_meteo.core.geometry_loader import (
    GeometryLoadError,
    cells_near_geometry,
    load_geometry_catalog,
)
from resumo_meteo.core.grib_fields import FieldSelector, read_selected_fields
from resumo_meteo.core.horizons import model_supports_window
from resumo_meteo.core.spatial import (
    BasinGeometry,
    BasinSpatialWeights,
    GridCell,
    aggregate_with_spatial_weights,
    compute_basin_spatial_weights,
    grid_cells_from_centers,
)
from resumo_meteo.core.summary_scope import WEATHER_SUMMARY_BASIN_IDS
from resumo_meteo.sources.catalog import (
    ECENS_PRODUCT_SLUG,
    GEFS_MEMBER_IDS,
    normalize_gefs_member_id,
)
from resumo_meteo.sources.ecmwf import (
    ECMWFOpenDataPrecipitationClient,
    select_ecmwf_index_records,
)
from resumo_meteo.sources.noaa import NOAAPrecipitationClient

SCHEMA_VERSION = "weather-summary-ensemble-precipitation.v1"
DEFAULT_OUTPUT_SUBDIR = "weather_summary_inputs/ensemble_precipitation"
HORIZON_HOURS = 336
WINDOW_LEAD_HOURS: tuple[tuple[str, int, int], ...] = (
    ("p1", 0, 120),
    ("p2", 120, 240),
    ("p3", 240, 336),
)
GEFS_DEACCUMULATION_METHOD = "native_interval_apcp"
ECENS_DEACCUMULATION_METHOD = "cumulative_difference"
ECENS_PACKING_NOISE_TOLERANCE_MM = 0.1
ECENS_MEMBER_IDS = tuple(str(number) for number in range(1, 51))
EXPECTED_MEMBER_IDS: dict[ForecastModel, tuple[str, ...]] = {
    ForecastModel.GEFS: GEFS_MEMBER_IDS,
    ForecastModel.ECENS: ECENS_MEMBER_IDS,
}
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SPATIAL_WEIGHT_CACHE_MAX_ENTRIES = 128
_SPATIAL_WEIGHT_CACHE: OrderedDict[
    tuple[str, str, str], BasinSpatialWeights
] = OrderedDict()
_SPATIAL_WEIGHT_CACHE_LOCK = RLock()


class EnsembleIngestionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


def _cached_spatial_weights(
    key: tuple[str, str, str],
    *,
    geometry: BasinGeometry,
    cells: Sequence[GridCell],
) -> BasinSpatialWeights:
    """Reuse immutable geometry/grid intersections across daily runs.

    Forecast values change every run, but the regular NOAA/ECMWF layouts and
    versioned basin polygons do not.  The bounded process-local cache avoids
    recomputing the expensive polygon intersections for every member/date.
    """

    with _SPATIAL_WEIGHT_CACHE_LOCK:
        cached = _SPATIAL_WEIGHT_CACHE.get(key)
        if cached is not None:
            _SPATIAL_WEIGHT_CACHE.move_to_end(key)
            return cached
    computed = compute_basin_spatial_weights(geometry, cells)
    with _SPATIAL_WEIGHT_CACHE_LOCK:
        existing = _SPATIAL_WEIGHT_CACHE.get(key)
        if existing is not None:
            _SPATIAL_WEIGHT_CACHE.move_to_end(key)
            return existing
        _SPATIAL_WEIGHT_CACHE[key] = computed
        while len(_SPATIAL_WEIGHT_CACHE) > _SPATIAL_WEIGHT_CACHE_MAX_ENTRIES:
            _SPATIAL_WEIGHT_CACHE.popitem(last=False)
    return computed


@dataclass(frozen=True, slots=True)
class DecodedPrecipitationGrid:
    longitudes: np.ndarray
    latitudes: np.ndarray
    values_mm: np.ndarray
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        longitudes = np.asarray(self.longitudes, dtype=np.float64).reshape(-1)
        latitudes = np.asarray(self.latitudes, dtype=np.float64).reshape(-1)
        values = np.asarray(self.values_mm, dtype=np.float64).reshape(-1)
        if not longitudes.size or not (
            longitudes.size == latitudes.size == values.size
        ):
            raise EnsembleIngestionError(
                "invalid_grid_shape",
                "Grade deve ter coordenadas e valores unidimensionais do mesmo tamanho",
            )
        if not np.all(np.isfinite(longitudes)) or not np.all(np.isfinite(latitudes)):
            raise EnsembleIngestionError(
                "invalid_grid_coordinates",
                "Grade contem coordenadas ausentes ou nao finitas",
            )
        object.__setattr__(self, "longitudes", longitudes)
        object.__setattr__(self, "latitudes", latitudes)
        object.__setattr__(self, "values_mm", values)
        object.__setattr__(
            self,
            "quality_flags",
            tuple(dict.fromkeys(str(flag) for flag in self.quality_flags if flag)),
        )


@dataclass(frozen=True, slots=True)
class MemberPrecipitationStep:
    model: str
    member_id: str
    start_lead_hour: int
    end_lead_hour: int
    grid: DecodedPrecipitationGrid
    source_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        model = normalize_model_id(self.model)
        if model not in {ForecastModel.GEFS, ForecastModel.ECENS}:
            raise ValueError("Somente GEFS e ECENS aceitam passos all-member")
        if self.end_lead_hour <= self.start_lead_hour:
            raise ValueError("Intervalo de precipitacao deve ter duracao positiva")
        if self.start_lead_hour < 0 or self.end_lead_hour > HORIZON_HOURS:
            raise ValueError("Passo de precipitacao fora do horizonte 0-336 h")
        finite_values = self.grid.values_mm[np.isfinite(self.grid.values_mm)]
        if finite_values.size and float(np.min(finite_values)) < -1e-6:
            raise EnsembleIngestionError(
                "negative_precipitation_increment",
                f"Incremento negativo para {model.value}/{self.member_id}",
            )
        object.__setattr__(self, "model", model.value)
        object.__setattr__(self, "start_lead_hour", int(self.start_lead_hour))
        object.__setattr__(self, "end_lead_hour", int(self.end_lead_hour))


@dataclass(frozen=True, slots=True)
class WindowPrecipitationGrid:
    model: str
    member_id: str
    window_id: str
    start_lead_hour: int
    end_lead_hour: int
    grid: DecodedPrecipitationGrid
    source_references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnsemblePrecipitationRow:
    model: str
    family: str
    member_id: str
    basin_id: str
    geometry_version: str
    window_id: str
    start_lead_hour: int
    end_lead_hour: int
    basin_mean_mm: float | None
    spatial_median_mm: float | None
    point_max_mm: float | None
    point_max_cell_id: str | None
    grid_coverage_fraction: float
    valid_data_coverage_fraction: float
    quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "family": self.family,
            "member_id": self.member_id,
            "basin_id": self.basin_id,
            "geometry_version": self.geometry_version,
            "window_id": self.window_id,
            "start_lead_hour": self.start_lead_hour,
            "end_lead_hour": self.end_lead_hour,
            "basin_mean_mm": self.basin_mean_mm,
            "spatial_median_mm": self.spatial_median_mm,
            "point_max_mm": self.point_max_mm,
            "point_max_cell_id": self.point_max_cell_id,
            "grid_coverage_fraction": self.grid_coverage_fraction,
            "valid_data_coverage_fraction": self.valid_data_coverage_fraction,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True, slots=True)
class ModelIngestionSummary:
    model: str
    family: str
    status: str
    expected_member_count: int
    available_member_count: int
    available_member_ids: tuple[str, ...]
    missing_member_ids: tuple[str, ...]
    failed_members: tuple[dict[str, str], ...]
    deaccumulation_method: str
    fetch_mode: str
    max_fetch_workers: int
    source_reference_count: int
    source_references_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "family": self.family,
            "status": self.status,
            "expected_member_count": self.expected_member_count,
            "available_member_count": self.available_member_count,
            "available_member_ids": list(self.available_member_ids),
            "missing_member_ids": list(self.missing_member_ids),
            "failed_members": list(self.failed_members),
            "deaccumulation_method": self.deaccumulation_method,
            "fetch_mode": self.fetch_mode,
            "max_fetch_workers": self.max_fetch_workers,
            "source_reference_count": self.source_reference_count,
            "source_references_sha256": self.source_references_sha256,
        }


@dataclass(frozen=True, slots=True)
class EnsembleIngestionResult:
    reference_date: date
    cycle: str
    generated_at_utc: str
    geometry_catalog: Mapping[str, object]
    models: Mapping[str, ModelIngestionSummary]
    rows: tuple[EnsemblePrecipitationRow, ...]
    quality_status: str
    quality_issues: tuple[dict[str, object], ...] = ()

    @property
    def init_time_utc(self) -> str:
        return datetime.combine(self.reference_date, datetime.min.time(), tzinfo=UTC).isoformat().replace(
            "+00:00", "Z"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "reference_date": self.reference_date.isoformat(),
            "cycle": self.cycle,
            "init_time_utc": self.init_time_utc,
            "generated_at_utc": self.generated_at_utc,
            "horizon_hours": HORIZON_HOURS,
            "windows": [
                {
                    "window_id": window_id,
                    "start_lead_hour": start,
                    "end_lead_hour": end,
                    "duration_days": (end - start) // 24,
                }
                for window_id, start, end in WINDOW_LEAD_HOURS
            ],
            "geometry_catalog": dict(self.geometry_catalog),
            "models": {
                model: summary.to_dict() for model, summary in self.models.items()
            },
            "rows": [row.to_dict() for row in self.rows],
            "quality": {
                "status": self.quality_status,
                "issues": list(self.quality_issues),
            },
        }


@dataclass(frozen=True, slots=True)
class PersistedEnsembleArtifacts:
    directory: str
    json_path: str
    csv_path: str
    manifest_path: str
    manifest_sha256: str
    files: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": self.directory,
            "json_path": self.json_path,
            "csv_path": self.csv_path,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "files": list(self.files),
        }


MemberFetcher = Callable[[date, str, str, Path], Iterable[MemberPrecipitationStep]]
MemberFetcherFactory = Callable[[], MemberFetcher]
GridReader = Callable[[Path], DecodedPrecipitationGrid]


def _default_grid_reader(path: Path) -> DecodedPrecipitationGrid:
    fields = read_selected_fields(
        path,
        selectors=(
            FieldSelector(
                key="total_precipitation",
                short_names=("tp", "apcp"),
                type_of_level="surface",
            ),
        ),
    )
    if len(fields) != 1:
        raise EnsembleIngestionError(
            "unexpected_grib_message_count",
            f"Esperada uma mensagem de precipitacao em {path.name}; recebidas {len(fields)}",
        )
    field = fields[0]
    return DecodedPrecipitationGrid(
        longitudes=field.longitudes,
        latitudes=field.latitudes,
        values_mm=field.values,
    )


def _same_grid(first: DecodedPrecipitationGrid, second: DecodedPrecipitationGrid) -> bool:
    return (
        first.values_mm.shape == second.values_mm.shape
        and np.allclose(first.longitudes, second.longitudes, rtol=0, atol=1e-9)
        and np.allclose(first.latitudes, second.latitudes, rtol=0, atol=1e-9)
    )


class GEFSMemberPrecipitationFetcher:
    """Fetch native 6-hour APCP ranges for one GEFS member."""

    def __init__(
        self,
        *,
        client: NOAAPrecipitationClient | None = None,
        grid_reader: GridReader = _default_grid_reader,
        interval_hours: int = 6,
        horizon_hours: int = HORIZON_HOURS,
    ):
        if interval_hours <= 0 or horizon_hours % interval_hours:
            raise ValueError("interval_hours deve dividir o horizonte exatamente")
        self.client = client or NOAAPrecipitationClient()
        self.grid_reader = grid_reader
        self.interval_hours = int(interval_hours)
        self.horizon_hours = int(horizon_hours)

    def close(self) -> None:
        close = getattr(getattr(self.client, "session", None), "close", None)
        if callable(close):
            close()

    def __call__(
        self,
        reference_date: date,
        cycle: str,
        member_id: str,
        work_dir: Path,
    ) -> tuple[MemberPrecipitationStep, ...]:
        cycle = require_editorial_cycle(cycle)
        member_id = normalize_gefs_member_id(member_id)
        steps: list[MemberPrecipitationStep] = []
        for end_hour in range(
            self.interval_hours,
            self.horizon_hours + 1,
            self.interval_hours,
        ):
            start_hour = end_hour - self.interval_hours
            selection = self.client.select_gefs_member_apcp_interval(
                reference_date,
                cycle=cycle,
                interval_start_hour=start_hour,
                interval_end_hour=end_hour,
                member_id=member_id,
            )
            path = work_dir / f"{member_id}-f{end_hour:03d}.grib2"
            try:
                self.client.download_selection(selection, path)
                grid = self.grid_reader(path)
            finally:
                path.unlink(missing_ok=True)
            steps.append(
                MemberPrecipitationStep(
                    model=ForecastModel.GEFS.value,
                    member_id=member_id,
                    start_lead_hour=start_hour,
                    end_lead_hour=end_hour,
                    grid=grid,
                    source_references=(selection.index_url, selection.grib_url),
                )
            )
        return tuple(steps)


class ECENSMemberPrecipitationFetcher:
    """Fetch one cumulative ``tp`` range per window and deaccumulate by member."""

    def __init__(
        self,
        *,
        client: ECMWFOpenDataPrecipitationClient | None = None,
        base_url: str | None = None,
        grid_reader: GridReader = _default_grid_reader,
        negative_tolerance_mm: float = ECENS_PACKING_NOISE_TOLERANCE_MM,
    ):
        if client is not None and base_url is not None:
            raise ValueError("Informe client ou base_url para ECENS, nao ambos")
        self.client = client or ECMWFOpenDataPrecipitationClient(base_url=base_url)
        self.grid_reader = grid_reader
        self.negative_tolerance_mm = float(negative_tolerance_mm)
        self._index_cache: dict[tuple[date, str, int], tuple] = {}

    def close(self) -> None:
        close = getattr(getattr(self.client, "session", None), "close", None)
        if callable(close):
            close()

    def _records(self, reference_date: date, cycle: str, forecast_hour: int):
        key = (reference_date, cycle, forecast_hour)
        if key not in self._index_cache:
            self._index_cache[key] = self.client.fetch_index(
                ECENS_PRODUCT_SLUG,
                reference_date,
                cycle=cycle,
                forecast_hour=forecast_hour,
            )
        return self._index_cache[key]

    def __call__(
        self,
        reference_date: date,
        cycle: str,
        member_id: str,
        work_dir: Path,
    ) -> tuple[MemberPrecipitationStep, ...]:
        cycle = require_editorial_cycle(cycle)
        try:
            member_number = int(member_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"member_id ECENS invalido: {member_id!r}") from exc
        if member_number not in range(1, 51):
            raise ValueError(f"member_id ECENS invalido: {member_id!r}")
        canonical_member_id = str(member_number)
        previous_grid: DecodedPrecipitationGrid | None = None
        previous_lead = 0
        steps: list[MemberPrecipitationStep] = []

        for _window_id, _start_hour, end_hour in WINDOW_LEAD_HOURS:
            records = select_ecmwf_index_records(
                self._records(reference_date, cycle, end_hour),
                param="tp",
                level_type="sfc",
                data_type={"pf", "ef"},
                stream="enfo",
                member_ids=(member_number,),
            )
            if len(records) != 1:
                raise EnsembleIngestionError(
                    "ecens_member_message_missing",
                    f"ECENS membro {canonical_member_id} tem {len(records)} mensagens tp em {end_hour} h",
                )
            grib_url = self.client.grib_url(
                ECENS_PRODUCT_SLUG,
                reference_date,
                cycle=cycle,
                forecast_hour=end_hour,
            )
            index_url = self.client.index_url(
                ECENS_PRODUCT_SLUG,
                reference_date,
                cycle=cycle,
                forecast_hour=end_hour,
            )
            path = work_dir / f"ecens-{canonical_member_id}-f{end_hour:03d}.grib2"
            try:
                self.client.download_messages(records, grib_url=grib_url, output_path=path)
                cumulative = self.grid_reader(path)
            finally:
                path.unlink(missing_ok=True)

            if previous_grid is not None and not _same_grid(previous_grid, cumulative):
                raise EnsembleIngestionError(
                    "ecens_grid_changed",
                    f"Grade ECENS mudou entre {previous_lead} e {end_hour} h",
                )
            if previous_grid is None:
                increment_values = cumulative.values_mm.copy()
            else:
                increment_values = cumulative.values_mm - previous_grid.values_mm
            finite = np.isfinite(increment_values)
            material_negative = finite & (
                increment_values < -self.negative_tolerance_mm
            )
            quality_flags: tuple[str, ...] = ()
            if material_negative.any():
                # ECMWF cumulative ``tp`` is regridded and independently packed
                # at every lead.  Local interpolation/packing artefacts can make
                # a small subset of cells non-monotonic even though the GRIB
                # range is explicitly 0..lead.  Invalidating the entire member
                # discards otherwise valid evidence for every canonical basin. Keep the
                # member, mark those cells unavailable and let area-weighted
                # basin coverage expose any material impact.
                increment_values = np.where(
                    material_negative,
                    np.nan,
                    increment_values,
                )
                quality_flags = ("negative_cumulative_cells_masked",)
                finite = np.isfinite(increment_values)
            increment_values = np.where(
                finite,
                np.maximum(increment_values, 0.0),
                np.nan,
            )
            steps.append(
                MemberPrecipitationStep(
                    model=ForecastModel.ECENS.value,
                    member_id=canonical_member_id,
                    start_lead_hour=previous_lead,
                    end_lead_hour=end_hour,
                    grid=DecodedPrecipitationGrid(
                        longitudes=cumulative.longitudes,
                        latitudes=cumulative.latitudes,
                        values_mm=increment_values,
                        quality_flags=quality_flags,
                    ),
                    source_references=(index_url, grib_url),
                )
            )
            previous_grid = cumulative
            previous_lead = end_hour
        return tuple(steps)


def accumulate_member_window_grids(
    steps: Iterable[MemberPrecipitationStep],
) -> tuple[WindowPrecipitationGrid, ...]:
    ordered = tuple(sorted(steps, key=lambda item: (item.start_lead_hour, item.end_lead_hour)))
    if not ordered:
        raise EnsembleIngestionError("member_without_steps", "Membro sem passos de precipitacao")
    model = normalize_model_id(ordered[0].model)
    member_id = ordered[0].member_id
    if any(item.model != model.value or item.member_id != member_id for item in ordered):
        raise EnsembleIngestionError(
            "mixed_member_steps",
            "Serie contem modelos ou membros diferentes",
        )

    results: list[WindowPrecipitationGrid] = []
    for window_id, start_hour, end_hour in WINDOW_LEAD_HOURS:
        if not model_supports_window(
            model,
            start_lead_hour=start_hour,
            end_lead_hour=end_hour,
        ):
            raise EnsembleIngestionError(
                "unsupported_model_window",
                f"{model.value} nao cobre {window_id} ate {end_hour} h",
            )
        window_steps = tuple(
            item
            for item in ordered
            if item.start_lead_hour >= start_hour and item.end_lead_hour <= end_hour
        )
        cursor = start_hour
        accumulated: np.ndarray | None = None
        base_grid: DecodedPrecipitationGrid | None = None
        references: list[str] = []
        quality_flags: list[str] = []
        for item in window_steps:
            if item.start_lead_hour != cursor:
                raise EnsembleIngestionError(
                    "incomplete_member_window",
                    f"{model.value}/{member_id}/{window_id} tem lacuna em {cursor} h",
                )
            if base_grid is None:
                base_grid = item.grid
                accumulated = np.zeros_like(item.grid.values_mm, dtype=np.float64)
            elif not _same_grid(base_grid, item.grid):
                raise EnsembleIngestionError(
                    "grid_changed_inside_window",
                    f"Grade mudou dentro de {model.value}/{member_id}/{window_id}",
                )
            accumulated += item.grid.values_mm
            references.extend(item.source_references)
            quality_flags.extend(item.grid.quality_flags)
            cursor = item.end_lead_hour
        if cursor != end_hour or base_grid is None or accumulated is None:
            raise EnsembleIngestionError(
                "incomplete_member_window",
                f"{model.value}/{member_id}/{window_id} termina em {cursor} h, esperado {end_hour} h",
            )
        results.append(
            WindowPrecipitationGrid(
                model=model.value,
                member_id=member_id,
                window_id=window_id,
                start_lead_hour=start_hour,
                end_lead_hour=end_hour,
                grid=DecodedPrecipitationGrid(
                    longitudes=base_grid.longitudes,
                    latitudes=base_grid.latitudes,
                    values_mm=accumulated,
                    quality_flags=tuple(dict.fromkeys(quality_flags)),
                ),
                source_references=tuple(references),
            )
        )
    return tuple(results)


def load_operational_basin_geometries(
    *,
    registry: BasinRegistry = DEFAULT_BASIN_REGISTRY,
    repository_root: str | Path | None = None,
) -> tuple[dict[str, BasinGeometry], tuple[dict[str, object], ...]]:
    try:
        catalog = load_geometry_catalog(
            registry=registry,
            repository_root=repository_root,
        )
    except GeometryLoadError as exc:
        return {}, (
            {
                "code": "basin_geometry_catalog_load_failed",
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            },
        )
    return dict(catalog.geometries), ()


@dataclass(frozen=True, slots=True)
class _GridLayout:
    signature: str
    cells: tuple[GridCell, ...]
    cell_ids_by_source_index: tuple[str, ...]

    def values_by_cell_id(self, values: np.ndarray) -> dict[str, float | None]:
        if len(values) != len(self.cell_ids_by_source_index):
            raise EnsembleIngestionError("grid_shape_changed", "Valores nao correspondem ao layout")
        return {
            cell_id: (float(value) if math.isfinite(float(value)) else None)
            for cell_id, value in zip(
                self.cell_ids_by_source_index, values, strict=True
            )
        }


def _grid_signature(grid: DecodedPrecipitationGrid) -> str:
    digest = hashlib.sha256()
    digest.update(np.asarray(sorted(set(grid.longitudes)), dtype=np.float64).tobytes())
    digest.update(np.asarray(sorted(set(grid.latitudes)), dtype=np.float64).tobytes())
    digest.update(grid.longitudes.tobytes())
    digest.update(grid.latitudes.tobytes())
    return digest.hexdigest()


def _grid_layout(
    grid: DecodedPrecipitationGrid,
    *,
    signature: str | None = None,
) -> _GridLayout:
    longitudes = tuple(sorted(set(float(value) for value in grid.longitudes)))
    latitudes = tuple(sorted(set(float(value) for value in grid.latitudes)))
    if len(longitudes) * len(latitudes) != grid.values_mm.size:
        raise EnsembleIngestionError(
            "non_rectangular_grid",
            "Grade recortada nao forma produto cartesiano completo",
        )
    lon_index = {value: index for index, value in enumerate(longitudes)}
    lat_index = {value: index for index, value in enumerate(latitudes)}
    seen: set[tuple[int, int]] = set()
    cell_ids: list[str] = []
    for lon, lat in zip(grid.longitudes, grid.latitudes, strict=True):
        position = (lat_index[float(lat)], lon_index[float(lon)])
        if position in seen:
            raise EnsembleIngestionError("duplicate_grid_point", "Grade contem ponto duplicado")
        seen.add(position)
        cell_ids.append(f"y{position[0]:04d}-x{position[1]:04d}")
    empty_values = tuple(tuple(0.0 for _lon in longitudes) for _lat in latitudes)
    cells = grid_cells_from_centers(longitudes, latitudes, empty_values)
    return _GridLayout(
        signature=signature or _grid_signature(grid),
        cells=cells,
        cell_ids_by_source_index=tuple(cell_ids),
    )


def _member_failure(member_id: str, exc: Exception) -> dict[str, str]:
    return {
        "member_id": member_id,
        "code": getattr(exc, "code", "member_ingestion_failed"),
        "message": str(exc),
        "error_type": exc.__class__.__name__,
    }


def _canonical_member_ids(
    model: ForecastModel,
    member_ids: Iterable[str],
) -> tuple[str, ...]:
    values: list[str] = []
    for raw_member_id in member_ids:
        if model == ForecastModel.GEFS:
            member_id = normalize_gefs_member_id(raw_member_id)
        else:
            try:
                member_number = int(raw_member_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"member_id ECENS invalido: {raw_member_id!r}") from exc
            if member_number not in range(1, 51):
                raise ValueError(f"member_id ECENS invalido: {raw_member_id!r}")
            member_id = str(member_number)
        values.append(member_id)
    if not values or len(values) != len(set(values)):
        raise ValueError(f"Membros invalidos/duplicados para {model.value}")
    return tuple(values)


def _references_digest(references: Iterable[str]) -> tuple[int, str]:
    values = tuple(sorted(set(str(value) for value in references if value)))
    payload = "\n".join(values).encode("utf-8")
    return len(values), hashlib.sha256(payload).hexdigest()


def _iter_members_concurrently(
    *,
    model: ForecastModel,
    reference_date: date,
    cycle: str,
    member_ids: Sequence[str],
    root: Path,
    fetcher_factory: MemberFetcherFactory,
    max_workers: int,
) -> Iterable[
    tuple[
        str,
        tuple[MemberPrecipitationStep, ...] | None,
        dict[str, str] | None,
    ]
]:

    def fetch_one(member_id: str) -> tuple[MemberPrecipitationStep, ...]:
        member_dir = root / model.value / member_id
        member_dir.mkdir(parents=True, exist_ok=True)
        fetcher = None
        try:
            fetcher = fetcher_factory()
            return tuple(fetcher(reference_date, cycle, member_id, member_dir))
        finally:
            close = getattr(fetcher, "close", None)
            if callable(close):
                close()
            shutil.rmtree(member_dir, ignore_errors=True)

    worker_count = min(max_workers, len(member_ids))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=f"weather-{model.value}",
    ) as executor:
        pending = {}
        next_index = 0
        while next_index < worker_count:
            member_id = member_ids[next_index]
            pending[member_id] = executor.submit(fetch_one, member_id)
            next_index += 1
        for member_id in member_ids:
            future = pending.pop(member_id)
            try:
                steps = future.result()
                failure = None
            except Exception as exc:
                steps = None
                failure = _member_failure(member_id, exc)
            if next_index < len(member_ids):
                next_member_id = member_ids[next_index]
                pending[next_member_id] = executor.submit(fetch_one, next_member_id)
                next_index += 1
            yield member_id, steps, failure


def ingest_all_member_precipitation(
    *,
    reference_date: date,
    cycle: str = "00",
    models: Sequence[str | ForecastModel] = (ForecastModel.GEFS, ForecastModel.ECENS),
    geometries: Mapping[str, BasinGeometry] | None = None,
    geometry_issues: Iterable[Mapping[str, object]] = (),
    required_basin_ids: Sequence[str] = WEATHER_SUMMARY_BASIN_IDS,
    member_fetchers: Mapping[str | ForecastModel, MemberFetcher] | None = None,
    gefs_fetcher_factory: MemberFetcherFactory | None = None,
    ecens_fetcher_factory: MemberFetcherFactory | None = None,
    gefs_max_workers: int = 3,
    ecens_max_workers: int = 4,
    ecmwf_base_url: str | None = None,
    member_ids_by_model: Mapping[str | ForecastModel, Sequence[str]] | None = None,
    temporary_root: str | Path | None = None,
    minimum_valid_coverage_fraction: float = 0.95,
    generated_at_utc: datetime | None = None,
) -> EnsembleIngestionResult:
    if isinstance(reference_date, datetime) or not isinstance(reference_date, date):
        raise TypeError("reference_date deve ser date")
    cycle = require_editorial_cycle(cycle)
    selected_models = tuple(normalize_model_id(model) for model in models)
    if not selected_models:
        raise ValueError("models deve conter GEFS e/ou ECENS")
    if len(set(selected_models)) != len(selected_models):
        raise ValueError("models contem duplicatas")
    if any(model not in EXPECTED_MEMBER_IDS for model in selected_models):
        raise ValueError("Ingestao all-member aceita somente GEFS e ECENS")
    basin_ids = validate_canonical_basin_ids(required_basin_ids)
    if isinstance(gefs_max_workers, bool) or not 1 <= int(gefs_max_workers) <= 16:
        raise ValueError("gefs_max_workers deve estar entre 1 e 16")
    gefs_max_workers = int(gefs_max_workers)
    if isinstance(ecens_max_workers, bool) or not 1 <= int(ecens_max_workers) <= 8:
        raise ValueError("ecens_max_workers deve estar entre 1 e 8")
    ecens_max_workers = int(ecens_max_workers)

    issues: list[dict[str, object]] = [dict(item) for item in geometry_issues]
    if geometries is None:
        loaded_geometries, loaded_issues = load_operational_basin_geometries()
        geometries = loaded_geometries
        issues.extend(loaded_issues)
    geometry_map = dict(geometries)
    for basin_id, geometry in geometry_map.items():
        if basin_id != geometry.basin_id:
            raise ValueError(f"Chave de geometria divergente: {basin_id}/{geometry.basin_id}")
    missing_basin_ids = tuple(basin_id for basin_id in basin_ids if basin_id not in geometry_map)
    existing_missing_issue_ids = {
        str(item.get("basin_id"))
        for item in issues
        if item.get("code") == "missing_basin_geometry"
    }
    for basin_id in missing_basin_ids:
        if basin_id not in existing_missing_issue_ids:
            issues.append(
                {
                    "code": "missing_basin_geometry",
                    "basin_id": basin_id,
                    "message": "Bacia sem geometria operacional; nenhuma linha foi produzida.",
                }
            )

    default_fetchers: dict[ForecastModel, MemberFetcher] = {}
    injected_fetcher_models: set[ForecastModel] = set()
    if member_fetchers:
        for model, fetcher in member_fetchers.items():
            canonical_model = normalize_model_id(model)
            if canonical_model not in EXPECTED_MEMBER_IDS:
                raise ValueError("member_fetchers aceita somente GEFS e ECENS")
            default_fetchers[canonical_model] = fetcher
            injected_fetcher_models.add(canonical_model)
    if (
        ForecastModel.ECENS in selected_models
        and ForecastModel.ECENS not in default_fetchers
        and ecens_max_workers == 1
    ):
        default_fetchers[ForecastModel.ECENS] = ECENSMemberPrecipitationFetcher(
            base_url=ecmwf_base_url
        )
    if ForecastModel.GEFS not in default_fetchers and gefs_max_workers == 1:
        default_fetchers[ForecastModel.GEFS] = (
            gefs_fetcher_factory or GEFSMemberPrecipitationFetcher
        )()

    configured_member_ids: dict[ForecastModel, tuple[str, ...]] = dict(EXPECTED_MEMBER_IDS)
    if member_ids_by_model:
        for raw_model, raw_member_ids in member_ids_by_model.items():
            model = normalize_model_id(raw_model)
            if model not in EXPECTED_MEMBER_IDS:
                raise ValueError("member_ids_by_model aceita somente GEFS e ECENS")
            configured_member_ids[model] = _canonical_member_ids(model, raw_member_ids)

    layouts: dict[str, _GridLayout] = {}
    weights: dict[tuple[str, str, str], BasinSpatialWeights] = {}
    rows: list[EnsemblePrecipitationRow] = []
    model_summaries: dict[str, ModelIngestionSummary] = {}

    temporary_parent = Path(temporary_root).resolve() if temporary_root else None
    if temporary_parent is not None:
        temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="weather-summary-ensemble-",
        dir=temporary_parent,
    ) as temporary_directory:
        root = Path(temporary_directory)
        for model in selected_models:
            expected_ids = configured_member_ids[model]
            available_ids: list[str] = []
            failures: list[dict[str, str]] = []
            model_references: list[str] = []
            fetcher = default_fetchers.get(model)
            use_concurrent_gefs = (
                model == ForecastModel.GEFS
                and model not in injected_fetcher_models
                and gefs_max_workers > 1
            )
            use_concurrent_ecens = (
                model == ForecastModel.ECENS
                and model not in injected_fetcher_models
                and ecens_max_workers > 1
            )
            use_concurrent_members = use_concurrent_gefs or use_concurrent_ecens
            concurrent_results = None
            if use_concurrent_members:
                factory: MemberFetcherFactory
                max_workers: int
                if use_concurrent_gefs:
                    factory = gefs_fetcher_factory or GEFSMemberPrecipitationFetcher
                    max_workers = gefs_max_workers
                else:
                    # Each worker owns its requests session.  ECMWF indexes are
                    # tiny compared with GRIB messages; avoiding shared sessions
                    # makes the bounded parallel download safe and deterministic.
                    if ecens_fetcher_factory is not None:
                        factory = ecens_fetcher_factory
                    else:
                        def factory() -> ECENSMemberPrecipitationFetcher:
                            return ECENSMemberPrecipitationFetcher(base_url=ecmwf_base_url)

                    max_workers = ecens_max_workers
                concurrent_results = iter(
                    _iter_members_concurrently(
                        model=model,
                        reference_date=reference_date,
                        cycle=cycle,
                        member_ids=expected_ids,
                        root=root,
                        fetcher_factory=factory,
                        max_workers=max_workers,
                    )
                )

            for member_id in expected_ids:
                member_dir = root / model.value / member_id
                if not use_concurrent_members:
                    member_dir.mkdir(parents=True, exist_ok=True)
                try:
                    if use_concurrent_members:
                        fetched_member_id, member_steps, failure = next(  # type: ignore[arg-type]
                            concurrent_results
                        )
                        if fetched_member_id != member_id:
                            raise RuntimeError("Ordem concorrente GEFS divergente")
                    else:
                        failure = None
                        member_steps = tuple(
                            fetcher(reference_date, cycle, member_id, member_dir)  # type: ignore[misc]
                        )
                    if failure is not None:
                        failures.append(failure)
                        issues.append(
                            {
                                "code": failure["code"],
                                "model": model.value,
                                "member_id": member_id,
                                "error_type": failure["error_type"],
                                "message": failure["message"],
                            }
                        )
                        continue
                    if member_steps is None:
                        raise RuntimeError("Membro concorrente sem passos nem falha")
                    window_grids = accumulate_member_window_grids(member_steps)
                    for window_grid in window_grids:
                        signature = _grid_signature(window_grid.grid)
                        cached_layout = layouts.get(signature)
                        if cached_layout is None:
                            cached_layout = _grid_layout(
                                window_grid.grid,
                                signature=signature,
                            )
                            layouts[signature] = cached_layout
                        values_by_cell = cached_layout.values_by_cell_id(
                            window_grid.grid.values_mm
                        )
                        for basin_id in basin_ids:
                            geometry = geometry_map.get(basin_id)
                            if geometry is None:
                                continue
                            weight_key = (
                                cached_layout.signature,
                                basin_id,
                                geometry.geometry_version,
                            )
                            if weight_key not in weights:
                                nearby_cells = cells_near_geometry(
                                    geometry,
                                    cached_layout.cells,
                                )
                                weights[weight_key] = _cached_spatial_weights(
                                    weight_key,
                                    geometry=geometry,
                                    cells=nearby_cells,
                                )
                            aggregation = aggregate_with_spatial_weights(
                                weights[weight_key],
                                values_by_cell,
                                minimum_valid_coverage_fraction=(
                                    minimum_valid_coverage_fraction
                                ),
                            )
                            row_quality_flags = list(aggregation.quality_flags)
                            if (
                                "negative_cumulative_cells_masked"
                                in window_grid.grid.quality_flags
                                and aggregation.valid_data_coverage_fraction
                                < aggregation.grid_coverage_fraction - 1e-9
                            ):
                                row_quality_flags.append(
                                    "negative_cumulative_cells_masked"
                                )
                            rows.append(
                                EnsemblePrecipitationRow(
                                    model=model.value,
                                    family=model_family(model).value,
                                    member_id=member_id,
                                    basin_id=basin_id,
                                    geometry_version=geometry.geometry_version,
                                    window_id=window_grid.window_id,
                                    start_lead_hour=window_grid.start_lead_hour,
                                    end_lead_hour=window_grid.end_lead_hour,
                                    basin_mean_mm=aggregation.basin_mean_mm,
                                    spatial_median_mm=aggregation.spatial_median_mm,
                                    point_max_mm=aggregation.point_max_mm,
                                    point_max_cell_id=aggregation.point_max_cell_id,
                                    grid_coverage_fraction=(
                                        aggregation.grid_coverage_fraction
                                    ),
                                    valid_data_coverage_fraction=(
                                        aggregation.valid_data_coverage_fraction
                                    ),
                                    quality_flags=tuple(
                                        dict.fromkeys(row_quality_flags)
                                    ),
                                )
                            )
                    available_ids.append(member_id)
                    model_references.extend(
                        reference
                        for step in member_steps
                        for reference in step.source_references
                    )
                except Exception as exc:
                    failure = _member_failure(member_id, exc)
                    failures.append(failure)
                    issues.append(
                        {
                            "code": failure["code"],
                            "model": model.value,
                            "member_id": member_id,
                            "error_type": failure["error_type"],
                            "message": failure["message"],
                        }
                    )
                finally:
                    if not use_concurrent_members:
                        shutil.rmtree(member_dir, ignore_errors=True)

            if use_concurrent_members:
                next(concurrent_results, None)  # type: ignore[arg-type]

            if model not in injected_fetcher_models and not use_concurrent_gefs:
                close = getattr(fetcher, "close", None)
                if callable(close):
                    close()

            missing_ids = tuple(
                member_id for member_id in expected_ids if member_id not in available_ids
            )
            status = (
                "complete"
                if len(available_ids) == len(expected_ids)
                else "partial" if available_ids else "failed"
            )
            reference_count, reference_digest = _references_digest(model_references)
            method = (
                GEFS_DEACCUMULATION_METHOD
                if model == ForecastModel.GEFS
                else ECENS_DEACCUMULATION_METHOD
            )
            model_summaries[model.value] = ModelIngestionSummary(
                model=model.value,
                family=model_family(model).value,
                status=status,
                expected_member_count=len(expected_ids),
                available_member_count=len(available_ids),
                available_member_ids=tuple(available_ids),
                missing_member_ids=missing_ids,
                failed_members=tuple(failures),
                deaccumulation_method=method,
                fetch_mode=(
                    "limited_concurrent"
                    if use_concurrent_gefs
                    else (
                        "synchronous_cached_index"
                        if model == ForecastModel.ECENS
                        else "synchronous"
                    )
                ),
                max_fetch_workers=(
                    min(gefs_max_workers, len(expected_ids))
                    if use_concurrent_gefs
                    else 1
                ),
                source_reference_count=reference_count,
                source_references_sha256=reference_digest,
            )

    quality_status = "complete"
    if issues or any(summary.status != "complete" for summary in model_summaries.values()):
        quality_status = "partial"
    if not rows:
        quality_status = "failed"
    generated = generated_at_utc or datetime.now(UTC)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at_utc deve ter timezone explicito")
    generated = generated.astimezone(UTC)
    return EnsembleIngestionResult(
        reference_date=reference_date,
        cycle=cycle,
        generated_at_utc=generated.isoformat().replace("+00:00", "Z"),
        geometry_catalog={
            "catalog_id": DEFAULT_BASIN_REGISTRY.catalog_id,
            "catalog_version": DEFAULT_BASIN_REGISTRY.catalog_version,
            "crs": DEFAULT_BASIN_REGISTRY.crs,
            "geometry_version": next(
                (
                    geometry_map[basin_id].geometry_version
                    for basin_id in basin_ids
                    if basin_id in geometry_map
                ),
                "",
            ),
            "requested_basin_ids": list(basin_ids),
            "available_basin_ids": [
                basin_id for basin_id in basin_ids if basin_id in geometry_map
            ],
            "missing_basin_ids": list(missing_basin_ids),
        },
        models=model_summaries,
        rows=tuple(rows),
        quality_status=quality_status,
        quality_issues=tuple(issues),
    )


def _relative_to_media(path: Path, media_root: Path) -> str:
    return path.resolve().relative_to(media_root.resolve()).as_posix()


def _file_entry(path: Path, media_root: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "name": path.name,
        "path": _relative_to_media(path, media_root),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _csv_gzip_bytes(rows: Iterable[EnsemblePrecipitationRow]) -> bytes:
    columns = tuple(EnsemblePrecipitationRow.__dataclass_fields__)
    binary = io.BytesIO()
    with gzip.GzipFile(fileobj=binary, mode="wb", mtime=0) as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
            writer = csv.DictWriter(text, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                payload = row.to_dict()
                payload["quality_flags"] = "|".join(row.quality_flags)
                writer.writerow(payload)
    return binary.getvalue()


def persist_ensemble_ingestion(
    result: EnsembleIngestionResult,
    *,
    media_root: str | Path,
    run_id: str,
    output_subdir: str = DEFAULT_OUTPUT_SUBDIR,
) -> PersistedEnsembleArtifacts:
    if not _SAFE_RUN_ID.fullmatch(str(run_id or "")):
        raise ValueError("run_id deve ser identificador seguro")
    root = Path(media_root).resolve()
    subdir = Path(str(output_subdir or ""))
    if subdir.is_absolute() or not subdir.parts or ".." in subdir.parts:
        raise ValueError("output_subdir deve ser relativo e seguro")
    parent = (
        root
        / subdir
        / result.reference_date.strftime("%Y/%m/%d")
        / f"{result.cycle}z"
    )
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / run_id
    if target.exists():
        raise FileExistsError(f"Ingestao ensemble ja existe: {target}")
    staging_root = root / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".tmp", dir=staging_root))
    try:
        json_path = temporary / "ensemble_precipitation.json"
        json_path.write_bytes(
            json.dumps(
                result.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        csv_path = temporary / "ensemble_precipitation.csv.gz"
        csv_path.write_bytes(_csv_gzip_bytes(result.rows))
        derived_entries = (
            _file_entry(json_path, root),
            _file_entry(csv_path, root),
        )
        for entry in derived_entries:
            entry["path"] = _relative_to_media(target / str(entry["name"]), root)
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(
            json.dumps(
                {
                    "schema_version": "weather-summary-ensemble-artifacts.v1",
                    "ensemble_schema_version": SCHEMA_VERSION,
                    "reference_date": result.reference_date.isoformat(),
                    "cycle": result.cycle,
                    "run_id": run_id,
                    "generated_at_utc": result.generated_at_utc,
                    "raw_grib_persisted": False,
                    "files": list(derived_entries),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    json_final = target / json_path.name
    csv_final = target / csv_path.name
    manifest_final = target / manifest_path.name
    file_entries = (
        _file_entry(json_final, root),
        _file_entry(csv_final, root),
        _file_entry(manifest_final, root),
    )
    return PersistedEnsembleArtifacts(
        directory=_relative_to_media(target, root),
        json_path=_relative_to_media(json_final, root),
        csv_path=_relative_to_media(csv_final, root),
        manifest_path=_relative_to_media(manifest_final, root),
        manifest_sha256=str(file_entries[-1]["sha256"]),
        files=file_entries,
    )


def build_weather_summary_ensemble_metadata(
    result: EnsembleIngestionResult,
    artifacts: PersistedEnsembleArtifacts,
    *,
    model: str | ForecastModel,
) -> dict[str, object]:
    canonical = normalize_model_id(model)
    try:
        summary = result.models[canonical.value]
    except KeyError as exc:
        raise ValueError(f"Modelo ausente do resultado: {canonical.value}") from exc
    status = summary.status
    if status == "complete" and result.quality_status != "complete":
        status = "partial"
    return {
        "schema_version": SCHEMA_VERSION,
        "model": canonical.value,
        "status": status,
        "quality_status": result.quality_status,
        "cycle": result.cycle,
        "reference_date": result.reference_date.isoformat(),
        "member_count": summary.available_member_count,
        "expected_member_count": summary.expected_member_count,
        "member_ids": list(summary.available_member_ids),
        "missing_member_ids": list(summary.missing_member_ids),
        "failed_members": list(summary.failed_members),
        "fetch_mode": summary.fetch_mode,
        "max_fetch_workers": summary.max_fetch_workers,
        "missing_basin_ids": list(
            result.geometry_catalog.get("missing_basin_ids") or []
        ),
        "requested_basin_ids": list(
            result.geometry_catalog.get("requested_basin_ids") or []
        ),
        "geometry_version": str(result.geometry_catalog.get("geometry_version") or ""),
        "quality_issues": list(result.quality_issues),
        "json_path": artifacts.json_path,
        "csv_path": artifacts.csv_path,
        "manifest_path": artifacts.manifest_path,
        "manifest_sha256": artifacts.manifest_sha256,
    }
