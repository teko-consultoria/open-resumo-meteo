from __future__ import annotations

from pathlib import Path

import resumo_meteo.core as weather_summary_core
from resumo_meteo.core.basins import CANONICAL_BASIN_IDS
from resumo_meteo.core.climatology import load_climatology_bundle
from resumo_meteo.core.geometry_loader import load_geometry_catalog
from resumo_meteo.core.summary_scope import (
    DEFAULT_SUMMARY_BASIN_SCOPE,
    WEATHER_SUMMARY_BASIN_IDS,
)


def test_weather_summary_scope_matches_the_merge_climatology_contract() -> None:
    assert WEATHER_SUMMARY_BASIN_IDS == (
        "jacui",
        "uruguai",
        "iguacu",
        "parana",
        "paranapanema",
        "tiete",
        "grande",
        "paranaiba",
        "sao_francisco",
        "tocantins",
        "madeira",
        "xingu",
    )
    assert DEFAULT_SUMMARY_BASIN_SCOPE.climatology_provider == "MERGE_INPE"
    assert set(WEATHER_SUMMARY_BASIN_IDS) < set(CANONICAL_BASIN_IDS)
    climatology = load_climatology_bundle(
        Path(weather_summary_core.__file__).resolve().parent
        / "data"
        / "merge_inpe_2001_2020.climatology.json",
        expected_basin_ids=WEATHER_SUMMARY_BASIN_IDS,
    )
    assert set(climatology.data["basins"]) == set(WEATHER_SUMMARY_BASIN_IDS)
    assert len(climatology.data["basins"]) == len(WEATHER_SUMMARY_BASIN_IDS)


def test_summary_scope_uses_the_same_revised_geometries_as_the_full_catalog() -> None:
    catalog = load_geometry_catalog()

    assert len(catalog.geometries) == 27
    assert tuple(catalog.geometry(basin_id).geometry_version for basin_id in WEATHER_SUMMARY_BASIN_IDS) == (
        catalog.version,
    ) * len(WEATHER_SUMMARY_BASIN_IDS)
