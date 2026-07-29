"""Strict readers for normalized precipitation inputs used by the packet builder.

Only normalized, persisted artefacts are accepted here.  Raw GRIB/HDF files
belong to ingestion and are deliberately outside the editorial boundary.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .spatial import GridCell


class GridInputError(ValueError):
    """A normalized input is unsafe or violates its declared contract."""


def _number(value: object, *, field: str, non_negative: bool = False) -> float:
    if isinstance(value, bool):
        raise GridInputError(f"{field} deve ser numerico")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise GridInputError(f"{field} deve ser numerico") from exc
    if not math.isfinite(result):
        raise GridInputError(f"{field} deve ser finito")
    if non_negative and result < 0:
        raise GridInputError(f"{field} nao pode ser negativo")
    return result


def _date(value: object, *, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise GridInputError(f"{field} deve ser uma data ISO") from exc
    return parsed


def _cell_id(longitude: float, latitude: float) -> str:
    return f"lon={longitude:.8f};lat={latitude:.8f}"


def _axis_bounds(values: Sequence[float], *, default_spacing: float) -> dict[float, tuple[float, float]]:
    ordered = tuple(sorted(set(values)))
    if not ordered:
        raise GridInputError("grade sem coordenadas")
    if len(ordered) == 1:
        if not math.isfinite(default_spacing) or default_spacing <= 0:
            raise GridInputError("grade de um ponto requer espacamento positivo")
        half = default_spacing / 2.0
        return {ordered[0]: (ordered[0] - half, ordered[0] + half)}
    if any(current <= previous for previous, current in zip(ordered, ordered[1:])):
        raise GridInputError("eixo de grade nao e estritamente crescente")
    midpoints = tuple((left + right) / 2.0 for left, right in zip(ordered, ordered[1:]))
    edges = (
        ordered[0] - (midpoints[0] - ordered[0]),
        *midpoints,
        ordered[-1] + (ordered[-1] - midpoints[-1]),
    )
    return {value: (edges[index], edges[index + 1]) for index, value in enumerate(ordered)}


@dataclass(frozen=True, slots=True)
class DailyGrid:
    valid_date: date
    cells: tuple[GridCell, ...]
    source_path: str

    @property
    def by_id(self) -> dict[str, GridCell]:
        return {cell.cell_id: cell for cell in self.cells}


@dataclass(frozen=True, slots=True)
class AccumulatedGrid:
    cells: tuple[GridCell, ...]
    valid_days: int
    expected_days: int
    missing_dates: tuple[date, ...]

    @property
    def complete(self) -> bool:
        return self.valid_days == self.expected_days and not self.missing_dates


@dataclass(frozen=True, slots=True)
class EnsembleBasinRow:
    model: str
    family: str
    member_id: str
    basin_id: str
    geometry_version: str
    window_id: str
    start_lead_hour: int
    end_lead_hour: int
    basin_mean_mm: float
    spatial_median_mm: float
    point_max_mm: float
    point_max_cell_id: str | None
    grid_coverage_fraction: float
    valid_data_coverage_fraction: float
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EnsembleInput:
    schema_version: str
    model: str
    member_count: int
    expected_member_count: int
    member_ids: tuple[str, ...]
    rows: tuple[EnsembleBasinRow, ...]
    json_path: str


def resolve_verified_artifact_file(
    contract: Mapping[str, object],
    *,
    repository,
    manifest_schema_version: str,
    path_field: str = "json_path",
) -> Path:
    """Resolve one derived artefact through its checksummed manifest.

    A checksum copied into metadata is not trusted by itself.  We first verify
    the manifest bytes against ``manifest_sha256`` and then verify the selected
    file against the checksum and size declared inside that manifest.
    """

    relative_path = str(contract.get(path_field) or "").strip()
    manifest_path = str(contract.get("manifest_path") or "").strip()
    manifest_sha256 = str(contract.get("manifest_sha256") or "").strip()
    if not relative_path or not manifest_path:
        raise GridInputError(f"contrato sem {path_field}/manifest_path")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise GridInputError("contrato sem checksum SHA-256 valido do manifesto")
    resolved_manifest = repository.resolve_media_file(
        manifest_path,
        expected_sha256=manifest_sha256,
    )
    try:
        manifest_bytes = resolved_manifest.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GridInputError(f"manifesto de artefatos invalido: {resolved_manifest}") from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        # Keep this explicit even when the repository already verifies hashes;
        # injected repositories used by batch/backfill code must obey it too.
        raise GridInputError("checksum do manifesto diverge dos bytes lidos")
    if not isinstance(manifest, dict) or str(manifest.get("schema_version") or "") != str(
        manifest_schema_version
    ):
        raise GridInputError("schema do manifesto de artefatos divergente")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise GridInputError("manifesto de artefatos sem lista files")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("path") or "") == relative_path
    ]
    if len(matches) != 1:
        raise GridInputError(f"{path_field} nao possui entrada unica no manifesto")
    entry = matches[0]
    file_sha256 = str(entry.get("sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", file_sha256):
        raise GridInputError(f"entrada {path_field} sem SHA-256 valido")
    try:
        expected_size = int(entry.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise GridInputError(f"entrada {path_field} sem size_bytes inteiro") from exc
    if expected_size < 0:
        raise GridInputError(f"entrada {path_field} com size_bytes negativo")
    resolved = repository.resolve_media_file(
        relative_path,
        expected_sha256=file_sha256,
    )
    try:
        actual_size = resolved.stat().st_size
        with open(resolved, "rb") as file_obj:
            actual_sha256 = hashlib.file_digest(file_obj, "sha256").hexdigest()
    except OSError as exc:
        raise GridInputError(f"nao foi possivel verificar artefato: {resolved}") from exc
    if actual_size != expected_size or actual_sha256 != file_sha256:
        raise GridInputError(f"bytes de {path_field} divergem do manifesto")
    return resolved


def read_daily_grid(
    path: str | Path,
    *,
    valid_date: date | str | None = None,
    default_spacing: float = 0.1,
) -> DailyGrid:
    """Read one persisted ``csv.gz`` grid and reconstruct native cells."""

    resolved = Path(path)
    opener = gzip.open if resolved.suffix == ".gz" else open
    rows: list[tuple[float, float, float, date]] = []
    try:
        with opener(resolved, "rt", encoding="utf-8", newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            required = {"forecast_date", "longitude", "latitude", "precipitation_mm"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise GridInputError(
                    f"grade {resolved} sem colunas obrigatorias {sorted(required)}"
                )
            for index, row in enumerate(reader, start=2):
                try:
                    longitude = _number(row.get("longitude"), field=f"longitude linha {index}")
                    latitude = _number(row.get("latitude"), field=f"latitude linha {index}")
                    precipitation = _number(
                        row.get("precipitation_mm"),
                        field=f"precipitation_mm linha {index}",
                        non_negative=True,
                    )
                    row_date = _date(row.get("forecast_date"), field=f"forecast_date linha {index}")
                except GridInputError:
                    raise
                if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                    raise GridInputError(f"coordenada fora do dominio na linha {index}")
                rows.append((longitude, latitude, precipitation, row_date))
    except (OSError, csv.Error) as exc:
        raise GridInputError(f"nao foi possivel ler grade normalizada {resolved}") from exc

    if not rows:
        raise GridInputError(f"grade normalizada vazia: {resolved}")
    declared_date = _date(valid_date, field="valid_date") if valid_date is not None else rows[0][3]
    row_dates = {row[3] for row in rows}
    if row_dates != {declared_date}:
        raise GridInputError(
            f"grade {resolved} mistura datas ou diverge da data declarada: "
            f"{sorted(item.isoformat() for item in row_dates)}"
        )

    longitude_bounds = _axis_bounds([row[0] for row in rows], default_spacing=default_spacing)
    latitude_bounds = _axis_bounds([row[1] for row in rows], default_spacing=default_spacing)
    cells: list[GridCell] = []
    seen: set[str] = set()
    for longitude, latitude, precipitation, _row_date in rows:
        cell_id = _cell_id(longitude, latitude)
        if cell_id in seen:
            raise GridInputError(f"celula duplicada em {resolved}: {cell_id}")
        seen.add(cell_id)
        west, east = longitude_bounds[longitude]
        south, north = latitude_bounds[latitude]
        cells.append(
            GridCell(
                cell_id=cell_id,
                west=west,
                east=east,
                south=south,
                north=north,
                value_mm=precipitation,
            )
        )
    return DailyGrid(valid_date=declared_date, cells=tuple(cells), source_path=str(resolved))


def load_daily_grids(
    file_metadata: Iterable[Mapping[str, object]],
    *,
    repository,
) -> tuple[DailyGrid, ...]:
    """Resolve and read normalized files declared by an artifact record."""

    grids: list[DailyGrid] = []
    for item in file_metadata:
        relative_path = str(item.get("path") or "").strip()
        if not relative_path:
            raise GridInputError("manifesto de grade sem path")
        forecast_date = item.get("forecast_date") or item.get("observation_date")
        resolved = repository.resolve_media_file(
            relative_path,
            expected_sha256=str(item.get("sha256") or ""),
        )
        grids.append(read_daily_grid(resolved, valid_date=forecast_date))
    by_date: dict[date, DailyGrid] = {}
    for grid in grids:
        if grid.valid_date in by_date:
            raise GridInputError(f"mais de uma grade para {grid.valid_date.isoformat()}")
        by_date[grid.valid_date] = grid
    return tuple(by_date[key] for key in sorted(by_date))


def accumulate_daily_grids(
    grids: Iterable[DailyGrid],
    *,
    expected_dates: Iterable[date],
) -> AccumulatedGrid:
    """Accumulate only cells present on every required day; never fill gaps with zero."""

    expected = tuple(expected_dates)
    if not expected or len(set(expected)) != len(expected):
        raise GridInputError("expected_dates deve conter datas unicas")
    by_date = {grid.valid_date: grid for grid in grids if grid.valid_date in set(expected)}
    missing = tuple(day for day in expected if day not in by_date)
    selected = tuple(by_date[day] for day in expected if day in by_date)
    if not selected:
        return AccumulatedGrid((), 0, len(expected), missing)

    cell_maps = [grid.by_id for grid in selected]
    common_ids = set(cell_maps[0])
    for values in cell_maps[1:]:
        common_ids.intersection_update(values)
    cells: list[GridCell] = []
    for cell_id in sorted(common_ids):
        originals = [values[cell_id] for values in cell_maps]
        bounds = {(cell.west, cell.east, cell.south, cell.north) for cell in originals}
        if len(bounds) != 1:
            raise GridInputError(f"geometria da celula mudou dentro da janela: {cell_id}")
        first = originals[0]
        values = [cell.value_mm for cell in originals]
        if any(value is None for value in values):
            value_mm = None
        else:
            value_mm = sum(float(value) for value in values if value is not None)
        cells.append(
            GridCell(
                cell_id=cell_id,
                west=first.west,
                east=first.east,
                south=first.south,
                north=first.north,
                value_mm=value_mm,
            )
        )
    return AccumulatedGrid(
        cells=tuple(cells),
        valid_days=len(selected),
        expected_days=len(expected),
        missing_dates=missing,
    )


def load_ensemble_input(record, *, repository) -> EnsembleInput | None:
    """Load the versioned all-member basin/window JSON produced at ingestion."""

    processed = (
        record.product_data.processed_data
        if isinstance(record.product_data.processed_data, dict)
        else {}
    )
    contract = processed.get("weather_summary_ensemble")
    if not isinstance(contract, dict):
        return None
    if str(contract.get("status") or "") == "failed":
        return None
    model = str(contract.get("model") or "").lower()
    if model not in {"gefs", "ecens"} or model != record.model_key:
        raise GridInputError("contrato ensemble aponta para modelo divergente")
    if str(contract.get("cycle") or "") != "00":
        raise GridInputError("contrato ensemble deve usar exclusivamente ciclo 00")
    json_path = str(contract.get("json_path") or "").strip()
    if not json_path:
        raise GridInputError("contrato ensemble sem json_path")
    resolved = resolve_verified_artifact_file(
        contract,
        repository=repository,
        manifest_schema_version="weather-summary-ensemble-artifacts.v1",
    )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GridInputError(f"JSON ensemble invalido: {resolved}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise GridInputError("JSON ensemble deve conter lista rows")
    schema_version = str(
        payload.get("schema_version")
        or contract.get("schema_version")
        or ""
    )
    if schema_version != "weather-summary-ensemble-precipitation.v1":
        raise GridInputError(f"schema ensemble nao suportado: {schema_version!r}")

    canonical_expected_count = 31 if model == "gefs" else 50
    expected_count = int(contract.get("expected_member_count") or canonical_expected_count)
    if expected_count != canonical_expected_count:
        raise GridInputError(
            f"{model} deve declarar exatamente {canonical_expected_count} membros esperados"
        )
    declared_member_ids = tuple(str(value) for value in (contract.get("member_ids") or ()))
    if not declared_member_ids or len(set(declared_member_ids)) != len(declared_member_ids):
        raise GridInputError("contrato ensemble sem member_ids unicos")

    payload_models = payload.get("models")
    if not isinstance(payload_models, dict) or not isinstance(payload_models.get(model), dict):
        raise GridInputError("JSON ensemble sem resumo do modelo selecionado")
    model_summary = payload_models[model]
    summary_member_ids = tuple(
        str(value) for value in (model_summary.get("available_member_ids") or ())
    )
    if summary_member_ids != declared_member_ids:
        raise GridInputError("member_ids divergem entre metadata e resumo do JSON")
    try:
        summary_expected_count = int(model_summary.get("expected_member_count"))
        summary_available_count = int(model_summary.get("available_member_count"))
    except (TypeError, ValueError) as exc:
        raise GridInputError("resumo do modelo sem contagens inteiras") from exc
    if (
        summary_expected_count != expected_count
        or summary_available_count != len(declared_member_ids)
    ):
        raise GridInputError("contagens do resumo ensemble sao inconsistentes")

    geometry_catalog = payload.get("geometry_catalog")
    if not isinstance(geometry_catalog, dict):
        raise GridInputError("JSON ensemble sem geometry_catalog")
    available_basin_ids = tuple(
        str(value) for value in (geometry_catalog.get("available_basin_ids") or ())
    )
    if not available_basin_ids or len(set(available_basin_ids)) != len(available_basin_ids):
        raise GridInputError("geometry_catalog sem available_basin_ids unicos")
    rows: list[EnsembleBasinRow] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(payload["rows"]):
        if not isinstance(raw, dict):
            raise GridInputError(f"row ensemble {index} nao e objeto")
        row_model = str(raw.get("model") or "").lower()
        family = str(raw.get("family") or "").lower()
        member_id = str(raw.get("member_id") or "").strip()
        basin_id = str(raw.get("basin_id") or "").strip()
        geometry_version = str(raw.get("geometry_version") or "").strip()
        window_id = str(raw.get("window_id") or "").strip().lower()
        if row_model != model or family != record.family:
            raise GridInputError(f"row ensemble {index} mistura modelo/familia")
        if (
            not member_id
            or member_id not in declared_member_ids
            or basin_id not in available_basin_ids
            or not geometry_version
            or window_id not in {"p1", "p2", "p3"}
        ):
            raise GridInputError(f"row ensemble {index} sem membro/janela validos")
        try:
            start_lead_hour = int(raw.get("start_lead_hour"))
            end_lead_hour = int(raw.get("end_lead_hour"))
        except (TypeError, ValueError) as exc:
            raise GridInputError(f"row ensemble {index} sem limites de janela") from exc
        expected_bounds = {"p1": (0, 120), "p2": (120, 240), "p3": (240, 336)}[window_id]
        if (start_lead_hour, end_lead_hour) != expected_bounds:
            raise GridInputError(
                f"row ensemble {index} nao representa integralmente {window_id}: "
                f"{start_lead_hour}-{end_lead_hour}h"
            )
        key = (member_id, basin_id, window_id)
        if key in seen:
            raise GridInputError(f"row ensemble duplicada: {key}")
        seen.add(key)
        coverage = _number(
            raw.get("grid_coverage_fraction"),
            field=f"grid_coverage_fraction row {index}",
            non_negative=True,
        )
        valid_coverage = _number(
            raw.get("valid_data_coverage_fraction"),
            field=f"valid_data_coverage_fraction row {index}",
            non_negative=True,
        )
        if coverage > 1 or valid_coverage > 1:
            raise GridInputError("cobertura ensemble deve estar entre zero e um")
        basin_mean = _number(
            raw.get("basin_mean_mm"),
            field=f"basin_mean_mm row {index}",
            non_negative=True,
        )
        spatial_median = _number(
            raw.get("spatial_median_mm"),
            field=f"spatial_median_mm row {index}",
            non_negative=True,
        )
        point_max = _number(
            raw.get("point_max_mm"),
            field=f"point_max_mm row {index}",
            non_negative=True,
        )
        if point_max < basin_mean:
            raise GridInputError("maximo pontual ensemble menor que media de bacia")
        rows.append(
            EnsembleBasinRow(
                model=model,
                family=family,
                member_id=member_id,
                basin_id=basin_id,
                geometry_version=geometry_version,
                window_id=window_id,
                start_lead_hour=start_lead_hour,
                end_lead_hour=end_lead_hour,
                basin_mean_mm=basin_mean,
                spatial_median_mm=spatial_median,
                point_max_mm=point_max,
                point_max_cell_id=(
                    str(raw.get("point_max_cell_id"))
                    if raw.get("point_max_cell_id") is not None
                    else None
                ),
                grid_coverage_fraction=coverage,
                valid_data_coverage_fraction=valid_coverage,
                quality_flags=tuple(str(value) for value in (raw.get("quality_flags") or ())),
            )
        )

    expected_keys = {
        (member_id, basin_id, window_id)
        for member_id in declared_member_ids
        for basin_id in available_basin_ids
        for window_id in ("p1", "p2", "p3")
    }
    if seen != expected_keys:
        missing = expected_keys - seen
        extra = seen - expected_keys
        raise GridInputError(
            "matriz ensemble incompleta ou excedente: "
            f"missing={len(missing)} extra={len(extra)}"
        )
    geometry_versions_by_basin: dict[str, set[str]] = {}
    for row in rows:
        geometry_versions_by_basin.setdefault(row.basin_id, set()).add(row.geometry_version)
    if any(len(versions) != 1 for versions in geometry_versions_by_basin.values()):
        raise GridInputError("geometry_version varia entre membros/janelas da mesma bacia")

    member_count = int(contract.get("member_count") or len(declared_member_ids))
    if member_count != len(declared_member_ids) or member_count > expected_count:
        raise GridInputError("contagem de membros ensemble inconsistente")
    return EnsembleInput(
        schema_version=schema_version,
        model=model,
        member_count=member_count,
        expected_member_count=expected_count,
        member_ids=declared_member_ids,
        rows=tuple(rows),
        json_path=json_path,
    )


__all__ = [
    "AccumulatedGrid",
    "DailyGrid",
    "EnsembleBasinRow",
    "EnsembleInput",
    "GridInputError",
    "accumulate_daily_grids",
    "load_daily_grids",
    "load_ensemble_input",
    "read_daily_grid",
    "resolve_verified_artifact_file",
]
