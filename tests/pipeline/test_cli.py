from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from resumo_meteo import cli
from resumo_meteo.config import Settings
from resumo_meteo.pipeline.runner import repository_from_settings

ISSUE_DATE = date(2026, 7, 13)
CUTOFF = datetime(2026, 7, 13, 12, tzinfo=UTC)


@pytest.fixture
def cli_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "RESUMO_METEO_DATABASE_URL", f"sqlite:///{tmp_path / 'var' / 'metadata.sqlite3'}"
    )
    monkeypatch.setenv("RESUMO_METEO_ARTIFACT_ROOT", str(tmp_path / "var" / "artifacts"))
    monkeypatch.setenv("RESUMO_METEO_OUTPUT_ROOT", str(tmp_path / "var" / "outputs"))
    monkeypatch.setenv("RESUMO_METEO_LOG_LEVEL", "CRITICAL")
    monkeypatch.setenv("WEATHER_SUMMARY_LLM_PROVIDER", "test-provider")
    monkeypatch.setenv("WEATHER_SUMMARY_LLM_MODEL", "test-model")
    for name in (
        "WEATHER_SUMMARY_LLM_API_KEY",
        "OPENAI_API_KEY",
        "NASA_EARTHDATA_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _read_json(capsys: pytest.CaptureFixture[str]) -> dict:
    output = capsys.readouterr().out
    return json.loads(output)


def test_settings_do_not_forward_openai_key_to_a_custom_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.delenv("WEATHER_SUMMARY_LLM_API_KEY", raising=False)
    monkeypatch.setenv("WEATHER_SUMMARY_LLM_BASE_URL", "https://editorial.invalid/api")

    custom = Settings.from_env(project_root=tmp_path)
    assert custom.llm_api_key is None

    monkeypatch.setenv("WEATHER_SUMMARY_LLM_BASE_URL", "https://api.openai.com/v1")
    official = Settings.from_env(project_root=tmp_path)
    assert official.llm_api_key == "openai-secret"


def test_cli_init_db_and_status_report_persisted_rows_without_network(
    cli_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init-db", "--json"]) == 0
    initialized = _read_json(capsys)
    assert initialized["status"] == "ok"
    assert initialized["database"] == "sqlite"
    assert (cli_project / "var" / "metadata.sqlite3").is_file()

    settings = Settings.from_env(project_root=cli_project)
    repository = repository_from_settings(settings)
    source, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        family="ncep",
        cycle="00",
        reference_date=ISSUE_DATE,
        run_datetime_utc=datetime(2026, 7, 13, tzinfo=UTC),
        available_at=CUTOFF - timedelta(hours=1),
        status="processed",
        max_lead_h=336,
    )
    repository.record_artifact(
        source_run_id=source.id,
        kind="normalized_grid",
        storage_path="normalized/gfs/day-001.csv.gz",
        size_bytes=123,
    )
    summary, _ = repository.create_or_get_summary_run(
        issue_date=ISSUE_DATE,
        issue_time=CUTOFF,
        cutoff_utc=CUTOFF,
        input_fingerprint="a" * 64,
        schema_version="2",
        prompt_version="weather-summary-editorial-v31",
        llm_provider="test-provider",
        llm_model="test-model",
        status="blocked",
    )

    exit_code = cli.main(
        [
            "status",
            "--date",
            ISSUE_DATE.isoformat(),
            "--cycle",
            "00z",
            "--cutoff",
            CUTOFF.isoformat(),
            "--json",
        ]
    )
    payload = _read_json(capsys)

    assert exit_code == 0
    assert payload["status"] == "found"
    assert payload["sources"] == [
        {
            "id": source.id,
            "source_id": "gfs",
            "reference_date": ISSUE_DATE.isoformat(),
            "cycle": "00",
            "status": "processed",
            "attempt_count": 0,
            "max_lead_h": 336,
            "member_count": None,
            "artifact_count": 1,
            "available_artifact_bytes": 123,
            "error_code": None,
            "error_message": None,
        }
    ]
    assert payload["summaries"] == [
        {
            "id": summary.id,
            "status": "blocked",
            "model": "test-model",
            "generation_attempts": 0,
            "output_path": None,
            "error_code": None,
            "error_message": None,
        }
    ]


def test_cli_cleanup_supports_dry_run_then_deletes_expired_transient(
    cli_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_env(project_root=cli_project)
    repository = repository_from_settings(settings)
    source, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        cycle="00",
        reference_date=ISSUE_DATE,
        status="processed",
    )
    relative_path = "raw/gfs/expired.grib2"
    artifact_path = repository.artifacts.resolve(relative_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"discard after normalization")
    repository.record_artifact(
        source_run_id=source.id,
        kind="raw_grib",
        storage_path=relative_path,
        retention_class="transient",
        size_bytes=artifact_path.stat().st_size,
        delete_after=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert cli.main(["cleanup", "--dry-run", "--json"]) == 0
    preview = _read_json(capsys)
    assert preview["artifact_count"] == 1
    assert preview["artifacts"][0]["status"] == "would_delete"
    assert artifact_path.is_file()

    assert cli.main(["cleanup", "--json"]) == 0
    cleaned = _read_json(capsys)
    assert cleaned["artifact_count"] == 1
    assert cleaned["bytes_reclaimed"] == len(b"discard after normalization")
    assert cleaned["artifacts"][0]["status"] == "deleted"
    assert not artifact_path.exists()


def test_cli_build_packet_is_blocked_cleanly_when_database_has_no_inputs(
    cli_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main(
        [
            "build-packet",
            "--date",
            ISSUE_DATE.isoformat(),
            "--cycle",
            "00",
            "--cutoff",
            CUTOFF.isoformat(),
            "--json",
        ]
    )
    payload = _read_json(capsys)

    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["assessment"]["quality_manifest"]["generation_allowed"] is False
    assert "forecast_precipitation_absent" in payload["assessment"]["blockers"]
