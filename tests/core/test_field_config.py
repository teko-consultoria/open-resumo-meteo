from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from resumo_meteo.core.constants import AUTHORIZED_MODEL_IDS
from resumo_meteo.core.field_config import (
    DEFAULT_MODEL_FIELD_CONFIG,
    DEFAULT_MODEL_FIELD_CONFIG_PATH,
    load_model_field_config,
)


class WeatherSummaryFieldConfigTests(unittest.TestCase):
    def test_config_has_only_authorized_models_and_00z(self):
        self.assertEqual(tuple(DEFAULT_MODEL_FIELD_CONFIG.models), AUTHORIZED_MODEL_IDS)
        self.assertEqual(DEFAULT_MODEL_FIELD_CONFIG.cycle_utc, "00")

    def test_ensembles_keep_expected_member_counts(self):
        self.assertEqual(DEFAULT_MODEL_FIELD_CONFIG.model("gefs")["expected_members"], 31)
        self.assertEqual(DEFAULT_MODEL_FIELD_CONFIG.model("ecens")["expected_members"], 50)

    def test_deterministic_models_cover_synoptic_levels(self):
        required_roles = {"surface", "850_hpa", "500_hpa", "200_250_hpa"}
        for model_id in ("gfs", "ec"):
            roles = {field["role"] for field in DEFAULT_MODEL_FIELD_CONFIG.required_fields(model_id)}
            self.assertTrue(required_roles.issubset(roles), model_id)

    def test_ec_horizon_is_never_extended(self):
        self.assertEqual(DEFAULT_MODEL_FIELD_CONFIG.model("ec")["max_lead_h"], 240)

    def test_rejects_unapproved_model(self):
        payload = json.loads(DEFAULT_MODEL_FIELD_CONFIG_PATH.read_text(encoding="utf-8"))
        payload["models"]["icon"] = payload["models"]["gfs"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fields.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "somente GFS"):
                load_model_field_config(path)


if __name__ == "__main__":
    unittest.main()
