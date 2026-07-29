"""Canonical regions used by the structured synoptic diagnostics.

The boxes deliberately extend beyond Brazil.  Systems affecting the SIN often
form over La Plata/Chaco or move through the adjacent South Atlantic before
their rainfall signal reaches a canonical basin.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .summary_scope import WEATHER_SUMMARY_BASIN_IDS

SYNOPTIC_REGIONS: dict[str, dict[str, object]] = {
    "amazon_west": {
        "label": "o oeste da Amazônia",
        "bounds": (-75.0, -55.0, -20.0, 5.0),
        "basin_ids": ("madeira", "xingu"),
    },
    "central_brazil": {
        "label": "o Brasil central",
        "bounds": (-60.0, -45.0, -20.0, -5.0),
        "basin_ids": ("tocantins", "paranaiba"),
    },
    "southeast_brazil": {
        "label": "o Sudeste",
        "bounds": (-53.0, -39.0, -26.0, -15.0),
        "basin_ids": (
            "tiete",
            "paranapanema",
            "grande",
            "paranaiba",
            "sao_francisco",
        ),
    },
    "south_brazil": {
        "label": "o Sul",
        "bounds": (-58.0, -47.0, -35.0, -22.0),
        "basin_ids": ("jacui", "uruguai", "iguacu", "parana"),
    },
    "northeast_brazil": {
        "label": "o Nordeste",
        "bounds": (-48.0, -34.0, -18.0, -2.0),
        "basin_ids": ("sao_francisco",),
    },
    "la_plata": {
        "label": "a região do Prata",
        "bounds": (-65.0, -52.0, -38.0, -27.0),
        "basin_ids": ("jacui", "uruguai", "iguacu", "parana"),
    },
    "chaco": {
        "label": "o Chaco e o norte da Argentina",
        "bounds": (-65.0, -53.0, -30.0, -18.0),
        "basin_ids": ("uruguai", "iguacu", "parana"),
    },
    "south_atlantic": {
        "label": "o Atlântico Sul adjacente",
        "bounds": (-52.0, -32.0, -42.0, -22.0),
        "basin_ids": ("jacui", "uruguai", "iguacu"),
    },
}

SYNOPTIC_REGION_IDS = tuple(SYNOPTIC_REGIONS)
_UNKNOWN_REGION_BASINS = {
    str(basin_id)
    for definition in SYNOPTIC_REGIONS.values()
    for basin_id in definition["basin_ids"]
    if str(basin_id) not in WEATHER_SUMMARY_BASIN_IDS
}
if _UNKNOWN_REGION_BASINS:
    raise ValueError(
        "regiões sinóticas possuem bacias fora do escopo editorial: "
        f"{sorted(_UNKNOWN_REGION_BASINS)}"
    )

TEMPERATURE_CLIMATOLOGY_PATH = (
    Path(__file__).with_name("data") / "noaa_ncep_2m_temperature_1991_2020.json"
)


def region_bounds() -> dict[str, dict[str, float]]:
    return {
        region_id: {
            "lon_min": float(definition["bounds"][0]),
            "lon_max": float(definition["bounds"][1]),
            "lat_min": float(definition["bounds"][2]),
            "lat_max": float(definition["bounds"][3]),
        }
        for region_id, definition in SYNOPTIC_REGIONS.items()
    }


def region_label(region_id: str) -> str:
    return str(SYNOPTIC_REGIONS[region_id]["label"])


def region_basin_ids(region_id: str) -> tuple[str, ...]:
    return tuple(str(value) for value in SYNOPTIC_REGIONS[region_id]["basin_ids"])


@lru_cache(maxsize=1)
def load_temperature_climatology() -> dict:
    payload = json.loads(TEMPERATURE_CLIMATOLOGY_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "weather-summary-temperature-climatology.v1":
        raise ValueError("climatologia regional de temperatura possui schema inválido")
    monthly = payload.get("monthly_region_mean_degc")
    if not isinstance(monthly, dict) or set(monthly) != {
        str(month) for month in range(1, 13)
    }:
        raise ValueError("climatologia regional de temperatura não cobre os 12 meses")
    for month in monthly.values():
        if not isinstance(month, dict) or set(month) != set(SYNOPTIC_REGION_IDS):
            raise ValueError("climatologia regional de temperatura diverge das regiões")
    return payload


def temperature_normal_degc(month: int, region_id: str) -> float:
    payload = load_temperature_climatology()
    return float(payload["monthly_region_mean_degc"][str(int(month))][region_id])
