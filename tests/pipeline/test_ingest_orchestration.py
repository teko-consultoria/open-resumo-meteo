from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from resumo_meteo.config import Settings
from resumo_meteo.core.summary_scope import WEATHER_SUMMARY_BASIN_IDS
from resumo_meteo.pipeline import ingest
from resumo_meteo.pipeline.runner import repository_from_settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'metadata.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        output_root=tmp_path / "outputs",
        timezone="America/Sao_Paulo",
        cycle="00",
        llm_provider="test",
        llm_base_url="https://invalid.test",
        llm_model="test",
        llm_api_key=None,
        llm_api_mode="chat_completions",
        llm_timeout_seconds=1,
        llm_max_attempts=1,
        llm_max_output_tokens=1000,
        nasa_earthdata_token=None,
        download_workers=1,
        log_level="CRITICAL",
        anchor_hour_utc=0,
        max_source_age_hours=42,
        source_retention_days=3,
    )


def test_00z_ingestion_never_reuses_a_06z_source_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = repository_from_settings(settings)
    reference_date = date(2026, 7, 13)
    wrong_cycle, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        family="ncep",
        cycle="06",
        reference_date=reference_date,
        status="processed",
        max_lead_h=336,
        idempotency_key="gfs-06z-existing",
    )
    repository.record_artifact(
        source_run_id=wrong_cycle.id,
        kind="grid_forecast",
        storage_path="normalized/gfs/06/day-001.csv.gz",
    )

    selected, reused = ingest._begin_source_run(
        repository,
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        reference_date=reference_date,
        data_kind="forecast",
        family="ncep",
        cycle="00",
        force=False,
    )

    assert reused is False
    assert selected.id != wrong_cycle.id
    assert selected.cycle == "00"
    assert selected.status == "downloading"


def test_concurrent_ingestion_has_only_one_active_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = repository_from_settings(settings)
    reference_date = date(2026, 7, 13)

    def begin(_index: int):
        return ingest._begin_source_run(
            repository,
            source_id="gfs",
            source_slug="noaa",
            product_slug="gfs-precipitation",
            reference_date=reference_date,
            data_kind="forecast",
            family="ncep",
            cycle="00",
            force=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(begin, range(2)))

    assert len({row.id for row, _reused in results}) == 1
    assert sorted(reused for _row, reused in results) == [False, True]
    active = [
        row
        for row in repository.source_runs_for_date(reference_date, cycle=None)
        if row.status in {"pending", "downloading", "downloaded", "processing"}
    ]
    assert len(active) == 1


def test_complete_required_fields_are_reused_when_only_optional_field_is_missing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    repository = repository_from_settings(settings)
    reference_date = date(2026, 7, 13)
    existing, _ = repository.register_source_run(
        source_id="ec",
        source_slug="ecmwf",
        product_slug="ec-precipitacao-brasil",
        data_kind="forecast",
        family="ecmwf",
        cycle="00",
        reference_date=reference_date,
        status="partial",
        max_lead_h=240,
        normalized_metadata={
            "quality": {"status": "complete", "issues": []},
            "weather_summary_synoptic": {
                "status": "complete",
                "missing_fields": [{"field_id": "cape_surface"}],
            },
        },
        idempotency_key="ec-optional-cape-missing",
    )
    repository.record_artifact(
        source_run_id=existing.id,
        kind="grid_forecast",
        storage_path="normalized/ec/day-001.csv.gz",
    )
    artifact_path = settings.artifact_root / "normalized/ec/day-001.csv.gz"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("grid", encoding="utf-8")

    selected, reused = ingest._begin_source_run(
        repository,
        source_id="ec",
        source_slug="ecmwf",
        product_slug="ec-precipitacao-brasil",
        reference_date=reference_date,
        data_kind="forecast",
        family="ecmwf",
        cycle="00",
        force=False,
    )

    assert reused is True
    assert selected.id == existing.id


def test_ensemble_reuse_requires_current_geometry_and_summary_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    repository = repository_from_settings(settings)
    existing, _ = repository.register_source_run(
        source_id="gefs",
        source_slug="noaa",
        product_slug="gefs-precipitation",
        data_kind="forecast",
        family="ncep",
        cycle="00",
        reference_date=date(2026, 7, 13),
        status="processed",
        max_lead_h=336,
        member_count=31,
        normalized_metadata={
            "weather_summary_ensemble": {
                "json_path": "normalized/gefs/summary.json",
                "geometry_version": "revised-contours-v5",
                "requested_basin_ids": list(WEATHER_SUMMARY_BASIN_IDS),
            }
        },
        idempotency_key="gefs-current-contours",
    )
    monkeypatch.setattr(
        ingest, "load_geometry_catalog", lambda: SimpleNamespace(version="revised-contours-v5")
    )

    assert ingest._is_reusable(existing, source_id="gefs") is True

    existing.normalized_metadata["weather_summary_ensemble"]["requested_basin_ids"] = list(
        WEATHER_SUMMARY_BASIN_IDS[:-1]
    )
    assert ingest._is_reusable(existing, source_id="gefs") is False


def test_daily_history_and_current_half_hour_snapshot_are_routed_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    repository = repository_from_settings(settings)
    issue_date = date(2026, 7, 13)
    cutoff = datetime(2026, 7, 13, 9, tzinfo=UTC)
    observed_calls: list[tuple[date, datetime | None]] = []

    def fake_model(_repository, _settings, *, model, reference_date, force):
        del force
        return ingest.SourceIngestionOutcome(
            model, reference_date.isoformat(), "processed", f"run-{model}"
        )

    def fake_imerg(
        _repository, _settings, *, product_date, cutoff_utc, force
    ):
        del force
        observed_calls.append((product_date, cutoff_utc))
        return ingest.SourceIngestionOutcome(
            "gpm_imerg", product_date.isoformat(), "processed", f"imerg-{product_date}"
        )

    monkeypatch.setattr(ingest, "ingest_deterministic_model", fake_model)
    monkeypatch.setattr(ingest, "ingest_ensemble_model", fake_model)
    monkeypatch.setattr(ingest, "ingest_imerg_date", fake_imerg)

    report = ingest.ingest_required_inputs(
        repository,
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff,
        imerg_history_days=2,
    )

    assert report.complete is True
    assert set(observed_calls) == {
        (date(2026, 7, 11), None),
        (date(2026, 7, 12), None),
        (issue_date, cutoff),
    }


def test_imerg_history_circuit_breaker_does_not_block_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    repository = repository_from_settings(settings)
    issue_date = date(2026, 7, 13)
    cutoff = datetime(2026, 7, 13, 9, tzinfo=UTC)
    model_calls: list[str] = []
    observed_calls: list[tuple[date, datetime | None]] = []

    def fake_model(_repository, _settings, *, model, reference_date, force):
        del force
        model_calls.append(model)
        return ingest.SourceIngestionOutcome(
            model, reference_date.isoformat(), "processed", f"run-{model}"
        )

    def failing_imerg(_repository, _settings, *, product_date, cutoff_utc, force):
        del force
        observed_calls.append((product_date, cutoff_utc))
        return ingest.SourceIngestionOutcome(
            "gpm_imerg",
            product_date.isoformat(),
            "failed",
            f"imerg-{product_date}",
            error_code="network_timeout",
        )

    monkeypatch.setattr(ingest, "ingest_deterministic_model", fake_model)
    monkeypatch.setattr(ingest, "ingest_ensemble_model", fake_model)
    monkeypatch.setattr(ingest, "ingest_imerg_date", failing_imerg)

    report = ingest.ingest_required_inputs(
        repository,
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff,
        imerg_history_days=4,
    )

    assert sorted(model_calls) == ["ec", "ecens", "gefs", "gfs"]
    assert observed_calls == [
        (date(2026, 7, 9), None),
        (date(2026, 7, 10), None),
    ]
    imerg_outcomes = [item for item in report.outcomes if item.source_id == "gpm_imerg"]
    assert [item.status for item in imerg_outcomes].count("failed") == 2
    assert [item.status for item in imerg_outcomes].count("skipped") == 3
    assert all(item.reference_date != issue_date.isoformat() or item.status == "skipped" for item in imerg_outcomes)
