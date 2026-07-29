"""Conservative basin aggregation by cell/polygon intersection area.

No geospatial runtime dependency is required.  Basin rings are clipped against
each rectangular native-grid cell with Sutherland-Hodgman clipping; the
intersection is then measured either on a sphere (the production default for
EPSG:4326) or in planar coordinates (useful for projected data and tests).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .basins import require_canonical_basin_id

EARTH_RADIUS_KM = 6371.0088
Point = tuple[float, float]
Ring = tuple[Point, ...]


def _finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} deve ser numerico")
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} deve ser numerico") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} deve ser finito")
    return numeric


def _normalize_ring(points: Iterable[Sequence[float]]) -> Ring:
    ring: list[Point] = []
    for raw_point in points:
        if len(raw_point) < 2:
            raise ValueError("Cada vertice precisa de longitude/x e latitude/y")
        point = (
            _finite_number(raw_point[0], field_name="coordenada x"),
            _finite_number(raw_point[1], field_name="coordenada y"),
        )
        if not ring or point != ring[-1]:
            ring.append(point)
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring.pop()
    if len(set(ring)) < 3:
        raise ValueError("Anel poligonal precisa de ao menos tres vertices distintos")
    if planar_polygon_area(tuple(ring)) <= 0:
        raise ValueError("Anel poligonal tem area nula")
    return tuple(ring)


@dataclass(frozen=True, slots=True)
class PolygonPart:
    exterior: Ring
    holes: tuple[Ring, ...] = ()

    def __post_init__(self) -> None:
        exterior = _normalize_ring(self.exterior)
        holes = tuple(_normalize_ring(hole) for hole in self.holes)
        for hole in holes:
            if not all(_point_in_or_on_ring(point, exterior) for point in hole):
                raise ValueError("Furo deve estar integralmente dentro do anel exterior")
        object.__setattr__(self, "exterior", exterior)
        object.__setattr__(self, "holes", holes)

    @classmethod
    def from_rings(
        cls,
        exterior: Iterable[Sequence[float]],
        holes: Iterable[Iterable[Sequence[float]]] = (),
    ) -> PolygonPart:
        return cls(
            exterior=_normalize_ring(exterior),
            holes=tuple(_normalize_ring(ring) for ring in holes),
        )


@dataclass(frozen=True, slots=True)
class BasinGeometry:
    basin_id: str
    polygons: tuple[PolygonPart, ...]
    geometry_version: str
    crs: str = "EPSG:4326"

    def __post_init__(self) -> None:
        require_canonical_basin_id(self.basin_id)
        if not self.polygons:
            raise ValueError("Geometria de bacia precisa de ao menos um poligono")
        if not self.geometry_version.strip():
            raise ValueError("geometry_version e obrigatoria")
        if not self.crs.strip():
            raise ValueError("crs e obrigatorio")

    @classmethod
    def from_polygon(
        cls,
        *,
        basin_id: str,
        exterior: Iterable[Sequence[float]],
        holes: Iterable[Iterable[Sequence[float]]] = (),
        geometry_version: str,
        crs: str = "EPSG:4326",
    ) -> BasinGeometry:
        return cls(
            basin_id=basin_id,
            polygons=(PolygonPart.from_rings(exterior, holes),),
            geometry_version=geometry_version,
            crs=crs,
        )

@dataclass(frozen=True, slots=True)
class GridCell:
    cell_id: str
    west: float
    east: float
    south: float
    north: float
    value_mm: float | None = None

    def __post_init__(self) -> None:
        if not self.cell_id:
            raise ValueError("cell_id e obrigatorio")
        for field_name in ("west", "east", "south", "north"):
            object.__setattr__(self, field_name, _finite_number(getattr(self, field_name), field_name=field_name))
        if self.east <= self.west or self.north <= self.south:
            raise ValueError("Limites da celula devem ter largura e altura positivas")
        if self.value_mm is not None:
            value = _finite_number(self.value_mm, field_name="value_mm")
            if value < 0:
                raise ValueError("value_mm nao pode ser negativo")
            object.__setattr__(self, "value_mm", value)


@dataclass(frozen=True, slots=True)
class CellIntersectionWeight:
    cell_id: str
    intersection_area: float
    basin_area_fraction: float

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "intersection_area": self.intersection_area,
            "basin_area_fraction": self.basin_area_fraction,
        }


@dataclass(frozen=True, slots=True)
class BasinSpatialWeights:
    basin_id: str
    geometry_version: str
    crs: str
    area_method: str
    basin_area: float
    grid_intersection_area: float
    grid_coverage_fraction: float
    cell_weights: tuple[CellIntersectionWeight, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "basin_id": self.basin_id,
            "geometry_version": self.geometry_version,
            "crs": self.crs,
            "area_method": self.area_method,
            "basin_area": self.basin_area,
            "grid_intersection_area": self.grid_intersection_area,
            "grid_coverage_fraction": self.grid_coverage_fraction,
            "cell_weights": [weight.to_dict() for weight in self.cell_weights],
        }


@dataclass(frozen=True, slots=True)
class BasinPrecipitationAggregation:
    basin_id: str
    basin_mean_mm: float | None
    spatial_p10_mm: float | None
    spatial_median_mm: float | None
    spatial_p90_mm: float | None
    point_max_mm: float | None
    point_max_cell_id: str | None
    grid_coverage_fraction: float
    valid_data_coverage_fraction: float
    covered_area: float
    valid_data_area: float
    area_above_threshold_fraction: Mapping[str, float]
    quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "basin_id": self.basin_id,
            "basin_mean_mm": self.basin_mean_mm,
            "spatial_p10_mm": self.spatial_p10_mm,
            "spatial_median_mm": self.spatial_median_mm,
            "spatial_p90_mm": self.spatial_p90_mm,
            # The explicit name is deliberate: it must never be rendered as a
            # basin-average amount.
            "point_max_mm": self.point_max_mm,
            "point_max_cell_id": self.point_max_cell_id,
            "grid_coverage_fraction": self.grid_coverage_fraction,
            "valid_data_coverage_fraction": self.valid_data_coverage_fraction,
            "covered_area": self.covered_area,
            "valid_data_area": self.valid_data_area,
            "area_above_threshold_fraction": dict(self.area_above_threshold_fraction),
            "quality_flags": list(self.quality_flags),
        }


def planar_polygon_area(ring: Ring) -> float:
    if len(ring) < 3:
        return 0.0
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(ring, (*ring[1:], ring[0]))
        )
    ) / 2.0


def _point_on_segment(point: Point, start: Point, end: Point, epsilon: float = 1e-10) -> bool:
    px, py = point
    sx, sy = start
    ex, ey = end
    cross = (px - sx) * (ey - sy) - (py - sy) * (ex - sx)
    if abs(cross) > epsilon:
        return False
    return (
        min(sx, ex) - epsilon <= px <= max(sx, ex) + epsilon
        and min(sy, ey) - epsilon <= py <= max(sy, ey) + epsilon
    )


def _point_in_or_on_ring(point: Point, ring: Ring) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        if _point_on_segment(point, previous, current):
            return True
        if (current[1] > point[1]) != (previous[1] > point[1]):
            x_crossing = (
                (previous[0] - current[0])
                * (point[1] - current[1])
                / (previous[1] - current[1])
                + current[0]
            )
            if point[0] < x_crossing:
                inside = not inside
        previous = current
    return inside


def spherical_polygon_area_km2(ring: Ring) -> float:
    """Area on a sphere for lon/lat rings, adequate for conservative weights."""

    if len(ring) < 3:
        return 0.0
    accumulator = 0.0
    closed = (*ring, ring[0])
    for (lon1, lat1), (lon2, lat2) in zip(closed, closed[1:]):
        delta_lon = math.radians(lon2 - lon1)
        if delta_lon > math.pi:
            delta_lon -= 2 * math.pi
        elif delta_lon < -math.pi:
            delta_lon += 2 * math.pi
        accumulator += delta_lon * (math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(accumulator) * (EARTH_RADIUS_KM**2) / 2.0


def _ring_area(ring: Ring, area_method: str) -> float:
    if area_method == "planar":
        return planar_polygon_area(ring)
    if area_method == "spherical":
        return spherical_polygon_area_km2(ring)
    raise ValueError("area_method deve ser 'spherical' ou 'planar'")


def _inside(point: Point, edge: str, boundary: float, epsilon: float = 1e-12) -> bool:
    x, y = point
    if edge == "left":
        return x >= boundary - epsilon
    if edge == "right":
        return x <= boundary + epsilon
    if edge == "bottom":
        return y >= boundary - epsilon
    return y <= boundary + epsilon


def _edge_intersection(start: Point, end: Point, edge: str, boundary: float) -> Point:
    x1, y1 = start
    x2, y2 = end
    if edge in {"left", "right"}:
        if math.isclose(x1, x2, abs_tol=1e-15):
            return boundary, y1
        fraction = (boundary - x1) / (x2 - x1)
        return boundary, y1 + fraction * (y2 - y1)
    if math.isclose(y1, y2, abs_tol=1e-15):
        return x1, boundary
    fraction = (boundary - y1) / (y2 - y1)
    return x1 + fraction * (x2 - x1), boundary


def clip_ring_to_cell(ring: Ring, cell: GridCell) -> Ring:
    """Clip a ring to one rectangular cell."""

    output = list(ring)
    for edge, boundary in (
        ("left", cell.west),
        ("right", cell.east),
        ("bottom", cell.south),
        ("top", cell.north),
    ):
        if not output:
            break
        input_points = output
        output = []
        start = input_points[-1]
        for end in input_points:
            start_inside = _inside(start, edge, boundary)
            end_inside = _inside(end, edge, boundary)
            if end_inside:
                if not start_inside:
                    output.append(_edge_intersection(start, end, edge, boundary))
                output.append(end)
            elif start_inside:
                output.append(_edge_intersection(start, end, edge, boundary))
            start = end
    cleaned: list[Point] = []
    for point in output:
        if not cleaned or not (
            math.isclose(point[0], cleaned[-1][0], abs_tol=1e-12)
            and math.isclose(point[1], cleaned[-1][1], abs_tol=1e-12)
        ):
            cleaned.append(point)
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    return tuple(cleaned) if len(cleaned) >= 3 else ()


def geometry_area(geometry: BasinGeometry, *, area_method: str = "spherical") -> float:
    area = 0.0
    for polygon in geometry.polygons:
        part_area = _ring_area(polygon.exterior, area_method)
        holes_area = sum(_ring_area(hole, area_method) for hole in polygon.holes)
        if holes_area >= part_area:
            raise ValueError(f"Furos invalidos na geometria {geometry.basin_id}")
        area += part_area - holes_area
    if area <= 0:
        raise ValueError(f"Geometria {geometry.basin_id} tem area nao positiva")
    return area


def cell_geometry_intersection_area(
    cell: GridCell,
    geometry: BasinGeometry,
    *,
    area_method: str = "spherical",
) -> float:
    area = 0.0
    for polygon in geometry.polygons:
        clipped_exterior = clip_ring_to_cell(polygon.exterior, cell)
        if not clipped_exterior:
            continue
        part_area = _ring_area(clipped_exterior, area_method)
        holes_area = sum(
            _ring_area(clipped_hole, area_method)
            for hole in polygon.holes
            if (clipped_hole := clip_ring_to_cell(hole, cell))
        )
        area += max(part_area - holes_area, 0.0)
    return area


def compute_basin_spatial_weights(
    geometry: BasinGeometry,
    cells: Iterable[GridCell],
    *,
    area_method: str = "spherical",
    overlap_tolerance: float = 1e-6,
) -> BasinSpatialWeights:
    cell_values = tuple(cells)
    cell_ids = tuple(cell.cell_id for cell in cell_values)
    if len(set(cell_ids)) != len(cell_ids):
        raise ValueError("cell_id duplicado na grade")
    basin_area = geometry_area(geometry, area_method=area_method)
    weights: list[CellIntersectionWeight] = []
    for cell in cell_values:
        area = cell_geometry_intersection_area(cell, geometry, area_method=area_method)
        if area > basin_area * 1e-15:
            weights.append(
                CellIntersectionWeight(
                    cell_id=cell.cell_id,
                    intersection_area=area,
                    basin_area_fraction=area / basin_area,
                )
            )
    covered_area = sum(weight.intersection_area for weight in weights)
    coverage = covered_area / basin_area
    if coverage > 1 + overlap_tolerance:
        raise ValueError("Celulas da grade se sobrepoem dentro da bacia")
    return BasinSpatialWeights(
        basin_id=geometry.basin_id,
        geometry_version=geometry.geometry_version,
        crs=geometry.crs,
        area_method=area_method,
        basin_area=basin_area,
        grid_intersection_area=min(covered_area, basin_area),
        grid_coverage_fraction=min(coverage, 1.0),
        cell_weights=tuple(weights),
    )


def _threshold_key(threshold_mm: float) -> str:
    token = str(int(threshold_mm)) if threshold_mm.is_integer() else format(threshold_mm, "g")
    return f"gt_{token}_mm"


def _weighted_quantile(weighted_values: Sequence[tuple[float, float]], probability: float) -> float:
    if not weighted_values:
        raise ValueError("Quantil ponderado sem valores")
    ordered = sorted(weighted_values, key=lambda item: item[0])
    total = sum(weight for _value, weight in ordered)
    target = total * probability
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def aggregate_with_spatial_weights(
    weights: BasinSpatialWeights,
    values_by_cell_id: Mapping[str, float | None],
    *,
    thresholds_mm: Iterable[float] = (5.0, 10.0, 25.0, 50.0),
    minimum_valid_coverage_fraction: float = 0.95,
) -> BasinPrecipitationAggregation:
    if not 0 <= minimum_valid_coverage_fraction <= 1:
        raise ValueError("minimum_valid_coverage_fraction deve estar entre 0 e 1")
    thresholds = tuple(float(value) for value in thresholds_mm)
    if len(set(thresholds)) != len(thresholds) or any(
        not math.isfinite(value) or value < 0 for value in thresholds
    ):
        raise ValueError("thresholds_mm deve conter valores unicos, finitos e nao negativos")

    weighted_values: list[tuple[float, float, str]] = []
    for weight in weights.cell_weights:
        raw_value = values_by_cell_id.get(weight.cell_id)
        if raw_value is None:
            continue
        try:
            value = _finite_number(raw_value, field_name=f"valor da celula {weight.cell_id}")
        except (TypeError, ValueError):
            continue
        if value < 0:
            raise ValueError(f"Precipitacao negativa na celula {weight.cell_id}")
        weighted_values.append((value, weight.intersection_area, weight.cell_id))

    valid_area = sum(area for _value, area, _cell_id in weighted_values)
    valid_coverage = valid_area / weights.basin_area
    flags: list[str] = []
    if weights.grid_coverage_fraction < 1 - 1e-6:
        flags.append("partial_grid_coverage")
    if valid_coverage < minimum_valid_coverage_fraction:
        flags.append("insufficient_valid_data_coverage")
    if not weighted_values:
        flags.append("no_valid_grid_values")
        basin_mean = None
        spatial_p10 = None
        spatial_median = None
        spatial_p90 = None
        point_max = None
        point_max_cell_id = None
    else:
        basin_mean = sum(value * area for value, area, _cell_id in weighted_values) / valid_area
        spatial_p10 = _weighted_quantile(
            [(value, area) for value, area, _cell_id in weighted_values],
            0.10,
        )
        spatial_median = _weighted_quantile(
            [(value, area) for value, area, _cell_id in weighted_values],
            0.50,
        )
        spatial_p90 = _weighted_quantile(
            [(value, area) for value, area, _cell_id in weighted_values],
            0.90,
        )
        point_max, _point_area, point_max_cell_id = max(
            weighted_values,
            key=lambda item: (item[0], item[2]),
        )

    threshold_fractions = {
        _threshold_key(threshold): (
            sum(area for value, area, _cell_id in weighted_values if value > threshold) / valid_area
            if valid_area
            else 0.0
        )
        for threshold in thresholds
    }
    return BasinPrecipitationAggregation(
        basin_id=weights.basin_id,
        basin_mean_mm=basin_mean,
        spatial_p10_mm=spatial_p10,
        spatial_median_mm=spatial_median,
        spatial_p90_mm=spatial_p90,
        point_max_mm=point_max,
        point_max_cell_id=point_max_cell_id,
        grid_coverage_fraction=weights.grid_coverage_fraction,
        valid_data_coverage_fraction=min(valid_coverage, 1.0),
        covered_area=weights.grid_intersection_area,
        valid_data_area=valid_area,
        area_above_threshold_fraction=threshold_fractions,
        quality_flags=tuple(flags),
    )


def aggregate_basin_precipitation(
    geometry: BasinGeometry,
    cells: Iterable[GridCell],
    *,
    area_method: str = "spherical",
    thresholds_mm: Iterable[float] = (5.0, 10.0, 25.0, 50.0),
    minimum_valid_coverage_fraction: float = 0.95,
) -> BasinPrecipitationAggregation:
    cell_values = tuple(cells)
    weights = compute_basin_spatial_weights(geometry, cell_values, area_method=area_method)
    values = {cell.cell_id: cell.value_mm for cell in cell_values}
    return aggregate_with_spatial_weights(
        weights,
        values,
        thresholds_mm=thresholds_mm,
        minimum_valid_coverage_fraction=minimum_valid_coverage_fraction,
    )


def _coordinate_edges(coordinates: Sequence[float], *, single_spacing: float | None) -> tuple[float, ...]:
    if not coordinates:
        raise ValueError("Eixo de grade vazio")
    values = tuple(_finite_number(value, field_name="coordenada de grade") for value in coordinates)
    if len(values) == 1:
        if single_spacing is None or single_spacing <= 0:
            raise ValueError("Eixo unitario requer single_spacing positivo")
        return (values[0] - single_spacing / 2, values[0] + single_spacing / 2)
    if any(current <= previous for previous, current in zip(values, values[1:])):
        raise ValueError("Coordenadas de grade devem estar estritamente crescentes")
    midpoints = tuple((first + second) / 2 for first, second in zip(values, values[1:]))
    return (
        values[0] - (midpoints[0] - values[0]),
        *midpoints,
        values[-1] + (values[-1] - midpoints[-1]),
    )


def grid_cells_from_centers(
    longitudes: Sequence[float],
    latitudes: Sequence[float],
    values_mm: Sequence[Sequence[float | None]],
    *,
    single_longitude_spacing: float | None = None,
    single_latitude_spacing: float | None = None,
) -> tuple[GridCell, ...]:
    """Build rectangular native-grid cells from ordered center coordinates."""

    original_longitudes = tuple(longitudes)
    original_latitudes = tuple(latitudes)
    rows = tuple(tuple(row) for row in values_mm)
    if len(rows) != len(original_latitudes):
        raise ValueError("values_mm deve ter uma linha por latitude")
    if any(len(row) != len(original_longitudes) for row in rows):
        raise ValueError("Cada linha de values_mm deve ter uma coluna por longitude")

    def axis_direction(axis: Sequence[float]) -> int:
        numeric = tuple(_finite_number(value, field_name="coordenada de grade") for value in axis)
        if len(numeric) <= 1:
            return 1
        if all(current > previous for previous, current in zip(numeric, numeric[1:])):
            return 1
        if all(current < previous for previous, current in zip(numeric, numeric[1:])):
            return -1
        raise ValueError("Coordenadas de grade devem ser estritamente monotonicas")

    if axis_direction(original_longitudes) < 0:
        ordered_longitudes = tuple(reversed(original_longitudes))
        rows = tuple(tuple(reversed(row)) for row in rows)
    else:
        ordered_longitudes = original_longitudes
    if axis_direction(original_latitudes) < 0:
        ordered_latitudes = tuple(reversed(original_latitudes))
        rows = tuple(reversed(rows))
    else:
        ordered_latitudes = original_latitudes

    lon_edges = _coordinate_edges(ordered_longitudes, single_spacing=single_longitude_spacing)
    lat_edges = _coordinate_edges(ordered_latitudes, single_spacing=single_latitude_spacing)
    cells: list[GridCell] = []
    for lat_index, row in enumerate(rows):
        for lon_index, value in enumerate(row):
            cells.append(
                GridCell(
                    cell_id=f"y{lat_index:04d}-x{lon_index:04d}",
                    west=lon_edges[lon_index],
                    east=lon_edges[lon_index + 1],
                    south=lat_edges[lat_index],
                    north=lat_edges[lat_index + 1],
                    value_mm=value,
                )
            )
    return tuple(cells)
