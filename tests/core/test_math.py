from __future__ import annotations

import json
import math
import unittest
from datetime import UTC, date, datetime

from resumo_meteo.core.basins import (
    CANONICAL_BASIN_IDS,
    CANONICAL_BASIN_NAMES,
    DEFAULT_BASIN_REGISTRY,
    format_basin_editorial_reference,
)
from resumo_meteo.core.consensus import ModelEstimate, compute_two_family_consensus
from resumo_meteo.core.constants import (
    AUTHORIZED_MODELS,
    EDITORIAL_CYCLE_UTC,
    MODEL_FAMILY_BY_MODEL,
    normalize_model_id,
    require_editorial_cycle,
)
from resumo_meteo.core.ensemble import ensemble_statistics, quantile
from resumo_meteo.core.horizons import (
    model_supports_window,
)
from resumo_meteo.core.spatial import (
    BasinGeometry,
    GridCell,
    aggregate_basin_precipitation,
    compute_basin_spatial_weights,
    grid_cells_from_centers,
)
from resumo_meteo.core.windows import build_forecast_windows


class AuthorizedModelContractTests(unittest.TestCase):
    def test_only_four_authorized_models_in_two_independent_families(self):
        self.assertEqual(AUTHORIZED_MODELS, {"gfs", "gefs", "ec", "ecens"})
        self.assertEqual(
            MODEL_FAMILY_BY_MODEL,
            {"gfs": "ncep", "gefs": "ncep", "ec": "ecmwf", "ecens": "ecmwf"},
        )
        with self.assertRaisesRegex(ValueError, "nao autorizado"):
            normalize_model_id("icon")
        with self.assertRaisesRegex(ValueError, "nao autorizado"):
            normalize_model_id("custom-model")

    def test_editorial_product_is_restricted_to_one_00z_cycle(self):
        self.assertEqual(EDITORIAL_CYCLE_UTC, "00")
        self.assertEqual(require_editorial_cycle("0z"), "00")
        self.assertEqual(require_editorial_cycle(0), "00")
        with self.assertRaisesRegex(ValueError, "exclusivamente"):
            require_editorial_cycle("12")


class ForecastWindowTests(unittest.TestCase):
    def test_windows_are_utc_semi_open_three_complete_pentads(self):
        windows = build_forecast_windows(date(2026, 7, 10))

        self.assertEqual(tuple(window.duration_days for window in windows), (5, 5, 4))
        self.assertEqual(windows[0].start_utc, datetime(2026, 7, 10, 0, tzinfo=UTC))
        self.assertEqual(windows[-1].end_utc, datetime(2026, 7, 24, 0, tzinfo=UTC))
        self.assertEqual(windows[0].end_utc, windows[1].start_utc)
        self.assertEqual(windows[1].end_utc, windows[2].start_utc)
        json.dumps([window.to_dict() for window in windows])

    def test_naive_anchor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            build_forecast_windows(datetime(2026, 7, 10, 12))


class EnsembleStatisticsTests(unittest.TestCase):
    def test_quantiles_probabilities_and_spread_use_all_valid_members(self):
        values = [0, 10, 20, 30, 40]
        statistics = ensemble_statistics(values, expected_member_count=5)

        self.assertEqual(quantile(values, 0.10), 4)
        self.assertEqual(statistics.p25_mm, 10)
        self.assertEqual(statistics.p50_mm, 20)
        self.assertEqual(statistics.p75_mm, 30)
        self.assertEqual(statistics.p90_mm, 36)
        self.assertEqual(statistics.spread_p90_p10_mm, 32)
        self.assertAlmostEqual(statistics.probability_gt(25), 0.4)
        self.assertAlmostEqual(statistics.standard_deviation_mm, math.sqrt(200))
        json.dumps(statistics.to_dict())

    def test_missing_members_are_excluded_but_completeness_is_explicit(self):
        statistics = ensemble_statistics([0, None, 20], expected_member_count=5)

        self.assertEqual(statistics.valid_member_count, 2)
        self.assertEqual(statistics.invalid_member_count, 1)
        self.assertEqual(statistics.completeness_fraction, 0.4)
        self.assertIn("incomplete_ensemble", statistics.quality_flags)
        self.assertIn("invalid_or_missing_members", statistics.quality_flags)


class TwoFamilyConsensusTests(unittest.TestCase):
    def test_consensus_uses_one_representative_per_family(self):
        consensus = compute_two_family_consensus(
            {
                "gfs": 0,
                "gefs": ModelEstimate(central_mm=20, lower_mm=10, upper_mm=30),
                "ec": 100,
                "ecens": ModelEstimate(central_mm=40, lower_mm=30, upper_mm=50),
            }
        )

        self.assertEqual(consensus.independent_family_count, 2)
        self.assertEqual(consensus.contributing_models, ("gefs", "ecens"))
        # (20 + 40) / 2, not a four-product vote and not an internal family mean.
        self.assertEqual(consensus.opinion_mm, 30)
        self.assertIn("gfs_outside_gefs_iqr", consensus.quality_flags)
        self.assertIn("ec_outside_ecens_iqr", consensus.quality_flags)
        json.dumps(consensus.to_dict())

    def test_missing_ensemble_is_an_explicit_degraded_fallback(self):
        consensus = compute_two_family_consensus({"gfs": 20, "ecens": 40})
        self.assertEqual(consensus.opinion_mm, 30)
        self.assertIn("ncep_ensemble_missing", consensus.quality_flags)


class ModelHorizonTests(unittest.TestCase):
    def test_ec_is_never_extrapolated_beyond_240_hours(self):
        self.assertTrue(model_supports_window("ec", start_lead_hour=120, end_lead_hour=240))
        self.assertFalse(model_supports_window("ec", start_lead_hour=240, end_lead_hour=336))
        self.assertTrue(model_supports_window("gfs", start_lead_hour=240, end_lead_hour=336))
        self.assertTrue(model_supports_window("gefs", start_lead_hour=240, end_lead_hour=336))
        self.assertTrue(model_supports_window("ecens", start_lead_hour=240, end_lead_hour=336))


class CanonicalBasinRegistryTests(unittest.TestCase):
    def test_registry_has_exactly_the_27_reviewed_smap_ids_and_names(self):
        self.assertEqual(len(DEFAULT_BASIN_REGISTRY.basins), 27)
        self.assertEqual(len(CANONICAL_BASIN_IDS), 27)
        self.assertIn("São Francisco", CANONICAL_BASIN_NAMES)
        json.dumps(DEFAULT_BASIN_REGISTRY.to_dict(), ensure_ascii=False)

    def test_sector_editorial_reference_is_catalog_driven_for_all_basins(self):
        self.assertEqual(DEFAULT_BASIN_REGISTRY.schema_version, 4)
        self.assertEqual(
            tuple(basin.locative_reference for basin in DEFAULT_BASIN_REGISTRY.basins),
            tuple(
                format_basin_editorial_reference(basin.basin_id)
                for basin in DEFAULT_BASIN_REGISTRY.basins
            ),
        )
        self.assertEqual(format_basin_editorial_reference("uruguai"), "no Uruguai")
        self.assertEqual(
            format_basin_editorial_reference("sao_francisco", preposition="de"),
            "do São Francisco",
        )
        self.assertEqual(
            format_basin_editorial_reference("tiete", preposition="a"),
            "ao Tietê",
        )
        with self.assertRaisesRegex(ValueError, "Preposicao editorial"):
            format_basin_editorial_reference("xingu", preposition="por")

    def test_registry_exposes_all_27_current_contours(self):
        uruguai = DEFAULT_BASIN_REGISTRY.require_id("uruguai")
        parana = DEFAULT_BASIN_REGISTRY.require_id("parana")
        tocantins = DEFAULT_BASIN_REGISTRY.require_id("tocantins")

        self.assertTrue(uruguai.geometry_path.endswith("uruguai.geojson"))
        self.assertTrue(parana.geometry_path.endswith("parana.geojson"))
        self.assertTrue(tocantins.geometry_path.endswith("tocantins.geojson"))


class BasinSpatialAggregationTests(unittest.TestCase):
    def setUp(self):
        self.geometry = BasinGeometry.from_polygon(
            basin_id="jacui",
            exterior=((0, 0), (2, 0), (2, 1), (0, 1)),
            geometry_version="synthetic-test-v1",
            crs="TEST:PLANAR",
        )

    def test_area_intersection_weights_basin_mean_and_keeps_point_max_separate(self):
        # The first cell contributes 25% of the basin and the second 75%. A
        # centroid-only mask cannot express this deliberately partial overlap.
        cells = (
            GridCell("dry", west=-1, east=0.5, south=0, north=1, value_mm=0),
            GridCell("wet", west=0.5, east=2.5, south=0, north=1, value_mm=100),
        )
        weights = compute_basin_spatial_weights(self.geometry, cells, area_method="planar")
        aggregation = aggregate_basin_precipitation(self.geometry, cells, area_method="planar")

        self.assertEqual(tuple(weight.intersection_area for weight in weights.cell_weights), (0.5, 1.5))
        self.assertEqual(weights.grid_coverage_fraction, 1)
        self.assertEqual(aggregation.basin_mean_mm, 75)
        self.assertEqual(aggregation.point_max_mm, 100)
        self.assertEqual(aggregation.point_max_cell_id, "wet")
        self.assertNotEqual(aggregation.basin_mean_mm, aggregation.point_max_mm)
        self.assertEqual(aggregation.valid_data_coverage_fraction, 1)
        self.assertEqual(aggregation.area_above_threshold_fraction["gt_50_mm"], 0.75)
        json.dumps(aggregation.to_dict())

    def test_missing_cell_value_reduces_coverage_without_changing_mean_semantics(self):
        cells = (
            GridCell("valid", west=-1, east=0.5, south=0, north=1, value_mm=20),
            GridCell("missing", west=0.5, east=2.5, south=0, north=1, value_mm=None),
        )
        aggregation = aggregate_basin_precipitation(self.geometry, cells, area_method="planar")

        self.assertEqual(aggregation.grid_coverage_fraction, 1)
        self.assertEqual(aggregation.valid_data_coverage_fraction, 0.25)
        self.assertEqual(aggregation.basin_mean_mm, 20)
        self.assertEqual(aggregation.point_max_mm, 20)
        self.assertIn("insufficient_valid_data_coverage", aggregation.quality_flags)

    def test_hole_is_subtracted_from_cell_intersection_area(self):
        geometry = BasinGeometry.from_polygon(
            basin_id="jacui",
            exterior=((0, 0), (2, 0), (2, 2), (0, 2)),
            holes=(((0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)),),
            geometry_version="synthetic-hole-v1",
            crs="TEST:PLANAR",
        )
        cell = GridCell("all", west=0, east=2, south=0, north=2, value_mm=10)
        weights = compute_basin_spatial_weights(geometry, (cell,), area_method="planar")

        self.assertEqual(weights.basin_area, 3)
        self.assertEqual(weights.grid_intersection_area, 3)
        self.assertEqual(weights.grid_coverage_fraction, 1)

    def test_descending_native_grid_axes_are_reordered_with_their_values(self):
        cells = grid_cells_from_centers(
            longitudes=(1.5, 0.5),
            latitudes=(0.75, 0.25),
            values_mm=((40, 30), (20, 10)),
        )

        self.assertEqual(
            tuple(cell.value_mm for cell in cells),
            (10, 20, 30, 40),
        )
        self.assertEqual((cells[0].west, cells[0].south), (0, 0))


if __name__ == "__main__":
    unittest.main()
