from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from editorial_samples import VALID_SUMMARY

from resumo_meteo.config import Settings
from resumo_meteo.core.openai_client import (
    PROMPT_VERSION,
    OpenAIEditorialResult,
    OpenAIUsage,
)
from resumo_meteo.core.schema import AnalysisPacket, WindowId
from resumo_meteo.pipeline import runner
from resumo_meteo.pipeline.generation import SUMMARY_DISCLAIMER

PUBLISHED_SUMMARY = f"{VALID_SUMMARY}\n\n{SUMMARY_DISCLAIMER}"


def _settings(tmp_path: Path, *, max_attempts: int = 1) -> Settings:
    return Settings(
        project_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'metadata.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        output_root=tmp_path / "outputs",
        timezone="America/Sao_Paulo",
        cycle="00",
        llm_provider="test-provider",
        llm_base_url="https://invalid.test/api",
        llm_model="test-model",
        llm_api_key=None,
        llm_api_mode="chat_completions",
        llm_timeout_seconds=1,
        llm_max_attempts=max_attempts,
        llm_max_output_tokens=1200,
        nasa_earthdata_token=None,
        download_workers=1,
        log_level="CRITICAL",
        anchor_hour_utc=0,
        max_source_age_hours=42,
        source_retention_days=3,
    )


def _packet() -> AnalysisPacket:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "analysis_packet_v2.json"
    return AnalysisPacket.model_validate_json(fixture.read_text(encoding="utf-8"))


def _sections(text: str = VALID_SUMMARY) -> dict[WindowId, str]:
    sections = text.split("\n\n")
    return dict(zip((WindowId.P1, WindowId.P2, WindowId.P3), sections, strict=True))


class FakeEditorialClient:
    def __init__(self, *, outputs: dict[WindowId, str], repair_output: str | None = None) -> None:
        self.outputs = outputs
        self.repair_output = repair_output
        self.calls = 0

    def _result(self, text: str) -> OpenAIEditorialResult:
        self.calls += 1
        return OpenAIEditorialResult(
            text=text,
            response_id=f"fake-response-{self.calls}",
            model="test-model",
            usage=OpenAIUsage(input_tokens=10, output_tokens=20, total_tokens=30),
            prompt_version=PROMPT_VERSION,
            request_metadata={},
        )

    def generate(self, _packet, *, audit_metadata=None, window_id=None, prior_sections=()):
        del audit_metadata, prior_sections
        return self._result(self.outputs[WindowId(window_id)])

    def repair_window(
        self,
        _packet,
        *,
        window_id,
        invalid_text,
        validation_error_code,
        validation_error_message,
        audit_metadata=None,
        prior_sections=(),
    ):
        del window_id, invalid_text, validation_error_code, validation_error_message, audit_metadata, prior_sections
        if self.repair_output is None:
            raise AssertionError("repair_window não deveria ser chamado")
        return self._result(self.repair_output)


class NeverCalledClient:
    def generate(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("o cliente editorial não deveria ser chamado")


class FailingClient:
    def generate(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("editorial unavailable")


def test_runner_publishes_persists_and_reuses_completed_run_then_cleans_raw_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _packet()
    settings = _settings(tmp_path)
    repository = runner.repository_from_settings(settings)
    monkeypatch.setattr(runner, "build_packet", lambda *args, **kwargs: packet)

    source, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        cycle="00",
        reference_date=packet.issue.date,
        status="processed",
    )
    raw_relative_path = "raw/gfs/run-00/grid.grib2"
    raw_path = repository.artifacts.resolve(raw_relative_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"large disposable grid")
    raw_artifact, _ = repository.record_artifact(
        source_run_id=source.id,
        kind="raw_grib",
        storage_path=raw_relative_path,
        retention_class="transient",
        size_bytes=raw_path.stat().st_size,
        delete_after=datetime.now(UTC) + timedelta(days=3),
    )

    result = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        client=FakeEditorialClient(outputs=_sections()),
    )

    assert result["status"] == "completed"
    assert result["generation_attempts"] == 1
    assert result["cleanup"] == {
        "eligible": 1,
        "deleted": 1,
        "bytes_reclaimed": len(b"large disposable grid"),
        "errors": [],
        "staging_deleted": 0,
        "staging_errors": [],
    }
    assert not raw_path.exists()
    assert repository.source_artifacts(source.id)[0].id == raw_artifact.id
    assert repository.source_artifacts(source.id)[0].status == "deleted"

    summary_path = settings.output_root / result["summary_path"]
    manifest_path = settings.output_root / result["audit_manifest_path"]
    assert summary_path.read_text(encoding="utf-8") == PUBLISHED_SUMMARY
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["run_id"] == result["run_id"]

    stored = repository.get_summary_run(result["run_id"])
    assert stored is not None
    assert stored.status == "completed"
    assert stored.output_text == PUBLISHED_SUMMARY
    assert stored.analysis_packet["schema_version"] == 2
    assert stored.generation_metadata["final_response_id"] == "fake-response-3"
    assert stored.llm_response_id == "fake-response-3"
    assert stored.output_sha256 == result["output_sha256"]
    assert stored.published_at is not None

    reused = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        client=NeverCalledClient(),
    )
    assert reused["run_id"] == result["run_id"]
    assert reused["idempotent"] is True
    assert len(repository.summary_runs_for_date(packet.issue.date)) == 1


def test_force_generation_reuses_published_packet_after_grid_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _packet()
    settings = _settings(tmp_path)
    repository = runner.repository_from_settings(settings)
    monkeypatch.setattr(runner, "build_packet", lambda *args, **kwargs: packet)
    first = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        client=FakeEditorialClient(outputs=_sections()),
    )
    assert first["status"] == "completed"

    def should_not_rebuild(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a regeneração editorial deve reutilizar o pacote publicado")

    monkeypatch.setattr(runner, "build_packet", should_not_rebuild)
    regenerated = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        force=True,
        client=FakeEditorialClient(outputs=_sections()),
    )

    assert regenerated["status"] == "completed"
    assert regenerated["run_id"] != first["run_id"]
    assert len(repository.summary_runs_for_date(packet.issue.date)) == 2


def test_runner_persists_blocked_packet_and_never_calls_llm(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = runner.repository_from_settings(settings)
    issue_date = _packet().issue.date
    cutoff = datetime(2026, 7, 10, 12, tzinfo=UTC)

    result = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff,
        client=NeverCalledClient(),
    )

    assert result["status"] == "blocked"
    assert result["error_code"] == "analysis_packet_blocked"
    assert result["generation_attempts"] == 0
    rows = repository.summary_runs_for_date(issue_date)
    assert len(rows) == 1
    assert rows[0].status == "blocked"
    assert rows[0].quality_manifest["generation_allowed"] is False
    assert "forecast_precipitation_absent" in rows[0].quality_manifest["blockers"]
    assert rows[0].finished_at is not None


def test_runner_persists_rejected_drafts_and_validator_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _packet()
    settings = _settings(tmp_path, max_attempts=1)
    repository = runner.repository_from_settings(settings)
    monkeypatch.setattr(runner, "build_packet", lambda *args, **kwargs: packet)
    rejected = {
        window_id: f"rascunho inválido para {window_id.value}"
        for window_id in (WindowId.P1, WindowId.P2, WindowId.P3)
    }

    result = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        client=FakeEditorialClient(outputs=rejected, repair_output="reparo ainda inválido"),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "editorial_generation_failed"
    assert result["generation_attempts"] == 1
    stored = repository.get_summary_run(result["run_id"])
    assert stored is not None
    assert stored.output_path == ""
    assert stored.generation_metadata["attempts"][0]["windows"][0]["generated"]["normalized_text"] == (
        "rascunho inválido para p1"
    )
    assert stored.generation_metadata["attempts"][0]["windows"][0]["repair"]["text"] == (
        "reparo ainda inválido"
    )
    assert len(stored.validation_errors) == 2
    assert {item["code"] for item in stored.validation_errors} == {"invalid_structure"}
    assert stored.validation_errors[-1]["candidate_text"] == "reparo ainda inválido"
    assert stored.error_message


def test_run_daily_advances_cutoff_after_its_own_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    repository = runner.repository_from_settings(settings)
    original_cutoff = datetime(2026, 7, 10, 12, tzinfo=UTC)
    observed: dict[str, datetime] = {}

    class Report:
        def to_dict(self):
            return {"complete": True}

    monkeypatch.setattr(runner, "ingest_required_inputs", lambda *args, **kwargs: Report())

    def generate(*args, **kwargs):
        observed["cutoff"] = kwargs["cutoff_utc"]
        return {"status": "completed"}

    monkeypatch.setattr(runner, "generate_daily_summary", generate)

    result = runner.run_daily(
        repository,
        settings,
        issue_date=date.fromisoformat("2026-07-10"),
        cutoff_utc=original_cutoff,
    )

    assert result["status"] == "completed"
    assert observed["cutoff"] > original_cutoff

    runner.run_daily(
        repository,
        settings,
        issue_date=date.fromisoformat("2026-07-10"),
        cutoff_utc=original_cutoff,
        freeze_cutoff=True,
    )
    assert observed["cutoff"] == original_cutoff


def test_forced_generation_never_demotes_a_completed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _packet()
    settings = _settings(tmp_path)
    repository = runner.repository_from_settings(settings)
    monkeypatch.setattr(runner, "build_packet", lambda *args, **kwargs: packet)

    first = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        client=FakeEditorialClient(outputs=_sections()),
    )
    forced = runner.generate_daily_summary(
        repository,
        settings,
        issue_date=packet.issue.date,
        cutoff_utc=packet.issue.cutoff_utc,
        force=True,
        client=FailingClient(),
    )

    assert first["status"] == "completed"
    assert forced["status"] == "failed"
    assert forced["run_id"] != first["run_id"]
    assert repository.completed_summary_for_date(packet.issue.date).id == first["run_id"]
    assert repository.get_summary_run(first["run_id"]).status == "completed"
