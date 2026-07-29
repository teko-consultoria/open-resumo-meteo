from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from resumo_meteo.core.grid_io import GridInputError, load_ensemble_input

SCHEMA = "weather-summary-ensemble-precipitation.v1"
MANIFEST_SCHEMA = "weather-summary-ensemble-artifacts.v1"


class _Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve_media_file(self, relative_path, *, expected_sha256=""):
        path = (self.root / str(relative_path)).resolve()
        path.relative_to(self.root)
        if expected_sha256:
            self.asserted_checksum = expected_sha256
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected_sha256:
                raise ValueError("checksum divergente")
        return path


def _member_ids():
    return ["gec00", *[f"gep{number:02d}" for number in range(1, 31)]]


def _payload(*, omit_last_row=False, mixed_geometry=False):
    members = _member_ids()
    rows = []
    for member_id in members:
        for window_id, start, end in (
            ("p1", 0, 120),
            ("p2", 120, 240),
            ("p3", 240, 336),
        ):
            rows.append(
                {
                    "model": "gefs",
                    "family": "ncep",
                    "member_id": member_id,
                    "basin_id": "uruguai",
                    "geometry_version": (
                        "geometry-v2"
                        if mixed_geometry and member_id == "gep30" and window_id == "p3"
                        else "geometry-v1"
                    ),
                    "window_id": window_id,
                    "start_lead_hour": start,
                    "end_lead_hour": end,
                    "basin_mean_mm": 20.0,
                    "spatial_median_mm": 18.0,
                    "point_max_mm": 35.0,
                    "point_max_cell_id": "cell-1",
                    "grid_coverage_fraction": 1.0,
                    "valid_data_coverage_fraction": 1.0,
                    "quality_flags": [],
                }
            )
    if omit_last_row:
        rows.pop()
    return {
        "schema_version": SCHEMA,
        "geometry_catalog": {
            "available_basin_ids": ["uruguai"],
            "missing_basin_ids": [],
        },
        "models": {
            "gefs": {
                "expected_member_count": 31,
                "available_member_count": 31,
                "available_member_ids": members,
            }
        },
        "rows": rows,
    }


def _write_artifacts(root: Path, payload: dict) -> dict:
    directory = root / "ensemble"
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "ensemble_precipitation.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    json_relative = json_path.relative_to(root).as_posix()
    json_bytes = json_path.read_bytes()
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA,
                "files": [
                    {
                        "name": json_path.name,
                        "path": json_relative,
                        "size_bytes": len(json_bytes),
                        "sha256": hashlib.sha256(json_bytes).hexdigest(),
                    }
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA,
        "model": "gefs",
        "status": "complete",
        "cycle": "00",
        "reference_date": "2026-07-10",
        "member_count": 31,
        "expected_member_count": 31,
        "member_ids": _member_ids(),
        "json_path": json_relative,
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def _record(contract):
    return SimpleNamespace(
        model_key="gefs",
        family="ncep",
        product_data=SimpleNamespace(processed_data={"weather_summary_ensemble": contract}),
    )


class EnsembleInputIntegrityTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = _Repository(self.root)

    def test_checksums_and_exact_member_basin_window_matrix_are_accepted(self):
        contract = _write_artifacts(self.root, _payload())

        loaded = load_ensemble_input(_record(contract), repository=self.repository)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.member_count, 31)
        self.assertEqual(len(loaded.rows), 31 * 1 * 3)
        self.assertEqual({row.geometry_version for row in loaded.rows}, {"geometry-v1"})

    def test_json_tampering_is_rejected_before_parse(self):
        contract = _write_artifacts(self.root, _payload())
        path = self.root / contract["json_path"]
        path.write_bytes(path.read_bytes() + b" ")

        with self.assertRaises((GridInputError, ValueError)):
            load_ensemble_input(_record(contract), repository=self.repository)

    def test_truncated_row_matrix_is_rejected_even_with_fresh_checksums(self):
        contract = _write_artifacts(self.root, _payload(omit_last_row=True))

        with self.assertRaisesRegex(GridInputError, "matriz ensemble incompleta"):
            load_ensemble_input(_record(contract), repository=self.repository)

    def test_geometry_version_cannot_change_between_members(self):
        contract = _write_artifacts(self.root, _payload(mixed_geometry=True))

        with self.assertRaisesRegex(GridInputError, "geometry_version varia"):
            load_ensemble_input(_record(contract), repository=self.repository)
