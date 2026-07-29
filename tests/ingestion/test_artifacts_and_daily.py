from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from resumo_meteo.ingestion.artifacts import managed_workdir
from resumo_meteo.ingestion.deterministic import (
    build_model_processed_data,
    ingest_model_precipitation,
    persist_model_precipitation,
)
from resumo_meteo.ingestion.grib import GribPrecipitationField


def test_managed_workdir_removes_raw_files_after_failure(tmp_path):
    captured = None
    with pytest.raises(RuntimeError):
        with managed_workdir(temporary_root=tmp_path) as work:
            captured = work
            (work / "large.grib2").write_bytes(b"raw")
            raise RuntimeError("boom")
    assert captured is not None
    assert not captured.exists()
    assert list(tmp_path.rglob("*.grib2")) == []


class FakeECMWFClient:
    def __init__(self):
        self.paths: list[Path] = []
        self.session = type("Session", (), {"close": lambda self: None})()

    def select_tp_messages(self, product_slug, reference_date, *, cycle, forecast_hour):
        del product_slug, reference_date, cycle
        return (forecast_hour,)

    def grib_url(self, product_slug, reference_date, *, cycle, forecast_hour):
        del product_slug, reference_date, cycle
        return f"https://example.test/f{forecast_hour:03d}.grib2"

    def index_url(self, product_slug, reference_date, *, cycle, forecast_hour):
        del product_slug, reference_date, cycle
        return f"https://example.test/f{forecast_hour:03d}.index"

    def download_messages(self, records, *, grib_url, output_path):
        del records, grib_url
        path = Path(output_path)
        path.write_bytes(b"temporary")
        self.paths.append(path)
        return {"path": str(path)}


def fake_precipitation_reader(path, *, source_slug, aggregation="sum"):
    del source_slug, aggregation
    lead = int(Path(path).stem.rsplit("f", 1)[-1])
    value = lead / 24 * 10.0
    return GribPrecipitationField(
        longitudes=np.asarray([-60.0, -50.0, -60.0, -50.0]),
        latitudes=np.asarray([-20.0, -20.0, -10.0, -10.0]),
        values_mm=np.full(4, value),
        units="m",
        message_count=1,
    )


def test_ec_daily_ingestion_stops_at_240h_and_cleans_every_grib(tmp_path):
    client = FakeECMWFClient()
    with patch(
        "resumo_meteo.ingestion.deterministic.read_precipitation_field",
        side_effect=fake_precipitation_reader,
    ):
        result = ingest_model_precipitation(
            model="ec",
            reference_date=date(2026, 7, 13),
            horizon_hours=336,
            temporary_root=tmp_path,
            ecmwf_client=client,
            generated_at_utc=datetime(2026, 7, 13, 9, tzinfo=UTC),
        )
    assert result.max_forecast_hour == 240
    assert len(result.daily_grids) == 10
    assert result.daily_grids[-1].forecast_date == date(2026, 7, 23)
    assert all(np.allclose(item.field.values_mm, 10.0) for item in result.daily_grids)
    assert all(not path.exists() for path in client.paths)
    assert list(tmp_path.rglob("*.grib2")) == []


def test_daily_persistence_exposes_current_packet_contract_without_raw_grib(tmp_path):
    client = FakeECMWFClient()
    with patch(
        "resumo_meteo.ingestion.deterministic.read_precipitation_field",
        side_effect=fake_precipitation_reader,
    ):
        result = ingest_model_precipitation(
            model="ec",
            reference_date=date(2026, 7, 13),
            horizon_hours=48,
            ecmwf_client=client,
        )
    artifacts = persist_model_precipitation(
        result,
        artifact_root=tmp_path,
        run_id="ec-20260713-00z",
    )
    processed = build_model_processed_data(result, artifacts)
    assert processed["complete_storage"]["files"] == list(artifacts.files)
    assert processed["max_forecast_hour"] == 48
    assert processed["cycle"] == "00"
    assert len(processed["complete_storage"]["files"]) == 2
    assert list(tmp_path.rglob("*.grib*")) == []
    assert (tmp_path / artifacts.manifest_path).is_file()
