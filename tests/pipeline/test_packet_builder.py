from __future__ import annotations

import csv
import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from resumo_meteo.core.basins import DEFAULT_BASIN_REGISTRY
from resumo_meteo.core.geometry_loader import GeometryCatalog
from resumo_meteo.core.spatial import BasinGeometry, geometry_area
from resumo_meteo.core.summary_scope import SummaryBasinScope
from resumo_meteo.pipeline.packet_builder import PacketBuildBlocked, build_analysis_packet

ISSUE_DATE = date(2026, 7, 13)
CUTOFF_UTC = datetime(2026, 7, 13, 10, tzinfo=UTC)
GEOMETRY_VERSION = "pipeline-test-contours@1"
TEST_SCOPE = SummaryBasinScope(
    schema_version=1,
    scope_id="pipeline-test-summary-scope",
    scope_version="1",
    description="escopo sintético de teste",
    climatology_provider="MERGE_INPE",
    basin_ids=("uruguai",),
)


def _geometry_catalog() -> GeometryCatalog:
    geometry = BasinGeometry.from_polygon(
        basin_id="uruguai",
        exterior=((0, 0), (1, 0), (1, 1), (0, 1)),
        geometry_version=GEOMETRY_VERSION,
    )
    return GeometryCatalog(
        version=GEOMETRY_VERSION,
        source_name="contorno sintético local do teste",
        crs="EPSG:4326",
        geometries={"uruguai": geometry},
        area_km2={"uruguai": geometry_area(geometry)},
        missing_basin_ids=tuple(
            basin.basin_id for basin in DEFAULT_BASIN_REGISTRY.basins if basin.basin_id != "uruguai"
        ),
    )


def _write_climatology(path: Path) -> Path:
    window = {
        "duration_days": 0,
        "sample_count": 20,
        "mean_mm": 20,
        "median_mm": 18,
        "p10_mm": 5,
        "p25_mm": 10,
        "p75_mm": 30,
        "p90_mm": 40,
    }
    windows = {}
    current = date(2000, 1, 1)
    while current.year == 2000:
        for duration in (4, 5):
            windows[f"{current:%m-%d}:{duration}"] = {
                **window,
                "duration_days": duration,
            }
        current += timedelta(days=1)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "climatology_id": "pipeline-test-climatology-v2",
                "version": "2.0.0",
                "reference_period": "2001-2020",
                "provider": "MERGE_INPE",
                "source_product": "MERGE_DAILY_BASIN",
                "basin_definition_version": "historical-test-mask@current",
                "source_mask_version": None,
                "source_mask_limitation": "Máscara histórica sintética usada apenas no teste.",
                "minimum_complete_years": 5,
                "basins": {"uruguai": {"windows": windows}},
                "quality": {
                    "calendar_complete": True,
                    "calendar_windows_per_basin": 732,
                },
                "provenance": {
                    "algorithm": "calendar_window_sum_r7_quantiles_v1",
                    "daily_percentiles_summed": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeAnalysisRepository:
    """Filesystem-backed fake implementing only the standalone read contract."""

    def __init__(self, root: Path, *, mixed_initializations: bool = False) -> None:
        self.root = root
        self.mixed_initializations = mixed_initializations
        self.records = self._model_records()
        self.imerg_records = self._observed_records()

    def _write_grid(self, name: str, valid_date: date, values: tuple[float, ...]) -> dict:
        path = self.root / name
        with gzip.open(path, "wt", encoding="utf-8", newline="") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(
                ["run_date", "forecast_date", "longitude", "latitude", "precipitation_mm"]
            )
            points = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
            for (longitude, latitude), value in zip(points, values, strict=True):
                writer.writerow([ISSUE_DATE, valid_date, longitude, latitude, value])
        return {"path": path.name, "forecast_date": valid_date.isoformat()}

    @staticmethod
    def _product(product_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=product_id,
            processed_at=CUTOFF_UTC - timedelta(hours=1),
            processed_data={},
        )

    def _record(self, model: str, *, days: int, max_lead_h: int, init_date: date):
        files = tuple(
            self._write_grid(
                f"{model}-{offset:02d}.csv.gz",
                ISSUE_DATE + timedelta(days=offset),
                (1.0 + offset, 2.0 + offset, 3.0 + offset, 8.0 + offset),
            )
            for offset in range(1, days + 1)
        )
        return SimpleNamespace(
            model_key=model,
            family="ncep" if model == "gfs" else "ecmwf",
            product_data=self._product(f"product-{model}"),
            reference_date=init_date,
            run_datetime_utc=datetime.combine(init_date, datetime.min.time(), tzinfo=UTC),
            max_lead_h=max_lead_h,
            member_count=None,
            grid_files=files,
            status="complete",
            issues=(),
        )

    def _model_records(self) -> dict[str, SimpleNamespace]:
        ec_date = ISSUE_DATE + timedelta(days=1) if self.mixed_initializations else ISSUE_DATE
        return {
            "gfs": self._record("gfs", days=15, max_lead_h=384, init_date=ISSUE_DATE),
            "ec": self._record("ec", days=10, max_lead_h=240, init_date=ec_date),
        }

    def _observed_records(self) -> tuple[SimpleNamespace, ...]:
        records = []
        for age in (3, 2, 1):
            observed_date = ISSUE_DATE - timedelta(days=age)
            records.append(
                SimpleNamespace(
                    product_data=self._product(f"imerg-{observed_date:%Y%m%d}"),
                    observation_date=observed_date,
                    grid_files=(
                        self._write_grid(
                            f"imerg-{observed_date:%Y%m%d}.csv.gz",
                            observed_date,
                            (1.0, 2.0, 3.0, 4.0),
                        ),
                    ),
                    status="complete",
                    issues=(),
                    basin_aggregates=None,
                )
            )
        return tuple(records)

    def select_model_inputs(self, *, cutoff_utc, target_reference_date=None):
        assert cutoff_utc == CUTOFF_UTC
        assert target_reference_date == ISSUE_DATE
        return self.records, {
            "selected_reference_date": ISSUE_DATE.isoformat(),
            "required_cycle": "00",
        }

    def select_imerg_history(self, *, cutoff_utc, days, end_date=None):
        assert cutoff_utc == CUTOFF_UTC
        assert days == 30
        selected = tuple(
            record
            for record in self.imerg_records
            if end_date is None or record.observation_date <= end_date
        )
        return selected, SimpleNamespace(
            status="partial",
            issues=(f"history_days_{len(selected)}_expected_{days}",),
        )

    def select_imerg_input(self, *, cutoff_utc):
        assert cutoff_utc == CUTOFF_UTC
        return None, None

    def resolve_media_file(self, relative_path: str, *, expected_sha256: str = "") -> Path:
        return self.root / relative_path


class StalePartialObservedRepository(FakeAnalysisRepository):
    def select_imerg_input(self, *, cutoff_utc):
        assert cutoff_utc == CUTOFF_UTC
        grid_file = self._write_grid(
            "imerg-partial-current-grid.csv.gz",
            ISSUE_DATE,
            (5.0, 5.0, 5.0, 5.0),
        )
        stale_aggregation = {
            "basin_id": "uruguai",
            "basin_mean_mm": 999.0,
            "spatial_p10_mm": 999.0,
            "spatial_median_mm": 999.0,
            "spatial_p90_mm": 999.0,
            "point_max_mm": 999.0,
            "point_max_cell_id": "stale",
            "grid_coverage_fraction": 1.0,
            "valid_data_coverage_fraction": 1.0,
            "covered_area": 1.0,
            "valid_data_area": 1.0,
            "area_above_threshold_fraction": {
                "gt_5_mm": 1.0,
                "gt_10_mm": 1.0,
                "gt_25_mm": 1.0,
                "gt_50_mm": 1.0,
            },
            "quality_flags": [],
        }
        return (
            SimpleNamespace(
                product_data=self._product("imerg-partial"),
                observation_date=ISSUE_DATE,
                temporal_resolution="30_minutes",
                period_complete=False,
                basin_aggregates={
                    "schema_version": "weather-summary-observed-basins.v1",
                    "geometry_version": "retired-contours",
                    "requested_basin_ids": ["uruguai"],
                    "basin_count": 1,
                    "basins": [stale_aggregation],
                },
                grid_files=(grid_file,),
                data_start_utc=datetime.combine(
                    ISSUE_DATE,
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                data_end_utc=CUTOFF_UTC - timedelta(hours=1),
                status="partial",
                issues=("calendar_day_in_progress",),
            ),
            SimpleNamespace(status="partial", issues=("calendar_day_in_progress",)),
        )


def test_builds_valid_packet_from_standalone_repository(tmp_path: Path) -> None:
    geometry = _geometry_catalog()
    packet = build_analysis_packet(
        ISSUE_DATE,
        CUTOFF_UTC,
        repository=FakeAnalysisRepository(tmp_path),
        climatology_path=_write_climatology(tmp_path / "climatology.json"),
        geometry_catalog=geometry,
        summary_basin_scope=TEST_SCOPE,
    )

    assert [window.duration_days for window in packet.forecast_windows] == [5, 5, 4]
    assert [basin.basin_id for basin in packet.basins] == ["uruguai"]
    assert packet.quality.summary_basin_ids == ["uruguai"]
    assert packet.quality.generation_allowed is True
    assert packet.quality.climatology_id == "pipeline-test-climatology-v2"
    assert packet.model_runs.gfs.max_lead_h == 336
    assert packet.model_runs.gfs.available_steps == 14
    assert packet.model_runs.ec.available_steps == 10
    basin = next(item for item in packet.basins if item.basin_id == "uruguai")
    assert basin.windows is not None
    assert basin.observed is not None
    assert basin.observed.rain_72h_mm is not None


def test_stale_partial_imerg_aggregate_is_recomputed_with_forecast_geometry(
    tmp_path: Path,
) -> None:
    packet = build_analysis_packet(
        ISSUE_DATE,
        CUTOFF_UTC,
        repository=StalePartialObservedRepository(tmp_path),
        climatology_path=_write_climatology(tmp_path / "climatology.json"),
        geometry_catalog=_geometry_catalog(),
        summary_basin_scope=TEST_SCOPE,
    )

    basin = packet.basins[0]
    assert basin.observed is not None
    assert basin.observed.rain_latest_partial_mm == pytest.approx(5.0)
    assert (
        "forecast_observed_geometry_catalog_aligned"
        in packet.quality.checks_passed
    )


def test_ec_is_never_extrapolated_into_third_window(tmp_path: Path) -> None:
    packet = build_analysis_packet(
        ISSUE_DATE,
        CUTOFF_UTC,
        repository=FakeAnalysisRepository(tmp_path),
        climatology_path=_write_climatology(tmp_path / "climatology.json"),
        geometry_catalog=_geometry_catalog(),
        summary_basin_scope=TEST_SCOPE,
    )

    basin = next(item for item in packet.basins if item.basin_id == "uruguai")
    assert basin.windows is not None
    assert packet.model_runs.ec.max_lead_h == 240
    assert basin.windows.p3.ec is None
    assert basin.windows.p3.families.ecmwf is None
    assert all(item.ec_basin_mean_mm is None for item in basin.windows.p3.daily_timing)
    assert "ec_not_extrapolated" in packet.quality.checks_passed


def test_mixed_model_initializations_are_blocked_before_grid_processing(tmp_path: Path) -> None:
    with pytest.raises(PacketBuildBlocked) as raised:
        build_analysis_packet(
            ISSUE_DATE,
            CUTOFF_UTC,
            repository=FakeAnalysisRepository(tmp_path, mixed_initializations=True),
            climatology_path=_write_climatology(tmp_path / "climatology.json"),
            geometry_catalog=_geometry_catalog(),
        )

    assert "mixed_model_initializations" in raised.value.assessment["blockers"]
    assert raised.value.assessment["quality_manifest"]["generation_allowed"] is False


def test_invalid_climatology_blocks_with_auditable_reason(tmp_path: Path) -> None:
    invalid_climatology = tmp_path / "invalid-climatology.json"
    invalid_climatology.write_text('{"schema_version": 2}', encoding="utf-8")

    with pytest.raises(PacketBuildBlocked) as raised:
        build_analysis_packet(
            ISSUE_DATE,
            CUTOFF_UTC,
            repository=FakeAnalysisRepository(tmp_path),
            climatology_path=invalid_climatology,
            geometry_catalog=_geometry_catalog(),
            summary_basin_scope=TEST_SCOPE,
        )

    assert raised.value.assessment["blockers"] == ["climatology_invalid"]
    details = raised.value.assessment["blocker_details"]["climatology_invalid"]
    assert "error" in details
    assert raised.value.assessment["quality_manifest"]["generation_allowed"] is False


def test_climatology_accepts_independent_source_mask_when_scope_matches(tmp_path: Path) -> None:
    """A documented source mask can support the current scope without claiming equivalence."""

    climatology_path = _write_climatology(tmp_path / "climatology.json")
    payload = json.loads(climatology_path.read_text(encoding="utf-8"))
    payload.update(
        provider="MERGE_INPE",
        basin_definition_version="independent-historical-mask@current",
    )
    climatology_path.write_text(json.dumps(payload), encoding="utf-8")

    packet = build_analysis_packet(
        ISSUE_DATE,
        CUTOFF_UTC,
        repository=FakeAnalysisRepository(tmp_path),
        climatology_path=climatology_path,
        geometry_catalog=_geometry_catalog(),
        summary_basin_scope=TEST_SCOPE,
    )

    assert packet.quality.climatology_id == "pipeline-test-climatology-v2"
    assert packet.basins[0].windows is not None
    assert packet.basins[0].windows.p1.climatology is not None
