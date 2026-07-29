from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import h5py
import numpy as np
import pytest

from resumo_meteo.core.observed_aggregation import (
    SCHEMA_VERSION as OBSERVED_BASIN_SCHEMA_VERSION,
)
from resumo_meteo.core.observed_aggregation import (
    observed_basin_aggregates_match_contract,
)
from resumo_meteo.core.summary_scope import WEATHER_SUMMARY_BASIN_IDS
from resumo_meteo.ingestion.imerg import (
    build_imerg_processed_data,
    ingest_imerg_daily_snapshot,
    ingest_imerg_early_snapshot,
    persist_imerg_snapshot,
    read_gpm_imerg_rows,
)
from resumo_meteo.sources.nasa import (
    GPM_IMERG_DAILY_PRODUCT_SLUG,
    GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
    NASAGranule,
)


def make_imerg_hdf(path: Path, *, rate: float = 2.0):
    with h5py.File(path, "w") as h5:
        grid = h5.create_group("Grid")
        grid.create_dataset("lon", data=np.asarray([-80.0, -70.0, -60.0]))
        grid.create_dataset("lat", data=np.asarray([-40.0, -30.0, -20.0]))
        dataset = grid.create_dataset("precipitation", data=np.full((3, 3), rate))
        dataset.attrs["units"] = "mm h-1"


class FakeNASAClient:
    def __init__(self, source: Path):
        self.source = source
        self.downloaded_paths: list[Path] = []

    def is_configured(self):
        return True

    def find_granules(self, product_slug, product_date):
        assert product_slug == GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG
        return tuple(
            NASAGranule(
                product_slug=product_slug,
                product_date=product_date,
                title=f"g{index}",
                concept_id=f"G{index}",
                url=f"https://example.test/g{index}.HDF5",
                start_utc=(
                    datetime(2026, 7, 13, tzinfo=UTC)
                    + timedelta(minutes=30 * index)
                ),
                end_utc=(
                    datetime(2026, 7, 13, tzinfo=UTC)
                    + timedelta(minutes=30 * (index + 1))
                ),
            )
            for index in range(2)
        )

    def download_granule(self, granule, output_path):
        del granule
        target = Path(output_path)
        shutil.copy2(self.source, target)
        self.downloaded_paths.append(target)
        return {"path": str(target), "size_bytes": target.stat().st_size}


class FakeDailyNASAClient(FakeNASAClient):
    def find_granules(self, product_slug, product_date):
        assert product_slug == GPM_IMERG_DAILY_PRODUCT_SLUG
        return (
            NASAGranule(
                product_slug=product_slug,
                product_date=product_date,
                title="daily",
                concept_id="DAILY-1",
                url="https://example.test/daily.HDF5",
                start_utc=datetime.combine(product_date, datetime.min.time(), tzinfo=UTC),
                end_utc=datetime.combine(
                    product_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ),
            ),
        )


def test_imerg_reader_converts_half_hour_rate_to_accumulation(tmp_path):
    path = tmp_path / "imerg.h5"
    make_imerg_hdf(path)
    rows, summary = read_gpm_imerg_rows(path, accumulation_hours=0.5)
    assert len(rows) == 4
    assert {value for _lon, _lat, value in rows} == {1.0}
    assert summary["precipitation_mm"]["avg"] == 1.0


def test_compact_observed_aggregates_require_current_geometry_scope_and_rows():
    payload = {
        "schema_version": OBSERVED_BASIN_SCHEMA_VERSION,
        "geometry_version": "current-contours",
        "requested_basin_ids": ["jacui", "uruguai"],
        "basin_count": 2,
        "basins": [{"basin_id": "jacui"}, {"basin_id": "uruguai"}],
    }

    assert observed_basin_aggregates_match_contract(
        payload,
        geometry_version="current-contours",
        basin_ids=("jacui", "uruguai"),
    )

    for field, value in (
        ("geometry_version", "retired-contours"),
        ("requested_basin_ids", ["uruguai", "jacui"]),
        ("basins", [{"basin_id": "jacui"}]),
    ):
        incompatible = {**payload, field: value}
        assert not observed_basin_aggregates_match_contract(
            incompatible,
            geometry_version="current-contours",
            basin_ids=("jacui", "uruguai"),
        )


def test_imerg_snapshot_accumulates_and_removes_downloads(tmp_path):
    source = tmp_path / "source.h5"
    make_imerg_hdf(source)
    client = FakeNASAClient(source)
    work_root = tmp_path / "work"
    snapshot = ingest_imerg_early_snapshot(
        product_date=date(2026, 7, 13),
        earthdata_token="unused",
        client=client,
        max_workers=1,
        temporary_root=work_root,
        aggregate_basins=False,
        generated_at_utc=datetime(2026, 7, 13, 9, tzinfo=UTC),
    )
    assert snapshot.interval_count == 2
    assert snapshot.coverage_pct == 100 * 2 / 48
    assert {value for _lon, _lat, value in snapshot.rows} == {2.0}
    assert all(not path.exists() for path in client.downloaded_paths)
    assert list(work_root.rglob("*.HDF5")) == []


def test_imerg_aggregation_requests_only_the_versioned_summary_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.h5"
    make_imerg_hdf(source)
    requested: dict[str, object] = {}

    def fake_aggregate(rows, **kwargs):
        requested["rows"] = rows
        requested.update(kwargs)
        return {"requested_basin_ids": list(kwargs["basin_ids"]), "basins": []}

    monkeypatch.setattr("resumo_meteo.ingestion.imerg.aggregate_observed_rows_by_basin", fake_aggregate)
    snapshot = ingest_imerg_early_snapshot(
        product_date=date(2026, 7, 13),
        earthdata_token="unused",
        client=FakeNASAClient(source),
        max_workers=1,
        geometry_catalog=object(),
        generated_at_utc=datetime(2026, 7, 13, 9, tzinfo=UTC),
    )

    assert requested["basin_ids"] == WEATHER_SUMMARY_BASIN_IDS
    assert snapshot.weather_summary_observed_basins == {
        "requested_basin_ids": list(WEATHER_SUMMARY_BASIN_IDS),
        "basins": [],
    }


def test_imerg_persistence_preserves_packet_contract(tmp_path):
    source = tmp_path / "source.h5"
    make_imerg_hdf(source)
    snapshot = ingest_imerg_early_snapshot(
        product_date=date(2026, 7, 13),
        earthdata_token="unused",
        client=FakeNASAClient(source),
        max_workers=1,
        aggregate_basins=False,
    )
    artifacts = persist_imerg_snapshot(
        snapshot,
        artifact_root=tmp_path / "artifacts",
        run_id="imerg-20260713",
    )
    processed = build_imerg_processed_data(snapshot, artifacts)
    assert processed["complete_storage"]["files"] == list(artifacts.files)
    assert processed["imerg_half_hour_snapshot"]["schema_version"].endswith(".v1")
    assert processed["imerg_half_hour_snapshot"]["raw_hdf_persisted"] is False
    assert "weather_summary_observed_basins" not in processed
    assert list((tmp_path / "artifacts").rglob("*.h5")) == []


def test_completed_history_uses_one_daily_granule_without_rate_overcount(tmp_path):
    source = tmp_path / "daily-source.h5"
    make_imerg_hdf(source, rate=2.0)
    client = FakeDailyNASAClient(source)
    snapshot = ingest_imerg_daily_snapshot(
        product_date=date(2026, 7, 12),
        earthdata_token="unused",
        client=client,
        aggregate_basins=False,
        generated_at_utc=datetime(2026, 7, 13, 9, tzinfo=UTC),
    )

    assert snapshot.interval_count == snapshot.expected_interval_count == 1
    assert snapshot.period_complete is True
    assert snapshot.temporal_resolution == "daily"
    assert snapshot.product_slug == GPM_IMERG_DAILY_PRODUCT_SLUG
    assert {value for _lon, _lat, value in snapshot.rows} == {2.0}
    assert all(not path.exists() for path in client.downloaded_paths)

    artifacts = persist_imerg_snapshot(
        snapshot,
        artifact_root=tmp_path / "artifacts",
        run_id="imerg-daily-20260712",
    )
    processed = build_imerg_processed_data(snapshot, artifacts)
    assert processed["product_slug"] == GPM_IMERG_DAILY_PRODUCT_SLUG
    assert processed["imerg_daily_snapshot"]["temporal_resolution"] == "daily"
