from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime

import numpy as np

from resumo_meteo.core.spatial import BasinGeometry
from resumo_meteo.ingestion.ensemble import (
    DecodedPrecipitationGrid,
    EnsembleIngestionError,
    MemberPrecipitationStep,
    build_weather_summary_ensemble_metadata,
    ingest_all_member_precipitation,
    persist_ensemble_ingestion,
)

REFERENCE_DATE = date(2026, 7, 13)


def grid(values) -> DecodedPrecipitationGrid:
    return DecodedPrecipitationGrid(
        longitudes=np.asarray([0.5, 1.5, 0.5, 1.5]),
        latitudes=np.asarray([0.5, 0.5, 1.5, 1.5]),
        values_mm=np.asarray(values, dtype=np.float64),
    )


def geometry() -> BasinGeometry:
    return BasinGeometry.from_polygon(
        basin_id="uruguai",
        exterior=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)),
        geometry_version="test-v1",
    )


class WindowFetcher:
    def __init__(self, model: str, *, fail_member: str | None = None, leave_raw=False):
        self.model = model
        self.fail_member = fail_member
        self.leave_raw = leave_raw

    def __call__(self, reference_date, cycle, member_id, work_dir):
        del reference_date, cycle
        if self.leave_raw:
            (work_dir / "must-disappear.grib2").write_bytes(b"raw")
        if member_id == self.fail_member:
            raise EnsembleIngestionError("member_unavailable", "membro ausente")
        value = sum(ord(character) for character in member_id) % 5 + 1
        return tuple(
            MemberPrecipitationStep(
                model=self.model,
                member_id=member_id,
                start_lead_hour=start,
                end_lead_hour=end,
                grid=grid((value, value + 1, value + 2, value + 3)),
                source_references=(f"https://example.test/{self.model}/{member_id}/{end}",),
            )
            for start, end in ((0, 120), (120, 240), (240, 336))
        )


def test_all_member_rows_remain_separate_between_models_and_members():
    result = ingest_all_member_precipitation(
        reference_date=REFERENCE_DATE,
        geometries={"uruguai": geometry()},
        required_basin_ids=("uruguai",),
        models=("gefs", "ecens"),
        member_ids_by_model={"gefs": ("gec00", "gep01"), "ecens": ("1", "2")},
        member_fetchers={
            "gefs": WindowFetcher("gefs"),
            "ecens": WindowFetcher("ecens"),
        },
        generated_at_utc=datetime(2026, 7, 13, 9, tzinfo=UTC),
    )
    assert result.quality_status == "complete"
    assert len(result.rows) == 12
    assert {(row.model, row.member_id) for row in result.rows} == {
        ("gefs", "gec00"),
        ("gefs", "gep01"),
        ("ecens", "1"),
        ("ecens", "2"),
    }
    assert {row.family for row in result.rows if row.model == "gefs"} == {"ncep"}
    assert {row.family for row in result.rows if row.model == "ecens"} == {"ecmwf"}
    assert all(row.point_max_mm > row.basin_mean_mm for row in result.rows)


def test_failed_member_is_explicit_and_temporary_raw_file_is_removed(tmp_path):
    result = ingest_all_member_precipitation(
        reference_date=REFERENCE_DATE,
        geometries={"uruguai": geometry()},
        required_basin_ids=("uruguai",),
        models=("gefs",),
        member_ids_by_model={"gefs": ("gec00", "gep01")},
        member_fetchers={
            "gefs": WindowFetcher("gefs", fail_member="gep01", leave_raw=True)
        },
        temporary_root=tmp_path,
    )
    summary = result.models["gefs"]
    assert summary.status == "partial"
    assert summary.available_member_ids == ("gec00",)
    assert summary.missing_member_ids == ("gep01",)
    assert summary.failed_members[0]["code"] == "member_unavailable"
    assert list(tmp_path.rglob("*.grib2")) == []


def test_ecens_members_use_bounded_parallel_fetchers():
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return WindowFetcher("ecens")

    result = ingest_all_member_precipitation(
        reference_date=REFERENCE_DATE,
        geometries={"uruguai": geometry()},
        required_basin_ids=("uruguai",),
        models=("ecens",),
        member_ids_by_model={"ecens": ("1", "2")},
        ecens_fetcher_factory=factory,
        ecens_max_workers=2,
    )

    assert result.models["ecens"].available_member_ids == ("1", "2")
    assert factory_calls == 2


def test_ensemble_persistence_is_compact_and_keeps_packet_metadata(tmp_path):
    result = ingest_all_member_precipitation(
        reference_date=REFERENCE_DATE,
        geometries={"uruguai": geometry()},
        required_basin_ids=("uruguai",),
        models=("gefs",),
        member_ids_by_model={"gefs": ("gec00", "gep01")},
        member_fetchers={"gefs": WindowFetcher("gefs")},
    )
    artifacts = persist_ensemble_ingestion(
        result,
        media_root=tmp_path,
        run_id="gefs-20260713",
    )
    manifest = json.loads((tmp_path / artifacts.manifest_path).read_text(encoding="utf-8"))
    assert manifest["raw_grib_persisted"] is False
    assert list((tmp_path / artifacts.directory).rglob("*.grib*")) == []
    with gzip.open(tmp_path / artifacts.csv_path, "rt", encoding="utf-8") as file_obj:
        rows = file_obj.read()
    assert "basin_mean_mm" in rows
    assert "point_max_mm" in rows
    metadata = build_weather_summary_ensemble_metadata(
        result, artifacts, model="gefs"
    )
    assert metadata["member_count"] == 2
    assert metadata["json_path"] == artifacts.json_path
    assert metadata["manifest_path"] == artifacts.manifest_path
    assert metadata["requested_basin_ids"] == ["uruguai"]
    assert metadata["geometry_version"] == "test-v1"


def test_nonzero_cycle_is_rejected():
    try:
        ingest_all_member_precipitation(
            reference_date=REFERENCE_DATE,
            cycle="12",
            geometries={"uruguai": geometry()},
            required_basin_ids=("uruguai",),
            models=("gefs",),
            member_fetchers={"gefs": WindowFetcher("gefs")},
        )
    except ValueError as exc:
        assert "exclusivamente" in str(exc)
    else:
        raise AssertionError("rodada 12z deveria ser rejeitada")
