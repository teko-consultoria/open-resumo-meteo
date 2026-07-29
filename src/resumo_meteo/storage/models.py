"""SQLAlchemy models for standalone meteorological ingestion and publication.

The database stores metadata and audit data only. Large meteorological payloads
remain in the artifact store and are addressed by safe, relative paths.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates
from sqlalchemy.types import DateTime, TypeDecorator


def utc_now() -> datetime:
    return datetime.now(UTC)


def uuid_string() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """Portable UTC timestamp that also round-trips correctly through SQLite."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


SOURCE_RUN_STATUSES = (
    "pending",
    "downloading",
    "downloaded",
    "processing",
    "processed",
    "partial",
    "failed",
    "skipped",
)
ACTIVE_SOURCE_RUN_STATUSES = ("pending", "downloading", "downloaded", "processing")

ARTIFACT_STATUSES = ("available", "missing", "deleted")
RETENTION_CLASSES = ("transient", "normalized", "reference", "publication", "audit")
SUMMARY_RUN_STATUSES = (
    "pending",
    "building_packet",
    "blocked",
    "generating",
    "completed",
    "failed",
)


class SourceRun(Base):
    """One externally available data run or observed-data snapshot."""

    __tablename__ = "source_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_slug: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    data_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cycle: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    reference_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    run_datetime_utc: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
    valid_start_utc: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_end_utc: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    status_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_lead_h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temporal_resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    coverage_pct: Mapped[float | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    normalized_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    quality_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_cleanup_after: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
    raw_cleaned_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="source_run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({','.join(repr(value) for value in SOURCE_RUN_STATUSES)})",
            name="ck_source_runs_status",
        ),
        CheckConstraint(
            "data_kind IN ('forecast','observed','reference')", name="ck_source_runs_kind"
        ),
        CheckConstraint("attempt_count >= 0", name="ck_source_runs_attempts"),
        CheckConstraint(
            "coverage_pct IS NULL OR (coverage_pct >= 0 AND coverage_pct <= 100)",
            name="ck_source_runs_coverage",
        ),
        Index("ix_source_runs_selection", "source_id", "cycle", "reference_date", "processed_at"),
    )

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        if value not in SOURCE_RUN_STATUSES:
            raise ValueError(f"invalid source run status: {value}")
        return value


# Only one worker may own an active download/normalization for a logical
# source product. Terminal attempts remain in the audit trail and do not block
# a later retry. COALESCE makes observation cycle=NULL participate in the key.
ACTIVE_SOURCE_IDENTITY_INDEX = Index(
    "uq_source_runs_active_identity",
    SourceRun.source_id,
    SourceRun.source_slug,
    SourceRun.product_slug,
    SourceRun.reference_date,
    func.coalesce(SourceRun.cycle, ""),
    unique=True,
    sqlite_where=SourceRun.status.in_(ACTIVE_SOURCE_RUN_STATUSES),
    postgresql_where=SourceRun.status.in_(ACTIVE_SOURCE_RUN_STATUSES),
)


class Artifact(Base):
    """Metadata for one file in the artifact store."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    source_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/octet-stream"
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="available", index=True)
    retention_class: Mapped[str] = mapped_column(
        String(24), nullable=False, default="normalized", index=True
    )
    valid_start_utc: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_end_utc: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    forecast_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    member_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    variable: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    delete_after: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    source_run: Mapped[SourceRun | None] = relationship(back_populates="artifacts")

    __table_args__ = (
        CheckConstraint(
            f"status IN ({','.join(repr(value) for value in ARTIFACT_STATUSES)})",
            name="ck_artifacts_status",
        ),
        CheckConstraint(
            f"retention_class IN ({','.join(repr(value) for value in RETENTION_CLASSES)})",
            name="ck_artifacts_retention",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_size"),
        CheckConstraint(
            "forecast_hour IS NULL OR forecast_hour >= 0", name="ck_artifacts_forecast_hour"
        ),
        Index("ix_artifacts_cleanup", "retention_class", "status", "delete_after"),
        UniqueConstraint("source_run_id", "kind", "storage_path", name="uq_artifact_run_kind_path"),
    )

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        if value not in ARTIFACT_STATUSES:
            raise ValueError(f"invalid artifact status: {value}")
        return value

    @validates("retention_class")
    def validate_retention(self, _key: str, value: str) -> str:
        if value not in RETENTION_CLASSES:
            raise ValueError(f"invalid retention class: {value}")
        return value


class SummaryRun(Base):
    """Auditable execution of the daily analysis and editorial pipeline."""

    __tablename__ = "summary_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    issue_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    cutoff_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    llm_provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default="openai-compatible"
    )
    llm_model: Mapped[str] = mapped_column(String(128), nullable=False)
    llm_response_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    analysis_packet: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    quality_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    output_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    audit_manifest_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    generation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({','.join(repr(value) for value in SUMMARY_RUN_STATUSES)})",
            name="ck_summary_runs_status",
        ),
        CheckConstraint("generation_attempts >= 0", name="ck_summary_runs_attempts"),
        UniqueConstraint(
            "issue_date",
            "input_fingerprint",
            "schema_version",
            "prompt_version",
            "llm_provider",
            "llm_model",
            name="uq_summary_run_generation",
        ),
        Index("ix_summary_runs_issue_status", "issue_date", "status"),
    )

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        if value not in SUMMARY_RUN_STATUSES:
            raise ValueError(f"invalid summary run status: {value}")
        return value


class AuditEvent(Base):
    """Append-only status and operational audit event."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('source_run','artifact','summary_run','system')",
            name="ck_audit_events_entity_type",
        ),
        Index("ix_audit_events_entity", "entity_type", "entity_id", "created_at"),
    )


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
