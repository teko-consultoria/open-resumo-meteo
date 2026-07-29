from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LON_MIN = -75.5
LON_MAX = -32.0
LAT_MIN = -35.0
LAT_MAX = 6.5


class GribPrecipitationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GribPrecipitationField:
    longitudes: np.ndarray
    latitudes: np.ndarray
    values_mm: np.ndarray
    units: str = ""
    message_count: int = 1

    @property
    def point_count(self) -> int:
        return int(self.values_mm.size)

    def iter_rows(self):
        for lon, lat, value in zip(
            self.longitudes, self.latitudes, self.values_mm, strict=True
        ):
            yield float(lon), float(lat), max(float(value), 0.0)


def _require_eccodes():
    os.environ.setdefault("ECCODES_PYTHON_USE_FINDLIBS", "1")
    try:
        import eccodes  # noqa: PLC0415
    except ImportError as exc:
        raise GribPrecipitationError(
            "Dependencia eccodes ausente; instale libeccodes e o pacote Python eccodes."
        ) from exc
    return eccodes


def _unit_factor(units: str, *, source_slug: str = "") -> float:
    normalized = str(units or "").strip().lower()
    if source_slug == "ecmwf" or normalized in {"m", "metre", "meter", "m of water equivalent"}:
        return 1000.0
    return 1.0


def _cropped_message_field(gid, *, eccodes, source_slug: str) -> GribPrecipitationField:
    try:
        units = str(eccodes.codes_get(gid, "units"))
    except Exception:
        units = ""
    try:
        longitudes = np.asarray(eccodes.codes_get_array(gid, "longitudes"), dtype=np.float64)
        latitudes = np.asarray(eccodes.codes_get_array(gid, "latitudes"), dtype=np.float64)
        values = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
    except Exception as exc:
        raise GribPrecipitationError("Falha extraindo arrays de precipitacao do GRIB") from exc

    longitudes = np.where(longitudes > 180.0, longitudes - 360.0, longitudes)
    mask = (
        (longitudes >= LON_MIN)
        & (longitudes <= LON_MAX)
        & (latitudes >= LAT_MIN)
        & (latitudes <= LAT_MAX)
    )
    return GribPrecipitationField(
        longitudes=longitudes[mask],
        latitudes=latitudes[mask],
        values_mm=values[mask] * _unit_factor(units, source_slug=source_slug),
        units=units,
        message_count=1,
    )


def _assert_same_grid(base: GribPrecipitationField, candidate: GribPrecipitationField):
    if base.point_count != candidate.point_count:
        raise GribPrecipitationError("Mensagens GRIB com grades diferentes apos recorte")
    if not np.allclose(base.longitudes, candidate.longitudes) or not np.allclose(base.latitudes, candidate.latitudes):
        raise GribPrecipitationError("Mensagens GRIB com coordenadas diferentes apos recorte")


def read_precipitation_field(path: str | Path, *, source_slug: str = "", aggregation: str = "sum") -> GribPrecipitationField:
    eccodes = _require_eccodes()
    path = Path(path)
    base_field = None
    accumulated_values = None
    message_count = 0
    units = ""

    with open(path, "rb") as file_obj:
        while True:
            gid = eccodes.codes_grib_new_from_file(file_obj)
            if gid is None:
                break
            try:
                field = _cropped_message_field(gid, eccodes=eccodes, source_slug=source_slug)
            finally:
                eccodes.codes_release(gid)

            if base_field is None:
                base_field = field
                accumulated_values = field.values_mm.astype(np.float64, copy=True)
                units = field.units
            else:
                _assert_same_grid(base_field, field)
                accumulated_values += field.values_mm
            message_count += 1

    if base_field is None or accumulated_values is None:
        raise GribPrecipitationError(f"Arquivo GRIB sem mensagens legiveis: {path}")

    if aggregation == "mean" and message_count:
        accumulated_values = accumulated_values / float(message_count)
    elif aggregation != "sum":
        raise ValueError(f"Agregacao GRIB desconhecida: {aggregation}")

    return GribPrecipitationField(
        longitudes=base_field.longitudes,
        latitudes=base_field.latitudes,
        values_mm=accumulated_values,
        units=units,
        message_count=message_count,
    )


def sum_precipitation_fields(fields: list[GribPrecipitationField]) -> GribPrecipitationField:
    if not fields:
        raise GribPrecipitationError("Nenhum campo GRIB para somar")
    base = fields[0]
    values = base.values_mm.astype(np.float64, copy=True)
    message_count = base.message_count
    for field in fields[1:]:
        _assert_same_grid(base, field)
        values += field.values_mm
        message_count += field.message_count
    return GribPrecipitationField(
        longitudes=base.longitudes,
        latitudes=base.latitudes,
        values_mm=values,
        units=base.units,
        message_count=message_count,
    )


def subtract_precipitation_fields(
    current: GribPrecipitationField,
    previous: GribPrecipitationField | None,
) -> GribPrecipitationField:
    if previous is None:
        values = current.values_mm.astype(np.float64, copy=True)
    else:
        _assert_same_grid(current, previous)
        values = current.values_mm - previous.values_mm
    return GribPrecipitationField(
        longitudes=current.longitudes,
        latitudes=current.latitudes,
        values_mm=np.maximum(values, 0.0),
        units=current.units,
        message_count=current.message_count,
    )
