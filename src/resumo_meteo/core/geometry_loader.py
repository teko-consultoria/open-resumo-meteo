"""Load the versioned, repository-local canonical basin geometries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .basins import DEFAULT_BASIN_REGISTRY, BasinRegistry
from .spatial import BasinGeometry, PolygonPart, geometry_area

PACKAGE_ROOT = Path(__file__).resolve().parent


class GeometryLoadError(ValueError):
    pass


Point = tuple[float, float]
Ring = tuple[Point, ...]


def _signed_area(ring: Ring) -> float:
    return sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(ring, (*ring[1:], ring[0]))
    ) / 2.0


def _normalize_ring(points: Iterable[Point]) -> Ring | None:
    cleaned: list[Point] = []
    for raw in points:
        point = float(raw[0]), float(raw[1])
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    if len(set(cleaned)) < 3 or abs(_signed_area(tuple(cleaned))) <= 1e-15:
        return None
    return tuple(cleaned)


def read_geojson_geometry(
    path: str | Path,
    *,
    basin_id: str,
    canonical_name: str,
    geometry_version: str,
    crs: str,
) -> BasinGeometry:
    """Read one audited GeoJSON Feature and verify its catalog identity."""

    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GeometryLoadError(f"nao foi possivel ler GeoJSON {resolved}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "Feature":
        raise GeometryLoadError(f"GeoJSON deve conter um unico Feature: {resolved}")
    properties = payload.get("properties")
    geometry = payload.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        raise GeometryLoadError(f"Feature GeoJSON incompleto: {resolved}")
    if properties.get("basin_id") != basin_id:
        raise GeometryLoadError(f"basin_id divergente no GeoJSON de {basin_id}")
    if properties.get("canonical_name") != canonical_name:
        raise GeometryLoadError(f"nome canonico divergente no GeoJSON de {basin_id}")
    embedded_version = properties.get("geometry_version")
    if not isinstance(embedded_version, str) or not embedded_version.strip():
        raise GeometryLoadError(f"geometry_version ausente no GeoJSON de {basin_id}")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        raise GeometryLoadError(f"coordinates invalidas no GeoJSON de {basin_id}")
    if geometry_type == "Polygon":
        raw_polygons = (coordinates,)
    elif geometry_type == "MultiPolygon":
        raw_polygons = tuple(coordinates)
    else:
        raise GeometryLoadError(f"tipo GeoJSON nao poligonal para {basin_id}")
    polygons: list[PolygonPart] = []
    try:
        for raw_polygon in raw_polygons:
            if not isinstance(raw_polygon, list) or not raw_polygon:
                raise ValueError("poligono sem anel exterior")
            exterior = _normalize_ring(raw_polygon[0])
            if exterior is None:
                raise ValueError("anel exterior degenerado")
            # Zero-area interior rings carry no area and are discarded.
            holes = tuple(
                normalized
                for raw_ring in raw_polygon[1:]
                if (normalized := _normalize_ring(raw_ring)) is not None
            )
            polygons.append(PolygonPart.from_rings(exterior, holes))
        return BasinGeometry(
            basin_id=basin_id,
            polygons=tuple(polygons),
            geometry_version=geometry_version,
            crs=crs,
        )
    except (TypeError, ValueError) as exc:
        raise GeometryLoadError(f"geometria GeoJSON invalida para {basin_id}") from exc


@dataclass(frozen=True, slots=True)
class GeometryCatalog:
    version: str
    source_name: str
    crs: str
    geometries: dict[str, BasinGeometry]
    area_km2: dict[str, float]
    missing_basin_ids: tuple[str, ...]

    def geometry(self, basin_id: str) -> BasinGeometry | None:
        return self.geometries.get(basin_id)


def geometry_bbox(geometry: BasinGeometry) -> tuple[float, float, float, float]:
    points = [
        point
        for polygon in geometry.polygons
        for ring in (polygon.exterior, *polygon.holes)
        for point in ring
    ]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def cells_near_geometry(geometry: BasinGeometry, cells, *, margin_degrees: float = 1.0):
    """Cheap bounding-box reduction before exact polygon/cell intersection."""

    west, south, east, north = geometry_bbox(geometry)
    return tuple(
        cell
        for cell in cells
        if cell.east >= west - margin_degrees
        and cell.west <= east + margin_degrees
        and cell.north >= south - margin_degrees
        and cell.south <= north + margin_degrees
    )


def load_geometry_catalog(
    *,
    registry: BasinRegistry = DEFAULT_BASIN_REGISTRY,
    repository_root: str | Path | None = None,
) -> GeometryCatalog:
    root = Path(repository_root or PACKAGE_ROOT).resolve()
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            registry.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    paths: dict[str, Path] = {}
    for basin in registry.basins:
        path = (root / basin.geometry_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise GeometryLoadError(f"caminho de contorno inseguro para {basin.basin_id}") from exc
        if not path.is_file():
            raise GeometryLoadError(f"contorno cadastrado ausente para {basin.basin_id}: {path}")
        digest.update(basin.basin_id.encode("ascii"))
        digest.update(path.read_bytes())
        paths[basin.basin_id] = path
    version = f"{registry.catalog_id}@{registry.catalog_version}+{digest.hexdigest()[:16]}"
    geometries: dict[str, BasinGeometry] = {}
    areas: dict[str, float] = {}
    for basin_id, path in paths.items():
        basin = registry.require_id(basin_id)
        geometry = read_geojson_geometry(
            path,
            basin_id=basin_id,
            canonical_name=basin.canonical_name,
            geometry_version=version,
            crs=registry.crs,
        )
        geometries[basin_id] = geometry
        areas[basin_id] = geometry_area(geometry, area_method="spherical")
    missing = tuple(basin.basin_id for basin in registry.basins if basin.basin_id not in geometries)
    if tuple(geometries) != tuple(basin.basin_id for basin in registry.basins) or missing:
        raise GeometryLoadError("catalogo geometrico diverge do cadastro canonico")
    return GeometryCatalog(
        version=version,
        source_name="contornos canonicos materializados e auditados no projeto",
        crs=registry.crs,
        geometries=geometries,
        area_km2=areas,
        missing_basin_ids=missing,
    )


__all__ = [
    "GeometryCatalog",
    "GeometryLoadError",
    "cells_near_geometry",
    "geometry_bbox",
    "load_geometry_catalog",
    "read_geojson_geometry",
]
