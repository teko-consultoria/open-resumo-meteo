"""Read contracts shared by persistence and the analysis packet builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtifactProduct:
    """Minimal artifact facade containing only fields read by the packet builder."""

    id: str
    source_slug: str
    product_slug: str
    processed_at: datetime | None
    processed_data: dict[str, Any]
    payload: dict[str, Any]
    data_produto: str

@dataclass(frozen=True, slots=True)
class SelectionSource:
    source_id: str
    category: str
    status: str
    required: bool
    source_name: str
    init_time: str = ""
    collected_at: str = ""
    valid_start: str = ""
    valid_end: str = ""
    latency_minutes: int | None = None
    product_data_id: str | None = None
    details: dict[str, Any] | None = None
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "category": self.category,
            "status": self.status,
            "required": self.required,
            "source_name": self.source_name,
            "init_time": self.init_time,
            "collected_at": self.collected_at,
            "valid_start": self.valid_start,
            "valid_end": self.valid_end,
            "latency_minutes": self.latency_minutes,
            "product_data_id": self.product_data_id,
            "details": self.details or {},
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class ModelInputRecord:
    model_key: str
    family: str
    product_data: ArtifactProduct
    reference_date: date
    run_datetime_utc: datetime
    max_lead_h: int
    member_count: int | None
    grid_files: tuple[dict[str, Any], ...]
    status: str
    issues: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ObservedIMERGInputRecord:
    product_data: ArtifactProduct
    observation_date: date
    grid_files: tuple[dict[str, Any], ...]
    status: str
    issues: tuple[str, ...]
    data_start_utc: datetime | None = None
    data_end_utc: datetime | None = None
    temporal_resolution: str = "daily"
    period_complete: bool = True
    coverage_pct: float | None = None
    basin_aggregates: dict[str, Any] | None = None
