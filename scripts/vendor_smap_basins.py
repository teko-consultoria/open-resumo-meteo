#!/usr/bin/env python3
"""Vendor the public ONS/SMAP basin contours used by gridded weather maps."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CATALOG_ID = "smap-aggregated-basins-apr2025"
CATALOG_VERSION = "2.0.0"
CONTOUR_SET_ID = "smap_abr2025"
GEOMETRY_VERSION = "smap-aggregated-apr2025-grid-v2"

# ID, shapefile stem, source label, canonical label, article, subsystem,
# hydrographic region.
BASINS = (
    ("amazonas_norte", "Amazonas_Norte", "Amazonas Norte", "Amazonas Norte", "o", "N", "Amazonas"),
    (
        "amazonas_sudeste",
        "Amazonas_Sudeste",
        "Amazonas Sudeste",
        "Amazonas Sudeste",
        "o",
        "N",
        "Amazonas",
    ),
    ("araguari", "Araguari", "Araguari", "Araguari", "o", "N", "Amazônica"),
    ("capivari", "Capivari", "Capivari", "Capivari", "o", "S", "Atlântico Sul"),
    ("doce", "Doce", "Doce", "Doce", "o", "SE_CO", "Atlântico Sudeste"),
    ("grande", "Grande", "Grande", "Grande", "o", "SE_CO", "Paraná"),
    ("iguacu", "Iguacu", "Iguaçu", "Iguaçu", "o", "S", "Paraná"),
    (
        "itabapoana",
        "Itabapoana",
        "Itabapoana",
        "Itabapoana",
        "o",
        "SE_CO",
        "Atlântico Sudeste",
    ),
    ("itajai_acu", "Itajai_Acu", "Itajaí-Açu", "Itajaí-Açu", "o", "S", "Atlântico Sul"),
    ("jacui", "Jacui", "Jacuí", "Jacuí", "o", "S", "Atlântico Sul"),
    (
        "jequitinhonha",
        "Jequitinhonha",
        "Jequitinhonha",
        "Jequitinhonha",
        "o",
        "NE",
        "Atlântico Leste",
    ),
    ("madeira", "Madeira", "Madeira", "Madeira", "o", "N", "Madeira"),
    ("mucuri", "Mucuri", "Mucuri", "Mucuri", "o", "NE", "Atlântico Leste"),
    (
        "paraguacu",
        "Paraguacu",
        "Paraguaçu",
        "Paraguaçu",
        "o",
        "NE",
        "Atlântico Leste",
    ),
    ("paraguai", "Paraguai", "Paraguai", "Paraguai", "o", "SE_CO", "Paraguai"),
    (
        "paraiba_do_sul",
        "Paraiba_do_Sul",
        "Paraíba do Sul",
        "Paraíba do Sul",
        "a",
        "SE_CO",
        "Atlântico Sudeste",
    ),
    ("parana", "Parana", "Paraná", "Paraná", "o", "SE_CO", "Paraná"),
    ("paranaiba", "Paranaiba", "Paranaíba", "Paranaíba", "o", "SE_CO", "Paraná"),
    (
        "paranapanema",
        "Paranapanema",
        "Paranapanema",
        "Paranapanema",
        "o",
        "S",
        "Paraná",
    ),
    ("parnaiba", "Parnaiba", "Parnaíba", "Parnaíba", "o", "NE", "Parnaíba"),
    (
        "sao_francisco",
        "Sao_Francisco",
        "São Francisco",
        "São Francisco",
        "o",
        "NE",
        "São Francisco",
    ),
    (
        "santa_maria_vitoria",
        "Sta_Maria_da_Vitoria",
        "Sta. Maria da Vitória",
        "Santa Maria da Vitória",
        "a",
        "SE_CO",
        "Atlântico Leste",
    ),
    ("teles_pires", "Teles_Pires", "Teles Pires", "Teles Pires", "o", "N", "Tapajós"),
    ("tiete", "Tiete", "Tietê", "Tietê", "o", "SE_CO", "Paraná"),
    (
        "tocantins",
        "Tocantins",
        "Tocantins",
        "Tocantins",
        "o",
        "N",
        "Tocantins-Araguaia",
    ),
    ("uruguai", "Uruguai", "Uruguai", "Uruguai", "o", "S", "Uruguai"),
    ("xingu", "Xingu", "Xingu", "Xingu", "o", "N", "Xingu"),
)


@dataclass(frozen=True)
class ShapefileGeometry:
    rings: tuple[tuple[tuple[float, float], ...], ...]
    record_count: int
    point_count: int


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ascii_alias(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).lower()


def _bbox(geometry: dict[str, Any]) -> list[float]:
    coordinates = geometry["coordinates"]
    points: list[tuple[float, float]] = []

    def visit(value: Any) -> None:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            points.append((float(value[0]), float(value[1])))
            return
        if isinstance(value, list):
            for item in value:
                visit(item)

    visit(coordinates)
    return [
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ]


def _signed_area(ring: tuple[tuple[float, float], ...]) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False))


def _point_in_ring(point: tuple[float, float], ring: tuple[tuple[float, float], ...]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        if (y1 > y) == (y2 > y):
            continue
        crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
        if x < crossing_x:
            inside = not inside
    return inside


def _read_polygon_shapefile(path: Path) -> ShapefileGeometry:
    data = path.read_bytes()
    if len(data) < 100 or struct.unpack_from(">i", data, 0)[0] != 9994:
        raise ValueError(f"Invalid shapefile header: {path}")

    declared_length = struct.unpack_from(">i", data, 24)[0] * 2
    if declared_length != len(data):
        raise ValueError(f"Unexpected shapefile length: {path}")

    file_shape_type = struct.unpack_from("<i", data, 32)[0]
    if file_shape_type not in {5, 15, 25}:
        raise ValueError(f"Expected Polygon shapefile, found type {file_shape_type}: {path}")

    offset = 100
    record_count = 0
    point_count = 0
    rings: list[tuple[tuple[float, float], ...]] = []
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError(f"Truncated shapefile record header: {path}")
        content_length = struct.unpack_from(">i", data, offset + 4)[0] * 2
        content_start = offset + 8
        content_end = content_start + content_length
        if content_end > len(data):
            raise ValueError(f"Truncated shapefile record: {path}")

        shape_type = struct.unpack_from("<i", data, content_start)[0]
        if shape_type == 0:
            offset = content_end
            continue
        if shape_type not in {5, 15, 25}:
            raise ValueError(f"Unexpected record shape type {shape_type}: {path}")

        num_parts, num_points = struct.unpack_from("<2i", data, content_start + 36)
        parts_offset = content_start + 44
        points_offset = parts_offset + 4 * num_parts
        parts = list(struct.unpack_from(f"<{num_parts}i", data, parts_offset))
        parts.append(num_points)
        for start, end in zip(parts, parts[1:], strict=False):
            ring = tuple(
                struct.unpack_from("<2d", data, points_offset + point_index * 16)
                for point_index in range(start, end)
            )
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError(f"Open or degenerate polygon ring: {path}")
            if math.isclose(_signed_area(ring), 0.0, abs_tol=1e-12):
                raise ValueError(f"Zero-area polygon ring: {path}")
            rings.append(ring)

        record_count += 1
        point_count += num_points
        offset = content_end

    if not rings:
        raise ValueError(f"No polygon rings found: {path}")
    return ShapefileGeometry(tuple(rings), record_count, point_count)


def _as_geojson(source: ShapefileGeometry) -> tuple[dict[str, Any], int]:
    ring_areas = [abs(_signed_area(ring)) for ring in source.rings]
    containers = [
        [
            candidate_index
            for candidate_index, candidate in enumerate(source.rings)
            if candidate_index != ring_index
            and ring_areas[candidate_index] > ring_areas[ring_index]
            and _point_in_ring(ring[0], candidate)
        ]
        for ring_index, ring in enumerate(source.rings)
    ]

    exterior_indexes = [index for index, parents in enumerate(containers) if len(parents) % 2 == 0]
    polygons: list[list[list[list[float]]]] = []
    for exterior_index in exterior_indexes:
        exterior_depth = len(containers[exterior_index])
        holes = [
            index
            for index, parents in enumerate(containers)
            if len(parents) == exterior_depth + 1 and exterior_index in parents
        ]
        polygon = [
            [[longitude, latitude] for longitude, latitude in source.rings[index]]
            for index in (exterior_index, *holes)
        ]
        polygons.append(polygon)

    if not polygons:
        raise ValueError("Could not classify shapefile rings")
    interior_ring_count = sum(len(polygon) - 1 for polygon in polygons)
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}, interior_ring_count
    return {"type": "MultiPolygon", "coordinates": polygons}, interior_ring_count


def _source_paths(source: Path) -> tuple[Path, Path]:
    candidate = source / "individuais"
    shapes_dir = candidate if candidate.is_dir() else source
    metadata_dir = shapes_dir.parent
    if not shapes_dir.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {shapes_dir}")
    return shapes_dir, metadata_dir


def _read_summary(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {row["bacia"]: row for row in rows}


def _source_set_sha256(shapes_dir: Path) -> str:
    digest = hashlib.sha256()
    for _basin_id, stem, *_metadata in BASINS:
        path = shapes_dir / f"{stem}.shp"
        relative_path = f"individuais/{path.name}"
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "source",
        type=Path,
        help="Directory containing the public SMAP contour set and its individuais/ folder",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    shapes_dir, metadata_dir = _source_paths(args.source.resolve())
    summary_path = metadata_dir / "resumo_bacias_agregadas.csv"
    readme_path = metadata_dir / "README.md"
    summary = _read_summary(summary_path)

    core_dir = repo / "src/resumo_meteo/core"
    output_dir = core_dir / "assets/basins/v2"
    output_dir.mkdir(parents=True, exist_ok=True)

    registry_items: list[dict[str, Any]] = []
    manifest_geometries: dict[str, dict[str, Any]] = {}
    for (
        basin_id,
        stem,
        source_name,
        canonical_name,
        article,
        subsystem,
        hydrographic_region,
    ) in BASINS:
        shapefile_path = shapes_dir / f"{stem}.shp"
        source_geometry = _read_polygon_shapefile(shapefile_path)
        geometry, interior_ring_count = _as_geojson(source_geometry)
        aliases = [f"Bacia {'da' if article == 'a' else 'do'} {canonical_name}"]
        ascii_alias = _ascii_alias(canonical_name)
        if ascii_alias != canonical_name.casefold():
            aliases.insert(0, ascii_alias)
        summary_row = summary.get(source_name)
        if summary_row is None:
            raise KeyError(f"Missing source summary row for {source_name!r}")

        feature = {
            "type": "Feature",
            "properties": {
                "basin_id": basin_id,
                "canonical_name": canonical_name,
                "source_basin_name": source_name,
                "geometry_version": GEOMETRY_VERSION,
                "source_status": summary_row["status"],
                "source_n_fontes": int(summary_row["n_fontes"]),
                "source_fontes": summary_row["fontes_usadas"],
            },
            "geometry": geometry,
        }

        relative_path = f"assets/basins/v2/{basin_id}.geojson"
        output_path = core_dir / relative_path
        output_path.write_text(
            json.dumps(feature, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        geometry_sha = _sha256(output_path)

        registry_items.append(
            {
                "basin_id": basin_id,
                "canonical_name": canonical_name,
                "editorial_article": article,
                "aliases": aliases,
                "subsystem": subsystem,
                "hydrographic_region": hydrographic_region,
                "geometry_id": f"smap-aggregated-{basin_id}",
                "geometry_path": relative_path,
                "geometry_source_reference": (
                    f"assets/basins/v2/manifest.json#geometries.{basin_id}"
                ),
            }
        )
        manifest_geometries[basin_id] = {
            "source_basin_name": source_name,
            "source_shapefile": f"individuais/{shapefile_path.name}",
            "source_shapefile_sha256": _sha256(shapefile_path),
            "source_record_count": source_geometry.record_count,
            "source_part_count": len(source_geometry.rings),
            "source_point_count": source_geometry.point_count,
            "interior_ring_count": interior_ring_count,
            "path": relative_path,
            "sha256": geometry_sha,
            "geometry_type": geometry["type"],
            "bbox": _bbox(geometry),
        }

    registry = {
        "schema_version": 4,
        "catalog_id": CATALOG_ID,
        "catalog_version": CATALOG_VERSION,
        "source_note": (
            "Catálogo canônico dos 27 contornos públicos agregados segundo a configuração "
            "SMAP de abril de 2025 e alinhados ao conjunto operacional dos mapas gradeados. "
            "O resumo meteorológico usa somente as 12 bacias com climatologia MERGE/INPE "
            "compatível; as demais permanecem disponíveis no catálogo geométrico."
        ),
        "crs": "EPSG:4326",
        "basins": registry_items,
    }
    registry_path = core_dir / "config/basins.json"
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema_version": 2,
        "catalog_id": CATALOG_ID,
        "catalog_version": CATALOG_VERSION,
        "geometry_version": GEOMETRY_VERSION,
        "crs": "EPSG:4326",
        "runtime_dependency_on_source_repository": False,
        "source": {
            "provider": "Operador Nacional do Sistema Elétrico (ONS)",
            "url": (
                "https://dados.ons.org.br/dataset/"
                "dados-hidrologicos-horizontes-de-bacias-hidrograficas"
            ),
            "license": "Creative Commons Attribution",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "description": (
                "Contornos públicos do ONS agregados conforme a configuração SMAP de "
                "abril de 2025 e fixados no mesmo conjunto operacional dos mapas gradeados."
            ),
            "contour_set_id": CONTOUR_SET_ID,
            "source_kind": "operational_gridded_weather_shapefiles",
            "source_shapefile_set_sha256": _source_set_sha256(shapes_dir),
            "readme_sha256": _sha256(readme_path),
            "summary_sha256": _sha256(summary_path),
            "conversion": (
                "Cada parte poligonal dos shapefiles operacionais foi transcrita para "
                "GeoJSON em WGS84, sem simplificação nem alteração de coordenadas."
            ),
        },
        "geometries": manifest_geometries,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
