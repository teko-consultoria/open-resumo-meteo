from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import TestCase

import resumo_meteo.core as weather_summary_core
from resumo_meteo.core.basins import (
    CANONICAL_BASIN_IDS,
    CANONICAL_BASIN_NAME_BY_ID,
    DEFAULT_BASIN_REGISTRY,
)
from resumo_meteo.core.geometry_loader import load_geometry_catalog

CORE_ROOT = Path(weather_summary_core.__file__).resolve().parent
ASSET_ROOT = CORE_ROOT / "assets" / "basins" / "v2"
MANIFEST_PATH = ASSET_ROOT / "manifest.json"
OPERATIONAL_CONTOUR_SET_SHA256 = (
    "44c3948fbf27b0394be3cca142ff4a92fd94adfe530a3fe24be7650758b03049"
)
ALIGNED_AREA_KM2 = {
    "grande": 139_567.671375,
    "iguacu": 61_731.277869,
    "jacui": 36_261.286102,
    "madeira": 979_670.166561,
    "paraiba_do_sul": 39_821.760739,
    "parana": 320_792.174426,
    "paranaiba": 201_216.118221,
    "tocantins": 762_590.713920,
    "uruguai": 65_960.426688,
}


class CanonicalBasinGeometryTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_manifest_and_registry_cover_exactly_the_canonical_basins(self):
        self.assertEqual(
            set(self.manifest["geometries"]),
            set(CANONICAL_BASIN_IDS),
        )
        self.assertEqual(
            tuple(basin.basin_id for basin in DEFAULT_BASIN_REGISTRY.basins),
            CANONICAL_BASIN_IDS,
        )
        self.assertFalse(self.manifest["runtime_dependency_on_source_repository"])

    def test_every_materialized_feature_has_matching_identity_and_checksum(self):
        hashes = set()
        for basin in DEFAULT_BASIN_REGISTRY.basins:
            entry = self.manifest["geometries"][basin.basin_id]
            self.assertEqual(basin.geometry_path, entry["path"])
            self.assertIn(
                f"#geometries.{basin.basin_id}",
                basin.geometry_source_reference,
            )

            path = (CORE_ROOT / basin.geometry_path).resolve()
            path.relative_to(CORE_ROOT)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            self.assertEqual(digest, entry["sha256"])
            self.assertNotIn(digest, hashes)
            hashes.add(digest)

            feature = json.loads(content)
            self.assertEqual(feature["type"], "Feature")
            self.assertEqual(feature["properties"]["basin_id"], basin.basin_id)
            self.assertEqual(
                feature["properties"]["canonical_name"],
                CANONICAL_BASIN_NAME_BY_ID[basin.basin_id],
            )
            self.assertEqual(
                feature["properties"]["geometry_version"],
                self.manifest["geometry_version"],
            )
            self.assertIn(feature["geometry"]["type"], {"Polygon", "MultiPolygon"})
            self.assertEqual(entry["source_basin_name"], feature["properties"]["source_basin_name"])

    def test_dependency_free_loader_exposes_positive_areas_for_all_basins(self):
        catalog = load_geometry_catalog()

        self.assertEqual(tuple(catalog.geometries), CANONICAL_BASIN_IDS)
        self.assertEqual(catalog.missing_basin_ids, ())
        self.assertEqual(set(catalog.area_km2), set(CANONICAL_BASIN_IDS))
        self.assertTrue(all(area > 0 for area in catalog.area_km2.values()))

    def test_manifest_pins_the_operational_gridded_map_contours(self):
        self.assertEqual(self.manifest["catalog_id"], "smap-aggregated-basins-apr2025")
        self.assertEqual(self.manifest["catalog_version"], "2.0.0")
        self.assertEqual(
            self.manifest["geometry_version"],
            "smap-aggregated-apr2025-grid-v2",
        )
        self.assertEqual(self.manifest["source"]["contour_set_id"], "smap_abr2025")
        self.assertEqual(
            self.manifest["source"]["source_kind"],
            "operational_gridded_weather_shapefiles",
        )
        self.assertEqual(
            self.manifest["source"]["source_shapefile_set_sha256"],
            OPERATIONAL_CONTOUR_SET_SHA256,
        )
        self.assertEqual(
            self.manifest["source"]["provider"],
            "Operador Nacional do Sistema Elétrico (ONS)",
        )
        self.assertEqual(
            self.manifest["source"]["license"],
            "Creative Commons Attribution",
        )
        self.assertTrue(self.manifest["source"]["readme_sha256"])
        self.assertTrue(self.manifest["source"]["summary_sha256"])

    def test_geojson_parts_and_points_match_the_pinned_shapefile_inventory(self):
        for basin_id, entry in self.manifest["geometries"].items():
            feature = json.loads((CORE_ROOT / entry["path"]).read_text(encoding="utf-8"))
            geometry = feature["geometry"]
            polygons = (
                [geometry["coordinates"]]
                if geometry["type"] == "Polygon"
                else geometry["coordinates"]
            )
            rings = [ring for polygon in polygons for ring in polygon]

            self.assertEqual(entry["source_part_count"], len(rings), basin_id)
            self.assertEqual(
                entry["source_point_count"],
                sum(len(ring) for ring in rings),
                basin_id,
            )
            self.assertEqual(entry["interior_ring_count"], 0, basin_id)
            self.assertEqual(len(polygons), entry["source_part_count"], basin_id)

    def test_changed_contours_have_stable_reference_areas(self):
        catalog = load_geometry_catalog()
        for basin_id, expected_area in ALIGNED_AREA_KM2.items():
            self.assertAlmostEqual(
                catalog.area_km2[basin_id],
                expected_area,
                places=5,
                msg=basin_id,
            )

    def test_reviewed_aggregate_basins_are_present_with_positive_scale(self):
        catalog = load_geometry_catalog()
        for basin_id in ("parana", "grande", "paranaiba", "sao_francisco", "tocantins"):
            self.assertGreater(catalog.area_km2[basin_id], 10_000)
