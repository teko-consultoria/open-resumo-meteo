"""Deterministic quality and audit helpers for the daily weather summary.

This module is intentionally free of database and network dependencies.  It
keeps the identity of an analysis packet stable across retries and turns the
typed packet into the two manifests persisted beside the reader output.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .schema import AnalysisPacket

_OPERATIONAL_FINGERPRINT_KEYS = frozenset(
    {
        "generated_at",
        "generated_at_utc",
        "collected_at",
        "latency_minutes",
    }
)


def json_safe(value: Any) -> Any:
    """Return a value accepted by SQL JSON columns and ``json.dumps``."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump(mode="json", exclude_none=False))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        json_safe(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _without_operational_timestamps(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_operational_timestamps(item)
            for key, item in value.items()
            if str(key) not in _OPERATIONAL_FINGERPRINT_KEYS
        }
    if isinstance(value, list):
        return [_without_operational_timestamps(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_operational_timestamps(item) for item in value)
    return value


def analysis_packet_fingerprint(packet: AnalysisPacket | Mapping[str, Any]) -> str:
    """Identify meteorological content while ignoring packet creation time.

    ``audit.created_at``, ``issue.cutoff_utc`` and ``issue.time`` are operational
    timestamps and may legitimately differ when the same source set is rebuilt
    after a worker retry.  The selected sources and their data times remain in
    the packet, so a changed input or diagnosis still creates a new fingerprint.
    """

    validated = packet if isinstance(packet, AnalysisPacket) else AnalysisPacket.model_validate(packet)
    payload = deepcopy(validated.model_dump(mode="json", exclude_none=False))
    payload.get("audit", {}).pop("created_at", None)
    # packet_id is derived from the source manifest, whose missing-source
    # entries legitimately use the current cutoff as their collection time.
    # Input checksums/data times below are the durable identity instead.
    payload.get("audit", {}).pop("packet_id", None)
    payload.get("issue", {}).pop("cutoff_utc", None)
    payload.get("issue", {}).pop("time", None)
    return sha256_json(_without_operational_timestamps(payload))


def fallback_fingerprint(
    payload: Any,
    *,
    issue_date: str,
    reason: str,
) -> str:
    """Stable identity for a blocked/invalid attempt without a valid packet."""

    return sha256_json(
        {
            "issue_date": issue_date,
            "reason": reason,
            "payload": _without_operational_timestamps(json_safe(payload)),
        }
    )


def source_manifest_payload(packet: AnalysisPacket) -> dict[str, Any]:
    return {
        "schema_version": "weather-summary-sources.v1",
        "sources": [source.model_dump(mode="json", exclude_none=False) for source in packet.source_manifest],
    }


def quality_manifest_payload(packet: AnalysisPacket) -> dict[str, Any]:
    return {
        "schema_version": "weather-summary-quality.v1",
        **packet.quality.model_dump(mode="json", exclude_none=False),
    }


@dataclass(frozen=True, slots=True)
class AssessedPacket:
    packet: AnalysisPacket
    fingerprint: str
    packet_payload: dict[str, Any]
    source_manifest: dict[str, Any]
    quality_manifest: dict[str, Any]


def assess_analysis_packet(packet: AnalysisPacket | Mapping[str, Any]) -> AssessedPacket:
    validated = packet if isinstance(packet, AnalysisPacket) else AnalysisPacket.model_validate(packet)
    return AssessedPacket(
        packet=validated,
        fingerprint=analysis_packet_fingerprint(validated),
        packet_payload=validated.model_dump(mode="json", exclude_none=False),
        source_manifest=source_manifest_payload(validated),
        quality_manifest=quality_manifest_payload(validated),
    )


__all__ = [
    "AssessedPacket",
    "analysis_packet_fingerprint",
    "assess_analysis_packet",
    "canonical_json",
    "fallback_fingerprint",
    "json_safe",
    "quality_manifest_payload",
    "sha256_json",
    "source_manifest_payload",
]
