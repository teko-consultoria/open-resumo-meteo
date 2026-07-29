from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import resumo_meteo.core as weather_summary_core
from resumo_meteo.core.climatology import (
    CANONICAL_BASIN_DEFINITION_VERSION,
    ClimatologyError,
    IncompleteClimatologyError,
    build_calendar_window_climatology,
    load_climatology_bundle,
    normalize_daily_basin_records,
    write_climatology_bundle_atomic,
)

DATA_DIR = Path(weather_summary_core.__file__).resolve().parent / "data"
MERGE_BUNDLE_PATH = DATA_DIR / "merge_inpe_2001_2020.climatology.json"
MERGE_MANIFEST_PATH = DATA_DIR / "merge_inpe_2001_2020.manifest.json"


def _daily_records(
    start: date,
    end: date,
    *,
    basin_id: str = "uruguai",
) -> list[dict]:
    records: list[dict] = []
    current = start
    while current <= end:
        records.append(
            {
                "basin_id": basin_id,
                "date": current.isoformat(),
                "mm": float(current.year - 1999),
                "coverage_pct": 100.0,
            }
        )
        current += timedelta(days=1)
    return records


def _build(records: list[dict], *, minimum_complete_years: int = 5) -> dict:
    return build_calendar_window_climatology(
        records,
        climatology_id="imerg-calendar-v2",
        version="2.0.0",
        reference_period="2000-2024",
        basin_definition_version="test-contours@current",
        minimum_complete_years=minimum_complete_years,
        provider="GPM_IMERG",
        source_product="GPM_3IMERGDL",
        source_mask_version="test-contours@current",
        minimum_daily_coverage_pct=95.0,
        expected_basin_ids=("uruguai",),
    )


class IMERGClimatologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = _daily_records(date(2000, 1, 1), date(2024, 12, 31))
        cls.bundle = _build(cls.records)

    def test_calculates_r7_quantiles_for_five_and_four_day_windows(self):
        five_days = self.bundle["basins"]["uruguai"]["windows"]["01-01:5"]
        four_days = self.bundle["basins"]["uruguai"]["windows"]["01-01:4"]

        self.assertEqual(five_days["sample_count"], 25)
        self.assertAlmostEqual(five_days["mean_mm"], 65.0)
        self.assertAlmostEqual(five_days["median_mm"], 65.0)
        self.assertAlmostEqual(five_days["p10_mm"], 17.0)
        self.assertAlmostEqual(five_days["p25_mm"], 35.0)
        self.assertAlmostEqual(five_days["p75_mm"], 95.0)
        self.assertAlmostEqual(five_days["p90_mm"], 113.0)

        self.assertEqual(four_days["sample_count"], 25)
        self.assertAlmostEqual(four_days["mean_mm"], 52.0)
        self.assertAlmostEqual(four_days["median_mm"], 52.0)
        self.assertAlmostEqual(four_days["p10_mm"], 13.6)
        self.assertAlmostEqual(four_days["p25_mm"], 28.0)
        self.assertAlmostEqual(four_days["p75_mm"], 76.0)
        self.assertAlmostEqual(four_days["p90_mm"], 90.4)
        self.assertEqual(self.bundle["durations_days"], [4, 5])
        self.assertEqual(self.bundle["quality"]["calendar_windows_per_basin"], 732)
        self.assertEqual(self.bundle["schema_version"], 2)

    def test_source_neutral_builder_sums_raw_days_before_distribution(self):
        records = [
            {key: value for key, value in item.items() if key != "coverage_pct"}
            for item in self.records
        ]
        bundle = build_calendar_window_climatology(
            records,
            climatology_id="merge-test-v1",
            version="1.0.0",
            reference_period="2000-2024",
            basin_definition_version="canonical-basins@test",
            minimum_complete_years=5,
            provider="MERGE_INPE",
            source_product="MERGE_DAILY_BASIN",
            source_path="MERGE_Daily_basin_historical.csv",
            source_sha256="a" * 64,
            source_mask_limitation="Mascara de origem nao publicada.",
            expected_basin_ids=("uruguai",),
        )

        five_days = bundle["basins"]["uruguai"]["windows"]["01-01:5"]
        self.assertEqual(five_days["mean_mm"], 65.0)
        self.assertEqual(bundle["provider"], "MERGE_INPE")
        self.assertEqual(bundle["source_product"], "MERGE_DAILY_BASIN")
        self.assertIsNone(bundle["minimum_daily_coverage_pct"])
        self.assertEqual(
            bundle["provenance"]["distribution_rule"],
            "sum_raw_daily_values_then_compute_distribution",
        )
        self.assertFalse(bundle["provenance"]["daily_percentiles_summed"])
        self.assertNotIn("samples", five_days)
        self.assertNotIn("sample_years", five_days)
        self.assertNotIn("excluded_years", five_days)

    def test_leap_day_uses_only_real_leap_year_samples(self):
        window = self.bundle["basins"]["uruguai"]["windows"]["02-29:4"]

        self.assertEqual(window["sample_count"], 7)
        invalid_years = [
            item["year"]
            for item in window["excluded_years"]
            if item["reason"] == "invalid_calendar_date"
        ]
        self.assertEqual(len(invalid_years), 18)
        self.assertNotIn(2000, invalid_years)
        self.assertIn(2001, invalid_years)

    def test_low_coverage_year_is_excluded_but_not_filled(self):
        records = [dict(item) for item in self.records]
        target = next(
            item
            for item in records
            if item["date"] == "2005-01-03" and item["basin_id"] == "uruguai"
        )
        target["coverage_pct"] = 80.0

        bundle = _build(records)
        window = bundle["basins"]["uruguai"]["windows"]["01-01:5"]

        self.assertEqual(window["sample_count"], 24)
        exclusion = next(item for item in window["excluded_years"] if item["year"] == 2005)
        self.assertEqual(exclusion["reason"], "coverage_below_threshold")
        self.assertEqual(exclusion["low_coverage_dates"], ["2005-01-03"])

    def test_missing_days_fail_when_minimum_complete_years_is_not_met(self):
        records = [
            item
            for item in self.records
            if not (
                item["date"].endswith("-01-03")
                and int(item["date"][:4]) <= 2020
            )
        ]

        with self.assertRaises(IncompleteClimatologyError) as captured:
            _build(records)

        failure = next(
            item
            for item in captured.exception.failures
            if item["basin_id"] == "uruguai" and item["window_key"] == "01-01:5"
        )
        self.assertEqual(failure["sample_count"], 4)
        self.assertEqual(failure["minimum_complete_years"], 5)
        missing = next(item for item in failure["excluded_years"] if item["year"] == 2000)
        self.assertEqual(missing["reason"], "missing_days")
        self.assertEqual(missing["missing_dates"], ["2000-01-03"])

    def test_loader_validates_bundle_and_reports_exact_file_sha(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "climatology.json"
            write_climatology_bundle_atomic(self.bundle, path)
            raw = path.read_bytes()

            loaded = load_climatology_bundle(path, expected_basin_ids=("uruguai",))

        self.assertEqual(loaded.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(loaded.reference_period, "2000-2024")
        self.assertEqual(loaded.source_product, "GPM_3IMERGDL")
        self.assertEqual(
            loaded.basin_window("uruguai", start_date=date(2026, 1, 1), duration_days=5)[
                "median_mm"
            ],
            65.0,
        )

    def test_normalization_rejects_missing_coverage_and_duplicates(self):
        with self.assertRaisesRegex(ClimatologyError, "coverage_pct"):
            normalize_daily_basin_records(
                [{"basin_id": "uruguai", "date": "2020-01-01", "mm": 2.0}],
                require_coverage=True,
            )
        duplicate = {
            "basin_id": "uruguai",
            "date": "2020-01-01",
            "mm": 2.0,
            "coverage_pct": 100.0,
        }
        with self.assertRaisesRegex(ClimatologyError, "duplicado"):
            normalize_daily_basin_records(
                [duplicate, duplicate],
                require_coverage=True,
            )

        generic = normalize_daily_basin_records(
            [{"basin_id": "uruguai", "date": "2020-01-01", "mm": 2.0}]
        )
        self.assertIsNone(generic[0].coverage_pct)

    def test_loader_rejects_retired_bundle_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "climatology.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "climatology_id": "retired",
                        "version": "1",
                        "reference_period": "2000-2020",
                        "observed_product": "GPM_3IMERGDL",
                        "basin_geometry_version": "retired",
                        "basins": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ClimatologyError, "nao suportada"):
                load_climatology_bundle(path)


class OperationalMERGEClimatologyAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle_bytes = MERGE_BUNDLE_PATH.read_bytes()
        cls.bundle = load_climatology_bundle(MERGE_BUNDLE_PATH)
        cls.manifest_bytes = MERGE_MANIFEST_PATH.read_bytes()
        cls.manifest = json.loads(cls.manifest_bytes.decode("utf-8"))

    def test_static_bundle_has_fixed_complete_reference_and_12_summary_basins(self):
        self.assertEqual(self.bundle.provider, "MERGE_INPE")
        self.assertEqual(self.bundle.source_product, "MERGE_DAILY_BASIN")
        self.assertEqual(self.bundle.reference_period, "2001-2020")
        self.assertEqual(
            self.bundle.basin_definition_version,
            "MERGE_DAILY_BASIN@historical-19-area-weighted-v2",
        )
        self.assertNotEqual(self.bundle.basin_definition_version, CANONICAL_BASIN_DEFINITION_VERSION)
        self.assertEqual(len(self.bundle.data["basins"]), 12)
        self.assertEqual(self.bundle.data["quality"]["used_daily_records"], 87660)
        self.assertEqual(
            self.bundle.data["quality"]["calendar_windows_per_basin"],
            732,
        )
        for basin in self.bundle.data["basins"].values():
            self.assertEqual(len(basin["windows"]), 732)

    def test_calendar_samples_cover_leap_and_end_boundaries_without_daily_percentiles(self):
        windows = self.bundle.data["basins"]["uruguai"]["windows"]
        self.assertEqual(windows["01-01:5"]["sample_count"], 20)
        self.assertEqual(windows["02-29:4"]["sample_count"], 5)
        self.assertEqual(windows["12-31:5"]["sample_count"], 19)
        self.assertFalse(self.bundle.data["provenance"]["daily_percentiles_summed"])
        self.assertEqual(
            self.bundle.data["provenance"]["distribution_rule"],
            "sum_raw_daily_values_then_compute_distribution",
        )

    def test_manifest_pins_source_and_bundle_checksums_without_runtime_endpoint(self):
        self.assertEqual(
            self.manifest["source"]["sha256"],
            "987ec12cd4911feef7285d10724e7b46d091ff677061928223335b12c5e44013",
        )
        self.assertEqual(
            self.manifest["bundle"]["sha256"],
            hashlib.sha256(self.bundle_bytes).hexdigest(),
        )
        self.assertEqual(self.manifest["source"]["reference_day_count"], 7305)
        self.assertEqual(self.manifest["validation"]["basin_count"], 12)
        self.assertNotIn("url", self.manifest["source"])
        self.assertNotIn("token", self.manifest["source"])
        self.assertIsNone(self.bundle.data.get("source_mask_version"))
        self.assertTrue(self.bundle.source_mask_limitation)

if __name__ == "__main__":
    unittest.main()
