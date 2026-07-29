"""Pipeline diario completo: inputs, pacote, redacao, auditoria e cleanup."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from ..core.openai_client import PROMPT_VERSION
from ..core.quality import (
    assess_analysis_packet,
    fallback_fingerprint,
    json_safe,
)
from ..core.schema import ANALYSIS_PACKET_SCHEMA_VERSION, AnalysisPacket
from ..ingestion.artifacts import atomic_write_bytes
from ..storage.artifacts import ArtifactStore
from ..storage.models import SummaryRun
from ..storage.repository import WeatherStorageRepository
from .generation import (
    EditorialGenerationError,
    build_editorial_client,
    generate_validated_summary,
)
from .ingest import ingest_required_inputs
from .packet_builder import PacketBuildBlocked, build_analysis_packet
from .publish import publish_summary

logger = logging.getLogger(__name__)


def repository_from_settings(settings: Settings, *, bootstrap: bool = True) -> WeatherStorageRepository:
    settings.ensure_directories()
    return WeatherStorageRepository(
        database_url=settings.database_url,
        artifact_root=settings.artifact_root,
        max_source_age_hours=settings.max_source_age_hours,
        bootstrap=bootstrap,
    )


def _packet_issue_time(packet: AnalysisPacket) -> datetime:
    return datetime.combine(
        packet.issue.date,
        packet.issue.time,
        tzinfo=ZoneInfo(packet.issue.timezone),
    )


def _run_dict(run: SummaryRun, *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "status": run.status,
        "run_id": run.id,
        "issue_date": run.issue_date.isoformat(),
        "cutoff_utc": run.cutoff_utc.isoformat(),
        "input_fingerprint": run.input_fingerprint,
        "schema_version": run.schema_version,
        "prompt_version": run.prompt_version,
        "llm_provider": run.llm_provider,
        "llm_model": run.llm_model,
        "llm_response_id": run.llm_response_id or None,
        "generation_attempts": run.generation_attempts,
        "summary_path": run.output_path or None,
        "audit_manifest_path": run.audit_manifest_path or None,
        "output_sha256": run.output_sha256 or None,
        "error_code": run.error_code or None,
        "error_message": run.error_message or None,
        "idempotent": idempotent,
    }


def build_packet(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    issue_date: date,
    cutoff_utc: datetime,
) -> AnalysisPacket:
    return build_analysis_packet(
        issue_date=issue_date,
        cutoff_utc=cutoff_utc,
        repository=repository,
        anchor_hour_utc=settings.anchor_hour_utc,
    )


def _published_packet_for_regeneration(
    repository: WeatherStorageRepository, *, issue_date: date
) -> AnalysisPacket | None:
    """Load the durable packet behind a published summary when available.

    Publication intentionally makes the large normalized grids disposable.
    A text-only forced regeneration must therefore use the packet retained in
    the audit catalog instead of requiring those already-cleaned grids again.
    """

    previous = repository.completed_summary_for_date(issue_date)
    if previous is None or not previous.analysis_packet:
        return None
    try:
        packet = AnalysisPacket.model_validate(previous.analysis_packet)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Pacote auditável inválido na publicação %s; reconstruindo inputs: %s",
            previous.id,
            exc,
        )
        return None
    if packet.issue.date != issue_date:
        logger.warning(
            "Pacote auditável da publicação %s tem data divergente; reconstruindo inputs",
            previous.id,
        )
        return None
    return packet


def write_packet_preview(
    packet: AnalysisPacket,
    settings: Settings,
) -> Path:
    path = settings.output_root / packet.issue.date.isoformat() / "00" / "analysis_packet.preview.json"
    content = (
        json.dumps(
            packet.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return atomic_write_bytes(path, content)


def _blocked_run(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    issue_date: date,
    cutoff_utc: datetime,
    fingerprint: str,
    assessment: dict[str, Any],
    message: str,
) -> SummaryRun:
    quality = assessment.get("quality_manifest")
    if not isinstance(quality, dict):
        quality = {
            "generation_allowed": False,
            "blockers": list(assessment.get("blockers") or ["analysis_packet_blocked"]),
            "assessment": assessment,
        }
    source_manifest = assessment.get("source_manifest") or assessment.get(
        "repository_source_manifest"
    )
    if not isinstance(source_manifest, dict):
        source_manifest = {"sources": []}
    run, _created = repository.create_or_get_summary_run(
        issue_date=issue_date,
        issue_time=datetime.now(UTC),
        cutoff_utc=cutoff_utc,
        input_fingerprint=fingerprint,
        schema_version=str(ANALYSIS_PACKET_SCHEMA_VERSION),
        prompt_version=PROMPT_VERSION,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        status="blocked",
        source_manifest=json_safe(source_manifest),
        quality_manifest=json_safe(quality),
    )
    return repository.transition_summary_run(
        run.id,
        "blocked",
        error_code="analysis_packet_blocked",
        error_message=message,
        source_manifest=json_safe(source_manifest),
        quality_manifest=json_safe(quality),
    )


def generate_daily_summary(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    issue_date: date,
    cutoff_utc: datetime,
    force: bool = False,
    reuse_published_packet: bool = True,
    client: Any | None = None,
) -> dict[str, Any]:
    if not force:
        completed = repository.completed_summary_for_date(issue_date)
        if completed is not None:
            return _run_dict(completed, idempotent=True)
    try:
        packet = (
            _published_packet_for_regeneration(repository, issue_date=issue_date)
            if force and reuse_published_packet
            else None
        )
        if packet is None:
            packet = build_packet(
                repository,
                settings,
                issue_date=issue_date,
                cutoff_utc=cutoff_utc,
            )
    except PacketBuildBlocked as exc:
        assessment = dict(json_safe(exc.assessment))
        run = _blocked_run(
            repository,
            settings,
            issue_date=issue_date,
            cutoff_utc=cutoff_utc,
            fingerprint=fallback_fingerprint(
                assessment,
                issue_date=issue_date.isoformat(),
                reason="automatic_packet_blocked",
            ),
            assessment=assessment,
            message=str(exc),
        )
        return _run_dict(run)
    except Exception as exc:
        detail = {"error_type": type(exc).__name__}
        fingerprint = fallback_fingerprint(
            detail,
            issue_date=issue_date.isoformat(),
            reason="packet_build_failed",
        )
        run, _created = repository.create_or_get_summary_run(
            issue_date=issue_date,
            issue_time=datetime.now(UTC),
            cutoff_utc=cutoff_utc,
            input_fingerprint=fingerprint,
            schema_version=str(ANALYSIS_PACKET_SCHEMA_VERSION),
            prompt_version=PROMPT_VERSION,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            status="failed",
            quality_manifest={
                "generation_allowed": False,
                "blockers": ["packet_build_failed"],
            },
        )
        run = repository.transition_summary_run(
            run.id,
            "failed",
            error_code="packet_build_failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        return _run_dict(run)

    assessment = assess_analysis_packet(packet)
    if not packet.quality.generation_allowed:
        run, _created = repository.create_or_get_summary_run(
            issue_date=packet.issue.date,
            issue_time=_packet_issue_time(packet),
            cutoff_utc=packet.issue.cutoff_utc,
            input_fingerprint=assessment.fingerprint,
            schema_version=str(packet.schema_version),
            prompt_version=PROMPT_VERSION,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            status="blocked",
            analysis_packet=assessment.packet_payload,
            source_manifest=assessment.source_manifest,
            quality_manifest=assessment.quality_manifest,
        )
        run = repository.transition_summary_run(
            run.id,
            "blocked",
            error_code="quality_gate_blocked",
            error_message="; ".join(packet.quality.blockers),
        )
        return _run_dict(run)

    run_fingerprint = assessment.fingerprint
    run_quality_manifest = dict(assessment.quality_manifest)
    if force:
        # A forced regeneration is a new auditable attempt. Never demote or
        # overwrite an already published run with the same meteorological input.
        run_fingerprint = hashlib.sha256(
            f"{assessment.fingerprint}|forced|{uuid.uuid4().hex}".encode()
        ).hexdigest()
        run_quality_manifest.update(
            {
                "forced_regeneration": True,
                "base_input_fingerprint": assessment.fingerprint,
            }
        )
    run, _created = repository.create_or_get_summary_run(
        issue_date=packet.issue.date,
        issue_time=_packet_issue_time(packet),
        cutoff_utc=packet.issue.cutoff_utc,
        input_fingerprint=run_fingerprint,
        schema_version=str(packet.schema_version),
        prompt_version=PROMPT_VERSION,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        status="pending",
        analysis_packet=assessment.packet_payload,
        source_manifest=assessment.source_manifest,
        quality_manifest=run_quality_manifest,
    )
    run, claimed = repository.claim_summary_run_for_generation(
        run.id,
        analysis_packet=assessment.packet_payload,
        source_manifest=assessment.source_manifest,
        quality_manifest=run_quality_manifest,
        force=force,
    )
    if not claimed:
        return _run_dict(run, idempotent=True)
    try:
        editorial_client = client or build_editorial_client(settings)
        generated = generate_validated_summary(
            packet,
            run_id=run.id,
            client=editorial_client,
            max_attempts=settings.llm_max_attempts,
        )
    except EditorialGenerationError as exc:
        attempt_count = max(len(exc.metadata.get("attempts") or []), 1)
        for _ in range(attempt_count):
            run = repository.transition_summary_run(
                run.id,
                "generating",
                increment_attempt=True,
                generation_metadata=exc.metadata,
            )
        run = repository.transition_summary_run(
            run.id,
            "failed",
            error_code="editorial_generation_failed",
            error_message=str(exc),
            generation_metadata=exc.metadata,
            validation_errors=exc.metadata.get("validation_errors") or [],
        )
        return _run_dict(run)
    except Exception as exc:
        run = repository.transition_summary_run(
            run.id,
            "failed",
            error_code="editorial_adapter_failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        return _run_dict(run)

    for _ in range(generated.attempts):
        run = repository.transition_summary_run(
            run.id,
            "generating",
            increment_attempt=True,
            generation_metadata=generated.metadata,
            validation_errors=generated.validation_errors,
            llm_response_id=generated.response_id,
        )
    try:
        publication = publish_summary(
            output_root=settings.output_root,
            issue_date=packet.issue.date,
            cycle="00",
            run_id=run.id,
            attempt=run.generation_attempts,
            summary_text=generated.text,
            analysis_packet=assessment.packet_payload,
            source_manifest=assessment.source_manifest,
            quality_manifest=run_quality_manifest,
            generation_metadata=generated.metadata,
            validation_errors=generated.validation_errors,
            schema_version=str(packet.schema_version),
            prompt_version=PROMPT_VERSION,
        )
    except Exception as exc:
        run = repository.transition_summary_run(
            run.id,
            "failed",
            error_code="publication_failed",
            error_message=f"{type(exc).__name__}: {exc}",
            generation_metadata=generated.metadata,
            validation_errors=generated.validation_errors,
        )
        return _run_dict(run)

    run = repository.transition_summary_run(
        run.id,
        "completed",
        generation_metadata=generated.metadata,
        validation_errors=generated.validation_errors,
        output_text=generated.text,
        output_path=publication.summary_path,
        audit_manifest_path=publication.manifest_path,
        output_sha256=publication.summary_sha256,
        llm_response_id=generated.response_id,
        published_at=datetime.now(UTC),
    )

    # Once the packet and publication are durable, full deterministic/IMERG
    # grids are disposable. Compact basin/ensemble/synoptic audit artifacts
    # remain. Cleanup failure must not invalidate an already published text.
    cleanup: dict[str, Any]
    try:
        eligible = repository.expire_transient_artifacts_through(issue_date)
        cleaned = repository.cleanup_expired_artifacts()
        stale_staging = repository.cleanup_stale_staging_directories()
        stale_output_staging = ArtifactStore(settings.output_root).cleanup_stale_staging_directories()
        all_stale_staging = (*stale_staging, *stale_output_staging)
        cleanup = {
            "eligible": eligible,
            "deleted": sum(item.status in {"deleted", "already_missing"} for item in cleaned),
            "bytes_reclaimed": sum(item.bytes_reclaimed for item in cleaned),
            "errors": [item.error for item in cleaned if item.error],
            "staging_deleted": sum(item.status == "deleted" for item in all_stale_staging),
            "staging_errors": [item.error for item in all_stale_staging if item.error],
        }
    except Exception as exc:
        cleanup = {"error": f"{type(exc).__name__}: {exc}"}
    result = _run_dict(run)
    result["cleanup"] = cleanup
    return result


def run_daily(
    repository: WeatherStorageRepository,
    settings: Settings,
    *,
    issue_date: date,
    cutoff_utc: datetime,
    force: bool = False,
    imerg_history_days: int = 30,
    client: Any | None = None,
    freeze_cutoff: bool = False,
) -> dict[str, Any]:
    if not force:
        completed = repository.completed_summary_for_date(issue_date)
        if completed is not None:
            return {"status": "completed", "summary": _run_dict(completed, idempotent=True)}
    ingestion = ingest_required_inputs(
        repository,
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff_utc,
        force=force,
        imerg_history_days=imerg_history_days,
    )
    # The default cutoff is captured just before downloads start. Advance it
    # after ingestion so artifacts produced by this same run are eligible for
    # the packet. Explicit historical/as-of callers can freeze it.
    packet_cutoff = (
        cutoff_utc.astimezone(UTC)
        if freeze_cutoff
        else max(cutoff_utc.astimezone(UTC), datetime.now(UTC))
    )
    summary = generate_daily_summary(
        repository,
        settings,
        issue_date=issue_date,
        cutoff_utc=packet_cutoff,
        force=force,
        reuse_published_packet=False,
        client=client,
    )
    return {
        "status": summary["status"],
        "ingestion": ingestion.to_dict(),
        "summary": summary,
    }


__all__ = [
    "build_packet",
    "generate_daily_summary",
    "repository_from_settings",
    "run_daily",
    "write_packet_preview",
]
