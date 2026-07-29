"""Compact, versioned basin aggregates for normalized IMERG calendar grids."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from threading import RLock

from .basins import CANONICAL_BASIN_IDS
from .geometry_loader import GeometryCatalog, cells_near_geometry
from .spatial import (
    BasinSpatialWeights,
    aggregate_with_spatial_weights,
    compute_basin_spatial_weights,
    grid_cells_from_centers,
)

SCHEMA_VERSION = "weather-summary-observed-basins.v1"
_SPATIAL_WEIGHTS_BY_LAYOUT: dict[
    tuple[str, str, tuple[str, ...]], dict[str, BasinSpatialWeights]
] = {}
_SPATIAL_WEIGHTS_LOCK = RLock()


def observed_basin_aggregates_match_contract(
    value: object,
    *,
    geometry_version: str,
    basin_ids: Iterable[str],
) -> bool:
    """Accept compact IMERG aggregates only for the current geometry and scope."""

    expected_basin_ids = tuple(str(basin_id) for basin_id in basin_ids)
    if not isinstance(value, Mapping):
        return False
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("geometry_version") != geometry_version
        or tuple(value.get("requested_basin_ids") or ()) != expected_basin_ids
    ):
        return False
    rows = value.get("basins")
    if not isinstance(rows, list) or value.get("basin_count") != len(expected_basin_ids):
        return False
    row_basin_ids = tuple(
        str(row.get("basin_id") or "")
        for row in rows
        if isinstance(row, Mapping)
    )
    return (
        len(row_basin_ids) == len(rows)
        and row_basin_ids == expected_basin_ids
        and len(set(row_basin_ids)) == len(row_basin_ids)
    )


def _grid(rows: Iterable[tuple[float, float, float]]):
    values_by_coordinate = {}
    for longitude, latitude, precipitation in rows:
        longitude = float(longitude)
        latitude = float(latitude)
        precipitation = float(precipitation)
        if (
            not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not math.isfinite(precipitation)
            or precipitation < 0
        ):
            raise ValueError("Grade observada contem coordenada/precipitacao invalida")
        key = (longitude, latitude)
        if key in values_by_coordinate:
            raise ValueError("Grade observada contem coordenada duplicada")
        values_by_coordinate[key] = precipitation
    if not values_by_coordinate:
        raise ValueError("Grade observada vazia")
    longitudes = tuple(sorted({item[0] for item in values_by_coordinate}))
    latitudes = tuple(sorted({item[1] for item in values_by_coordinate}))
    matrix = tuple(
        tuple(values_by_coordinate.get((longitude, latitude)) for longitude in longitudes)
        for latitude in latitudes
    )
    return grid_cells_from_centers(longitudes, latitudes, matrix)


def _layout_sha256(cells) -> str:
    payload = [
        [cell.cell_id, cell.west, cell.east, cell.south, cell.north]
        for cell in cells
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _compute_weights_by_basin(
    cells,
    geometry_catalog: GeometryCatalog,
    *,
    basin_ids: Iterable[str] = CANONICAL_BASIN_IDS,
):
    return {
        basin_id: compute_basin_spatial_weights(
            geometry_catalog.geometries[basin_id],
            cells_near_geometry(geometry_catalog.geometries[basin_id], cells),
        )
        for basin_id in basin_ids
    }


def _weights_by_basin(
    cells,
    *,
    layout_sha256: str,
    geometry_catalog: GeometryCatalog,
    basin_ids: tuple[str, ...],
    spatial_weights_cache: dict[tuple[str, str, str], BasinSpatialWeights] | None,
):
    if spatial_weights_cache is not None:
        weights_by_basin = {}
        for basin_id in basin_ids:
            cache_key = (geometry_catalog.version, layout_sha256, basin_id)
            weights = spatial_weights_cache.get(cache_key)
            if weights is None:
                geometry = geometry_catalog.geometries[basin_id]
                weights = compute_basin_spatial_weights(
                    geometry,
                    cells_near_geometry(geometry, cells),
                )
                spatial_weights_cache[cache_key] = weights
            weights_by_basin[basin_id] = weights
        return weights_by_basin

    cache_key = (geometry_catalog.version, layout_sha256, basin_ids)
    with _SPATIAL_WEIGHTS_LOCK:
        weights_by_basin = _SPATIAL_WEIGHTS_BY_LAYOUT.get(cache_key)
        if weights_by_basin is None:
            weights_by_basin = _compute_weights_by_basin(
                cells, geometry_catalog, basin_ids=basin_ids
            )
        _SPATIAL_WEIGHTS_BY_LAYOUT[cache_key] = weights_by_basin
        return weights_by_basin


def aggregate_observed_rows_by_basin(
    rows: Iterable[tuple[float, float, float]],
    *,
    observation_date: date,
    geometry_catalog: GeometryCatalog,
    valid_start_utc: datetime,
    valid_end_utc: datetime,
    temporal_resolution: str,
    period_complete: bool,
    basin_ids: Iterable[str] = CANONICAL_BASIN_IDS,
    spatial_weights_cache: dict[tuple[str, str, str], BasinSpatialWeights] | None = None,
) -> dict:
    if valid_start_utc.tzinfo is None or valid_end_utc.tzinfo is None:
        raise ValueError("Periodo observado deve possuir timezone")
    valid_start_utc = valid_start_utc.astimezone(UTC)
    valid_end_utc = valid_end_utc.astimezone(UTC)
    if valid_end_utc <= valid_start_utc:
        raise ValueError("Periodo observado invalido")
    selected_basin_ids = tuple(str(value) for value in basin_ids)
    if not selected_basin_ids or len(set(selected_basin_ids)) != len(selected_basin_ids):
        raise ValueError("Agregacao observada requer bacias unicas")
    unknown_basin_ids = sorted(set(selected_basin_ids) - set(CANONICAL_BASIN_IDS))
    if unknown_basin_ids:
        raise ValueError(f"Agregacao observada possui bacias nao canonicas: {unknown_basin_ids}")
    missing_geometries = sorted(set(selected_basin_ids) - set(geometry_catalog.geometries))
    if missing_geometries:
        raise ValueError(f"Agregacao observada sem geometria: {missing_geometries}")
    cells = _grid(rows)
    layout_sha256 = _layout_sha256(cells)
    values = {cell.cell_id: cell.value_mm for cell in cells}
    weights_by_basin = _weights_by_basin(
        cells,
        layout_sha256=layout_sha256,
        geometry_catalog=geometry_catalog,
        basin_ids=selected_basin_ids,
        spatial_weights_cache=spatial_weights_cache,
    )
    basin_rows = []
    for basin_id in selected_basin_ids:
        weights = weights_by_basin[basin_id]
        aggregation = aggregate_with_spatial_weights(
            weights,
            {
                weight.cell_id: values.get(weight.cell_id)
                for weight in weights.cell_weights
            },
            minimum_valid_coverage_fraction=0.80,
        )
        basin_rows.append(aggregation.to_dict())
    return {
        "schema_version": SCHEMA_VERSION,
        "observation_date": observation_date.isoformat(),
        "valid_start_utc": valid_start_utc.isoformat().replace("+00:00", "Z"),
        "valid_end_utc": valid_end_utc.isoformat().replace("+00:00", "Z"),
        "temporal_resolution": str(temporal_resolution),
        "period_complete": bool(period_complete),
        "geometry_version": geometry_catalog.version,
        "requested_basin_ids": list(selected_basin_ids),
        "grid_layout_sha256": layout_sha256,
        "basin_count": len(basin_rows),
        "basins": basin_rows,
    }


__all__ = [
    "SCHEMA_VERSION",
    "aggregate_observed_rows_by_basin",
    "observed_basin_aggregates_match_contract",
]
