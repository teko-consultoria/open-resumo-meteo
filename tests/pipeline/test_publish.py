from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from resumo_meteo.pipeline import publish


def _publication_kwargs(output_root: Path) -> dict:
    return {
        "output_root": output_root,
        "issue_date": date(2026, 7, 13),
        "cycle": "00",
        "run_id": "run-safe_01",
        "attempt": 2,
        "summary_text": "  resumo final\n",
        "analysis_packet": {"schema_version": 2, "packet": "value"},
        "source_manifest": {"schema_version": "sources.v1", "sources": []},
        "quality_manifest": {"generation_allowed": True, "blockers": []},
        "generation_metadata": {"response_id": "response-1"},
        "validation_errors": [{"code": "first_draft_invalid"}],
        "schema_version": "2",
        "prompt_version": "weather-summary-editorial-v31",
    }


def test_publish_writes_immutable_audit_layout_and_matching_checksums(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"

    publication = publish.publish_summary(**_publication_kwargs(output_root))

    assert publication.directory == "2026-07-13/00/run-run-safe_01-attempt-2"
    final_dir = output_root / publication.directory
    assert final_dir.is_dir()
    assert {path.name for path in final_dir.iterdir()} == {
        "analysis_packet.json",
        "generation.json",
        "manifest.json",
        "quality.json",
        "source_manifest.json",
        "summary.txt",
        "validation.json",
    }
    assert (final_dir / "summary.txt").read_text(encoding="utf-8") == "resumo final"

    manifest = json.loads((final_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "analysis_packet_schema_version": "2",
        "attempt": 2,
        "cycle": "00",
        "files": manifest["files"],
        "issue_date": "2026-07-13",
        "prompt_version": "weather-summary-editorial-v31",
        "run_id": "run-safe_01",
        "schema_version": "weather-summary-publication.v2",
    }
    assert {item["name"] for item in manifest["files"]} == {
        "analysis_packet.json",
        "generation.json",
        "quality.json",
        "source_manifest.json",
        "summary.txt",
        "validation.json",
    }
    for item in manifest["files"]:
        content = (final_dir / item["name"]).read_bytes()
        assert item["size_bytes"] == len(content)
        assert item["sha256"] == hashlib.sha256(content).hexdigest()

    validation = json.loads((final_dir / "validation.json").read_text(encoding="utf-8"))
    assert validation["accepted"] is True
    assert validation["errors_before_acceptance"] == [{"code": "first_draft_invalid"}]
    assert publication.summary_path == f"{publication.directory}/summary.txt"
    assert publication.manifest_path == f"{publication.directory}/manifest.json"
    assert publication.summary_sha256 == hashlib.sha256(b"resumo final").hexdigest()

    with pytest.raises(FileExistsError):
        publish.publish_summary(**_publication_kwargs(output_root))


def test_publish_removes_staging_directory_when_any_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "outputs"
    original_write = publish._write
    write_count = 0

    def failing_write(path: Path, content: bytes) -> dict:
        nonlocal write_count
        write_count += 1
        if write_count == 3:
            raise OSError("simulated filesystem failure")
        return original_write(path, content)

    monkeypatch.setattr(publish, "_write", failing_write)

    with pytest.raises(OSError, match="simulated filesystem failure"):
        publish.publish_summary(**_publication_kwargs(output_root))

    parent = output_root / "2026-07-13" / "00"
    assert parent.is_dir()
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("cycle", ["06", "12z", "18Z"])
def test_publish_rejects_every_cycle_except_00z(tmp_path: Path, cycle: str) -> None:
    kwargs = {**_publication_kwargs(tmp_path / "outputs"), "cycle": cycle}

    with pytest.raises(ValueError, match="Somente a rodada 00z"):
        publish.publish_summary(**kwargs)

    assert not (tmp_path / "outputs").exists()
