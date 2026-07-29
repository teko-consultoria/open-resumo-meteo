"""Publicacao atomica e auditavel no filesystem interno."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class Publication:
    directory: str
    summary_path: str
    manifest_path: str
    summary_sha256: str
    files: tuple[dict[str, Any], ...]


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode(
        "utf-8"
    )


def _write(path: Path, content: bytes) -> dict[str, Any]:
    path.write_bytes(content)
    return {
        "name": path.name,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def publish_summary(
    *,
    output_root: str | Path,
    issue_date: date,
    cycle: str,
    run_id: int | str,
    attempt: int,
    summary_text: str,
    analysis_packet: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    quality_manifest: Mapping[str, Any],
    generation_metadata: Mapping[str, Any],
    validation_errors: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    schema_version: str,
    prompt_version: str,
) -> Publication:
    """Create one immutable release; no reader can observe a partial directory."""

    root = Path(output_root).expanduser().resolve()
    normalized_cycle = str(cycle).strip().replace("z", "").replace("Z", "")
    if normalized_cycle != "00":
        raise ValueError("Somente a rodada 00z pode ser publicada")
    safe_run_id = str(run_id)
    if not _SAFE_RUN_ID.fullmatch(safe_run_id):
        raise ValueError("run_id inseguro")
    attempt_number = max(int(attempt), 1)
    parent = root / issue_date.isoformat() / "00"
    parent.mkdir(parents=True, exist_ok=True)
    final_dir = parent / f"run-{safe_run_id}-attempt-{attempt_number}"
    if final_dir.exists():
        raise FileExistsError(f"Publicação já existe: {final_dir}")
    staging_root = root / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{final_dir.name}.", suffix=".tmp", dir=staging_root)
    )
    try:
        text = str(summary_text or "").strip()
        files = [
            _write(temporary / "summary.txt", text.encode("utf-8")),
            _write(temporary / "analysis_packet.json", _json_bytes(analysis_packet)),
            _write(temporary / "source_manifest.json", _json_bytes(source_manifest)),
            _write(temporary / "quality.json", _json_bytes(quality_manifest)),
            _write(temporary / "generation.json", _json_bytes(generation_metadata)),
            _write(
                temporary / "validation.json",
                _json_bytes(
                    {
                        "schema_version": "weather-summary-validation.v1",
                        "accepted": True,
                        "errors_before_acceptance": list(validation_errors),
                    }
                ),
            ),
        ]
        manifest = {
            "schema_version": "weather-summary-publication.v2",
            "analysis_packet_schema_version": schema_version,
            "prompt_version": prompt_version,
            "issue_date": issue_date.isoformat(),
            "cycle": "00",
            "run_id": safe_run_id,
            "attempt": attempt_number,
            "files": files,
        }
        manifest_entry = _write(temporary / "manifest.json", _json_bytes(manifest))
        os.replace(temporary, final_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    relative_dir = final_dir.relative_to(root).as_posix()
    summary = next(item for item in files if item["name"] == "summary.txt")
    return Publication(
        directory=relative_dir,
        summary_path=f"{relative_dir}/summary.txt",
        manifest_path=f"{relative_dir}/manifest.json",
        summary_sha256=str(summary["sha256"]),
        files=tuple([*files, manifest_entry]),
    )


__all__ = ["Publication", "publish_summary"]
