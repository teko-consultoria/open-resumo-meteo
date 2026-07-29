from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest import TestCase

from resumo_meteo.core.summary_scope import WEATHER_SUMMARY_BASIN_IDS
from resumo_meteo.core.synoptic_features import detect_structured_synoptic_features
from resumo_meteo.core.synoptic_regions import (
    SYNOPTIC_REGION_IDS,
    SYNOPTIC_REGIONS,
)
from resumo_meteo.core.windows import build_forecast_windows

ISSUE_DATE = date(2026, 7, 13)


def test_synoptic_regions_reference_only_current_editorial_basins():
    referenced = {
        basin_id
        for definition in SYNOPTIC_REGIONS.values()
        for basin_id in definition["basin_ids"]
    }
    assert referenced <= set(WEATHER_SUMMARY_BASIN_IDS)


def _field(field_id: str, by_region: dict[str, float]) -> dict:
    return {
        "field_id": field_id,
        "units": "hPa" if field_id == "mean_sea_level_pressure" else "degC",
        "regional_summaries": {
            region_id: {
                "p50": float(by_region.get(region_id, 20.0)),
                "mean": float(by_region.get(region_id, 20.0)),
            }
            for region_id in SYNOPTIC_REGION_IDS
        },
    }


def _input(model: str, *, pressure_offset: float = 0.0) -> dict:
    timeline = []
    south_pressure = (1023, 1024, 1023, 1022, 1020, 1018, 1016, 1014, 1011, 1008)
    southeast_pressure = (1020, 1022, 1024, 1024, 1023, 1021, 1019, 1017, 1015, 1012)
    plata_pressure = (1017, 1017, 1017, 1017, 1016, 1016, 1015, 1014, 1004, 999)
    south_t850 = (7, 8, 10, 12, 14, 16, 18, 19, 20, 19)
    south_t2m = (10, 11, 13, 15, 17, 18, 20, 21, 22, 21)
    south_v850 = (4, 3, 2, 1, 0, -1, -2, -3, -4, -3)
    south_rh = (76, 67, 58, 54, 52, 55, 58, 62, 68, 72)
    ocean_rh = (70, 73, 76, 78, 75, 72, 70, 69, 72, 75)
    for index, lead_hour in enumerate(range(0, 120, 12)):
        pressure = {region_id: 1015.0 for region_id in SYNOPTIC_REGION_IDS}
        pressure.update(
            {
                "south_brazil": south_pressure[index] + pressure_offset,
                "southeast_brazil": southeast_pressure[index] + pressure_offset,
                "la_plata": plata_pressure[index] + pressure_offset,
            }
        )
        temperature_850 = {region_id: 18.0 for region_id in SYNOPTIC_REGION_IDS}
        temperature_850["south_brazil"] = south_t850[index]
        temperature_2m = {region_id: 23.0 for region_id in SYNOPTIC_REGION_IDS}
        temperature_2m["south_brazil"] = south_t2m[index]
        meridional_wind = {region_id: 0.0 for region_id in SYNOPTIC_REGION_IDS}
        meridional_wind["south_brazil"] = south_v850[index]
        humidity = {region_id: 60.0 for region_id in SYNOPTIC_REGION_IDS}
        humidity["south_brazil"] = south_rh[index]
        humidity["south_atlantic"] = ocean_rh[index]
        timeline.append(
            {
                "lead_hour": lead_hour,
                "valid_time": datetime.combine(ISSUE_DATE, datetime.min.time(), tzinfo=UTC)
                + timedelta(hours=lead_hour),
                "window_id": "p1",
                "fields": [
                    _field("mean_sea_level_pressure", pressure),
                    _field("temperature_850", temperature_850),
                    _field("temperature_2m", temperature_2m),
                    _field("wind_v_850", meridional_wind),
                    _field("relative_humidity_850", humidity),
                ],
            }
        )
    return {
        "model": model,
        "family": "ncep" if model == "gfs" else "ecmwf",
        "status": "complete",
        "timeline": timeline,
        "available_field_ids": [
            "mean_sea_level_pressure",
            "temperature_850",
            "temperature_2m",
            "wind_v_850",
            "relative_humidity_850",
        ],
    }


def _with_active_p2_low(model: str, *, pressure_offset: float = 0.0) -> dict:
    result = _input(model, pressure_offset=pressure_offset)
    south_pressure = (1006, 1006, 1005, 1008, 1009, 1008, 1008, 1013, 1017, 1019)
    meridional_wind = (-15, -14, -12, -10, -9, -8, -6, -3, 0, -2)
    humidity = (70, 72, 74, 68, 66, 70, 65, 58, 48, 45)
    for index, lead_hour in enumerate(range(120, 240, 12)):
        pressure = {region_id: 1015.0 for region_id in SYNOPTIC_REGION_IDS}
        pressure["south_brazil"] = south_pressure[index] + pressure_offset
        wind = {region_id: 0.0 for region_id in SYNOPTIC_REGION_IDS}
        wind["south_brazil"] = meridional_wind[index]
        relative_humidity = {
            region_id: 55.0 for region_id in SYNOPTIC_REGION_IDS
        }
        relative_humidity["south_brazil"] = humidity[index]
        result["timeline"].append(
            {
                "lead_hour": lead_hour,
                "valid_time": datetime.combine(
                    ISSUE_DATE,
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                + timedelta(hours=lead_hour),
                "window_id": "p2",
                "fields": [
                    _field("mean_sea_level_pressure", pressure),
                    _field("wind_v_850", wind),
                    _field("relative_humidity_850", relative_humidity),
                ],
            }
        )
    return result


class StructuredSynopticFeatureTests(TestCase):
    def test_two_families_promote_causal_p1_sequence(self):
        features, temperature, warnings = detect_structured_synoptic_features(
            {
                "gfs": _input("gfs"),
                "ec": _input("ec", pressure_offset=0.5),
            },
            forecast_windows=build_forecast_windows(ISSUE_DATE),
            model_source_ids={
                "gfs": "noaa-gfs-20260713-00",
                "ec": "ecmwf-ec-20260713-00",
            },
        )

        feature_types = {item["feature_type"] for item in features["p1"]}
        self.assertIn("post_frontal_high", feature_types)
        self.assertIn("cold_front", feature_types)
        self.assertIn("deepening_low", feature_types)
        self.assertTrue(all(item["confidence"] == "high" for item in features["p1"]))
        developing_low = next(
            item for item in features["p1"] if item["feature_type"] == "deepening_low"
        )
        self.assertEqual(developing_low["continues_into"], "p2")
        self.assertEqual(
            developing_low["start_utc"],
            datetime(2026, 7, 17, tzinfo=UTC),
        )
        south_temperature = next(
            item for item in temperature["p1"] if item["region_id"] == "south_brazil"
        )
        self.assertEqual(south_temperature["start_regime"], "below_average")
        self.assertEqual(south_temperature["trend"], "warming")
        self.assertEqual(warnings, [])

    def test_one_family_does_not_name_a_synoptic_mechanism(self):
        features, temperature, warnings = detect_structured_synoptic_features(
            {"gfs": _input("gfs")},
            forecast_windows=build_forecast_windows(ISSUE_DATE),
            model_source_ids={"gfs": "noaa-gfs-20260713-00"},
        )

        self.assertEqual(features["p1"], [])
        self.assertTrue(temperature["p1"])
        self.assertTrue(warnings)

    def test_active_low_is_carried_into_p2_and_weakens(self):
        features, _temperature, _warnings = detect_structured_synoptic_features(
            {
                "gfs": _with_active_p2_low("gfs"),
                "ec": _with_active_p2_low("ec", pressure_offset=0.3),
            },
            forecast_windows=build_forecast_windows(ISSUE_DATE),
            model_source_ids={
                "gfs": "noaa-gfs-20260713-00",
                "ec": "ecmwf-ec-20260713-00",
            },
        )

        active_low = next(
            item for item in features["p2"] if item["feature_type"] == "low_pressure"
        )
        self.assertEqual(active_low["lifecycle"], "weakening")
        self.assertEqual(active_low["confidence"], "high")
