from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime

import pytest

from resumo_meteo.ingestion.synoptic import (
    SYNOPTIC_FIELD_SPECS,
    FetchedSynopticStep,
    SynopticFieldEvidence,
    build_weather_summary_synoptic_metadata,
    ingest_synoptic_diagnostics,
    persist_synoptic_ingestion,
)

REFERENCE_DATE = date(2026, 7, 13)


def evidence(lead_hour: int) -> SynopticFieldEvidence:
    return SynopticFieldEvidence(
        field_id="mean_sea_level_pressure",
        group="surface",
        parameter="mean_sea_level_pressure",
        level_type="surface",
        level_hpa=None,
        accumulation_semantics="instantaneous",
        units="hPa",
        point_count=4,
        minimum=995.0,
        maximum=1025.0,
        mean=1010.0,
        p10=998.0,
        p50=1010.0,
        p90=1022.0,
        min_location={"longitude": -55.0, "latitude": -30.0},
        max_location={"longitude": -45.0, "latitude": -20.0},
        source_message={"lead_hour": lead_hour},
        regional_summaries={"south_brazil": {"point_count": 2, "mean": 1005.0}},
    )


class StepFetcher:
    def __init__(self, *, fail_lead: int | None = None, leave_raw=False):
        self.fail_lead = fail_lead
        self.leave_raw = leave_raw

    def __call__(self, reference_date, cycle, lead_hour, work_dir):
        del reference_date, cycle
        if self.leave_raw:
            (work_dir / "temporary.grib2").write_bytes(b"raw")
        if lead_hour == self.fail_lead:
            raise RuntimeError("indisponivel")
        return FetchedSynopticStep(lead_hour=lead_hour, fields=(evidence(lead_hour),))


def test_synoptic_ingestion_builds_objective_timeline_and_cleans_raw(tmp_path):
    result = ingest_synoptic_diagnostics(
        reference_date=REFERENCE_DATE,
        model="gfs",
        lead_hours=(0, 12, 120, 240),
        step_fetcher=StepFetcher(leave_raw=True),
        temporary_root=tmp_path,
        generated_at_utc=datetime(2026, 7, 13, 9, tzinfo=UTC),
    )
    assert result.status == "complete"
    assert [item["window_id"] for item in result.timeline] == ["p1", "p1", "p2", "p3"]
    assert result.source_id == "noaa_gfs"
    assert list(tmp_path.rglob("*.grib2")) == []


def test_failed_synoptic_step_is_auditable_and_partial():
    result = ingest_synoptic_diagnostics(
        reference_date=REFERENCE_DATE,
        model="ec",
        lead_hours=(0, 12, 24),
        step_fetcher=StepFetcher(fail_lead=12),
    )
    assert result.status == "partial"
    assert result.failed_steps[0]["lead_hour"] == 12
    assert result.failed_steps[0]["code"] == "synoptic_step_failed"


def test_ec_synoptic_never_extrapolates_to_240h_or_third_window():
    with pytest.raises(ValueError, match="dentro"):
        ingest_synoptic_diagnostics(
            reference_date=REFERENCE_DATE,
            model="ec",
            lead_hours=(0, 120, 240),
            step_fetcher=StepFetcher(),
        )


def test_synoptic_persistence_and_packet_metadata(tmp_path):
    result = ingest_synoptic_diagnostics(
        reference_date=REFERENCE_DATE,
        model="gfs",
        lead_hours=(0, 12),
        step_fetcher=StepFetcher(),
    )
    artifacts = persist_synoptic_ingestion(
        result,
        media_root=tmp_path,
        run_id="gfs-synoptic-20260713",
    )
    manifest = json.loads((tmp_path / artifacts.manifest_path).read_text(encoding="utf-8"))
    assert manifest["raw_grib_persisted"] is False
    with gzip.open(tmp_path / artifacts.csv_path, "rt", encoding="utf-8") as file_obj:
        assert "mean_sea_level_pressure" in file_obj.read()
    metadata = build_weather_summary_synoptic_metadata(result, artifacts)
    assert metadata["model"] == "gfs"
    assert metadata["json_path"] == artifacts.json_path
    assert metadata["manifest_path"] == artifacts.manifest_path


def test_configured_synoptic_fields_include_surface_850_500_and_250_hpa():
    groups = {item.group for item in SYNOPTIC_FIELD_SPECS}
    assert {"surface", "850_hpa", "500_hpa", "250_hpa"}.issubset(groups)
