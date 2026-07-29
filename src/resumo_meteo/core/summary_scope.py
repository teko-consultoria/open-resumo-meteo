"""Versioned basin selection for the meteorological summary.

The canonical geometry registry is deliberately broader than a particular
editorial product.  This module defines the auditable subset for which the
weather summary has a compatible historical precipitation reference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .basins import CANONICAL_BASIN_IDS

DEFAULT_SUMMARY_BASIN_SCOPE_PATH = (
    Path(__file__).resolve().parent / "config" / "weather_summary_basins.json"
)


@dataclass(frozen=True, slots=True)
class SummaryBasinScope:
    schema_version: int
    scope_id: str
    scope_version: str
    description: str
    climatology_provider: str
    basin_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version do escopo do resumo deve ser 1")
        if not self.scope_id or not self.scope_version:
            raise ValueError("escopo do resumo sem id ou versao")
        if not self.basin_ids or len(set(self.basin_ids)) != len(self.basin_ids):
            raise ValueError("escopo do resumo deve conter bacias unicas")
        unknown = sorted(set(self.basin_ids) - set(CANONICAL_BASIN_IDS))
        if unknown:
            raise ValueError(f"escopo do resumo possui IDs nao canonicos: {unknown}")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scope_id": self.scope_id,
            "scope_version": self.scope_version,
            "description": self.description,
            "climatology_provider": self.climatology_provider,
            "basin_ids": list(self.basin_ids),
        }


def load_summary_basin_scope(path: str | Path = DEFAULT_SUMMARY_BASIN_SCOPE_PATH) -> SummaryBasinScope:
    resolved = Path(path).expanduser().resolve()
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"nao foi possivel ler escopo do resumo: {resolved}") from exc
    if not isinstance(raw, dict):
        raise ValueError("escopo do resumo deve ser um objeto JSON")
    try:
        basin_ids = tuple(str(value) for value in raw["basin_ids"])
        return SummaryBasinScope(
            schema_version=int(raw["schema_version"]),
            scope_id=str(raw["scope_id"]),
            scope_version=str(raw["scope_version"]),
            description=str(raw.get("description") or ""),
            climatology_provider=str(raw.get("climatology_provider") or ""),
            basin_ids=basin_ids,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("escopo do resumo invalido") from exc


DEFAULT_SUMMARY_BASIN_SCOPE = load_summary_basin_scope()
WEATHER_SUMMARY_BASIN_IDS = DEFAULT_SUMMARY_BASIN_SCOPE.basin_ids


__all__ = [
    "DEFAULT_SUMMARY_BASIN_SCOPE",
    "DEFAULT_SUMMARY_BASIN_SCOPE_PATH",
    "SummaryBasinScope",
    "WEATHER_SUMMARY_BASIN_IDS",
    "load_summary_basin_scope",
]
