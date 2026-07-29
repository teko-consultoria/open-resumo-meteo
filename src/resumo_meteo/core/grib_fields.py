from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

SOUTH_AMERICA_DOMAIN = {
    "lat_min": -60.0,
    "lat_max": 15.0,
    "lon_min": -110.0,
    "lon_max": -20.0,
}


class WeatherFieldError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FieldSelector:
    key: str
    short_names: tuple[str, ...]
    type_of_level: str
    level: int | None = None

    def matches(self, *, short_name: str, type_of_level: str, level) -> bool:
        if short_name not in self.short_names or type_of_level != self.type_of_level:
            return False
        return self.level is None or int(level) == int(self.level)


@dataclass(frozen=True, slots=True)
class NormalizedWeatherField:
    key: str
    short_name: str
    type_of_level: str
    level: int | None
    units: str
    member_id: str
    init_time_utc: str
    valid_time_utc: str
    lead_hour: int | None
    longitudes: np.ndarray
    latitudes: np.ndarray
    values: np.ndarray

    def summary(self) -> dict:
        finite = np.isfinite(self.values)
        values = self.values[finite]
        lons = self.longitudes[finite]
        lats = self.latitudes[finite]
        if not values.size:
            return {
                "key": self.key,
                "units": self.units,
                "point_count": 0,
                "member_id": self.member_id,
                "valid_time_utc": self.valid_time_utc,
            }
        minimum_index = int(np.argmin(values))
        maximum_index = int(np.argmax(values))
        return {
            "key": self.key,
            "short_name": self.short_name,
            "type_of_level": self.type_of_level,
            "level": self.level,
            "units": self.units,
            "member_id": self.member_id,
            "init_time_utc": self.init_time_utc,
            "valid_time_utc": self.valid_time_utc,
            "lead_hour": self.lead_hour,
            "point_count": int(values.size),
            "min": float(values[minimum_index]),
            "max": float(values[maximum_index]),
            "mean": float(np.mean(values)),
            "p10": float(np.quantile(values, 0.10)),
            "p50": float(np.quantile(values, 0.50)),
            "p90": float(np.quantile(values, 0.90)),
            "min_location": {"longitude": float(lons[minimum_index]), "latitude": float(lats[minimum_index])},
            "max_location": {"longitude": float(lons[maximum_index]), "latitude": float(lats[maximum_index])},
        }


def _require_eccodes():
    os.environ.setdefault("ECCODES_PYTHON_USE_FINDLIBS", "1")
    try:
        import eccodes  # noqa: PLC0415
    except ImportError as exc:
        raise WeatherFieldError("Dependencia ecCodes indisponivel") from exc
    return eccodes


def _get(eccodes, gid, key, default=None):
    try:
        return eccodes.codes_get(gid, key)
    except Exception:
        return default


def _parse_grib_datetime(date_value, time_value) -> str:
    try:
        parsed = datetime.strptime(f"{int(date_value):08d}{int(time_value):04d}", "%Y%m%d%H%M")
    except (TypeError, ValueError):
        return ""
    return parsed.replace(tzinfo=UTC).isoformat()


def _normalized_values(values: np.ndarray, units: str, short_name: str) -> tuple[np.ndarray, str]:
    normalized_units = str(units or "").strip()
    name = str(short_name or "").lower()
    if normalized_units in {"K", "kelvin"} and name in {"2t", "t", "mx2t", "mn2t", "d2m"}:
        return values - 273.15, "degC"
    if normalized_units == "Pa" and name in {"msl", "prmsl", "sp", "pres"}:
        return values / 100.0, "hPa"
    if normalized_units in {"m of water equivalent", "m"} and name in {"tp", "apcp"}:
        return values * 1000.0, "mm"
    if normalized_units in {"m**2 s**-2", "m^2 s^-2"} and name in {"z", "gh"}:
        return values / 9.80665, "gpm"
    return values, normalized_units


def read_selected_fields(
    path: str | Path,
    *,
    selectors: tuple[FieldSelector, ...],
    domain: dict | None = None,
) -> tuple[NormalizedWeatherField, ...]:
    """Read configured GRIB messages and normalize units over South America."""

    eccodes = _require_eccodes()
    bounds = {**SOUTH_AMERICA_DOMAIN, **(domain or {})}
    fields = []
    with open(Path(path), "rb") as file_obj:
        while True:
            gid = eccodes.codes_grib_new_from_file(file_obj)
            if gid is None:
                break
            try:
                short_name = str(_get(eccodes, gid, "shortName", ""))
                type_of_level = str(_get(eccodes, gid, "typeOfLevel", ""))
                level = _get(eccodes, gid, "level")
                selector = next(
                    (
                        item
                        for item in selectors
                        if item.matches(short_name=short_name, type_of_level=type_of_level, level=level)
                    ),
                    None,
                )
                if selector is None:
                    continue
                lons = np.asarray(eccodes.codes_get_array(gid, "longitudes"), dtype=np.float64)
                lats = np.asarray(eccodes.codes_get_array(gid, "latitudes"), dtype=np.float64)
                values = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
                lons = np.where(lons > 180.0, lons - 360.0, lons)
                mask = (
                    (lons >= float(bounds["lon_min"]))
                    & (lons <= float(bounds["lon_max"]))
                    & (lats >= float(bounds["lat_min"]))
                    & (lats <= float(bounds["lat_max"]))
                )
                units = str(_get(eccodes, gid, "units", ""))
                normalized, normalized_units = _normalized_values(values[mask], units, short_name)
                number = _get(eccodes, gid, "number")
                member_id = "control" if number is None else f"member-{int(number):02d}"
                fields.append(
                    NormalizedWeatherField(
                        key=selector.key,
                        short_name=short_name,
                        type_of_level=type_of_level,
                        level=int(level) if level is not None else None,
                        units=normalized_units,
                        member_id=member_id,
                        init_time_utc=_parse_grib_datetime(
                            _get(eccodes, gid, "dataDate"),
                            _get(eccodes, gid, "dataTime"),
                        ),
                        valid_time_utc=_parse_grib_datetime(
                            _get(eccodes, gid, "validityDate"),
                            _get(eccodes, gid, "validityTime"),
                        ),
                        lead_hour=(
                            int(_get(eccodes, gid, "forecastTime"))
                            if _get(eccodes, gid, "forecastTime") is not None
                            else None
                        ),
                        longitudes=lons[mask],
                        latitudes=lats[mask],
                        values=normalized,
                    )
                )
            finally:
                eccodes.codes_release(gid)
    return tuple(fields)
