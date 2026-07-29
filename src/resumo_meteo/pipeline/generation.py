"""Redacao, reparo editorial e validacao sem dependencia de persistencia."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..core.openai_client import (
    PROMPT_VERSION,
    OpenAIEditorialResult,
    WeatherSummaryGenerationError,
    WeatherSummaryOpenAIClient,
    expected_heading,
)
from ..core.output_validator import (
    SummaryValidationError,
    validate_summary_output,
    validate_window_output,
)
from ..core.quality import json_safe
from ..core.schema import AnalysisPacket, WindowId

SUMMARY_DISCLAIMER_VERSION = "weather-summary-disclaimer-v1"
SUMMARY_DISCLAIMER = (
    "Este resumo foi gerado com apoio de inteligência artificial, a partir das "
    "previsões das rodadas 00Z dos modelos meteorológicos GFS, GEFS, ECMWF e "
    "ECENS, combinadas com dados de chuva por satélite (IMERG) e climatologia "
    "histórica das bacias. As informações são indicativas, sujeitas a erros e "
    "devem ser interpretadas com cautela; também podem ser atualizadas conforme "
    "novas rodadas dos modelos forem disponibilizadas."
)


class EditorialGenerationError(RuntimeError):
    """The provider failed or exhausted the editorial validation budget."""

    def __init__(self, message: str, *, metadata: dict[str, Any]) -> None:
        self.metadata = metadata
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GeneratedSummary:
    text: str
    sha256: str
    response_id: str
    attempts: int
    metadata: dict[str, Any]
    validation_errors: tuple[dict[str, Any], ...]


def _with_summary_disclaimer(text: str) -> str:
    """Append the immutable reader disclaimer after LLM-output validation."""

    return f"{text}\n\n{SUMMARY_DISCLAIMER}"


def _normalize_window(text: str, packet: AnalysisPacket, window_id: WindowId) -> str:
    heading = expected_heading(packet, window_id)
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    position = normalized.find(heading)
    if position >= 0:
        normalized = normalized[position:]
        normalized = re.sub(rf"({re.escape(heading)})[ \t]+", r"\1\n", normalized, count=1)
    lines = [line.strip() for line in normalized.splitlines()]
    try:
        heading_index = lines.index(heading)
    except ValueError:
        return normalized
    paragraph = []
    for line in lines[heading_index + 1 :]:
        if not line:
            continue
        if line.endswith(":") and "ª pêntada" in line:
            break
        paragraph.append(line)
    return f"{heading}\n{' '.join(paragraph)}" if paragraph else normalized


def _result_audit(result: OpenAIEditorialResult) -> dict[str, Any]:
    return {
        "response_id": result.response_id,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "usage": result.usage.model_dump(mode="json", exclude_none=False),
        "request_metadata": result.request_metadata,
        # The exact candidate is intentionally retained for editorial audit.
        "text": result.text,
    }


def build_editorial_client(settings: Settings) -> WeatherSummaryOpenAIClient:
    return WeatherSummaryOpenAIClient(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=float(settings.llm_timeout_seconds),
        base_url=settings.llm_base_url,
        api_mode=settings.llm_api_mode,
        max_attempts=1,
        max_output_tokens=settings.llm_max_output_tokens,
    )


def generate_validated_summary(
    packet: AnalysisPacket,
    *,
    run_id: int | str,
    client: Any,
    max_attempts: int = 2,
) -> GeneratedSummary:
    """Generate and validate pentads independently before publication.

    A rejected section is repaired in isolation. Approved sections are never
    handed back to the model, which prevents a local factual error from
    rewriting two valid pentads and avoids boilerplate being reintroduced.
    """

    if not packet.quality.generation_allowed:
        raise EditorialGenerationError(
            "Pacote bloqueado pela qualidade dos inputs.",
            metadata={"blockers": list(packet.quality.blockers)},
        )
    attempts: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []
    for attempt_number in range(1, max(int(max_attempts), 1) + 1):
        attempt: dict[str, Any] = {"attempt": attempt_number, "mode": "per_window"}
        sections: list[str] = []
        response_id = ""
        failed = False
        for window_id in (WindowId.P1, WindowId.P2, WindowId.P3):
            try:
                generated = client.generate(
                    packet,
                    audit_metadata={
                        "run_id": str(run_id),
                        "editorial_attempt": str(attempt_number),
                    },
                    window_id=window_id,
                    prior_sections=tuple(sections),
                )
            except WeatherSummaryGenerationError as exc:
                attempt.update(
                    status="request_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    window_id=window_id.value,
                )
                attempts.append(attempt)
                raise EditorialGenerationError(str(exc), metadata={"attempts": attempts}) from exc

            normalized = _normalize_window(generated.text, packet, window_id)
            window_audit: dict[str, Any] = {
                "window_id": window_id.value,
                "generated": {**_result_audit(generated), "normalized_text": normalized},
            }
            try:
                section = validate_window_output(normalized, packet, window_id)
            except SummaryValidationError as exc:
                invalid = {
                    "attempt": attempt_number,
                    "window_id": window_id.value,
                    "response_id": generated.response_id,
                    "code": exc.code,
                    "message": exc.message,
                    "candidate_text": normalized,
                }
                validation_errors.append(invalid)
                window_audit["validation"] = {"status": "failed", **invalid}
                try:
                    repaired = client.repair_window(
                        packet,
                        window_id=window_id,
                        invalid_text=normalized,
                        validation_error_code=exc.code,
                        validation_error_message=exc.message,
                        audit_metadata={
                            "run_id": str(run_id),
                            "editorial_attempt": str(attempt_number),
                        },
                        prior_sections=tuple(sections),
                    )
                except (AttributeError, WeatherSummaryGenerationError) as repair_exc:
                    window_audit["repair"] = {
                        "status": "request_failed",
                        "error_type": type(repair_exc).__name__,
                        "error": str(repair_exc),
                    }
                    attempt.setdefault("windows", []).append(window_audit)
                    failed = True
                    break
                repaired_text = _normalize_window(repaired.text, packet, window_id)
                window_audit["repair"] = {
                    **_result_audit(repaired),
                    "normalized_text": repaired_text,
                }
                try:
                    section = validate_window_output(repaired_text, packet, window_id)
                except SummaryValidationError as repair_exc:
                    repair_error = {
                        "attempt": attempt_number,
                        "window_id": window_id.value,
                        "response_id": repaired.response_id,
                        "code": repair_exc.code,
                        "message": repair_exc.message,
                        "candidate_text": repaired_text,
                    }
                    validation_errors.append(repair_error)
                    window_audit["repair"]["validation"] = {
                        "status": "failed",
                        **repair_error,
                    }
                    attempt.setdefault("windows", []).append(window_audit)
                    failed = True
                    break
                window_audit["repair"]["validation"] = {"status": "accepted"}
                response_id = repaired.response_id
            else:
                window_audit["validation"] = {"status": "accepted"}
                response_id = generated.response_id
            sections.append(f"{section.heading}\n{section.paragraph}")
            attempt.setdefault("windows", []).append(window_audit)

        if failed:
            attempt["status"] = "window_repair_failed"
            attempts.append(attempt)
            continue

        candidate = "\n\n".join(sections)
        attempt["candidate_text"] = candidate
        try:
            validated = validate_summary_output(candidate, packet)
        except SummaryValidationError as exc:
            # This should be unreachable because every section was accepted, but
            # retain a fail-closed combined check at the publication boundary.
            invalid = {
                "attempt": attempt_number,
                "code": exc.code,
                "message": exc.message,
                "candidate_text": candidate,
            }
            validation_errors.append(invalid)
            attempt["validation"] = {"status": "failed", **invalid}
            attempt["status"] = "combined_validation_failed"
            attempts.append(attempt)
            continue
        attempt["status"] = "validated_with_window_repairs" if any(
            "repair" in window for window in attempt["windows"]
        ) else "validated"

        published_text = _with_summary_disclaimer(validated.text)
        published_sha256 = hashlib.sha256(published_text.encode("utf-8")).hexdigest()
        attempt["output_sha256"] = published_sha256
        attempts.append(attempt)
        metadata = {
            "schema_version": "weather-summary-generation.v2",
            "prompt_version": PROMPT_VERSION,
            "generation_mode": "sequential_window_validation_with_scoped_repair",
            "attempts": attempts,
            "final_response_id": response_id,
            "validated_sections": [
                section.model_dump(mode="json") for section in validated.sections
            ],
            "publication_disclaimer": {
                "version": SUMMARY_DISCLAIMER_VERSION,
                "text": SUMMARY_DISCLAIMER,
            },
        }
        return GeneratedSummary(
            text=published_text,
            sha256=published_sha256,
            response_id=response_id,
            attempts=attempt_number,
            metadata=json_safe(metadata),
            validation_errors=tuple(json_safe(validation_errors)),
        )

    metadata = {
        "schema_version": "weather-summary-generation.v2",
        "prompt_version": PROMPT_VERSION,
        "attempts": attempts,
        "validation_errors": validation_errors,
    }
    raise EditorialGenerationError(
        "O modelo não produziu uma saída válida dentro do limite editorial.",
        metadata=json_safe(metadata),
    )


__all__ = [
    "EditorialGenerationError",
    "GeneratedSummary",
    "SUMMARY_DISCLAIMER",
    "SUMMARY_DISCLAIMER_VERSION",
    "build_editorial_client",
    "generate_validated_summary",
]
