"""ORM models.

Three groups of tables:

* Telemetry the agent reads: ``services``, ``spans``, ``logs``, ``deploys``.
  ``spans`` follows the OpenTelemetry shape used by LLM Observatory
  (W3C trace/span ids, ``kind``, ``status`` of UNSET/OK/ERROR, attributes JSON).
* The incident being investigated: ``incidents`` and ``incident_labels``
  (ground truth for synthetic scenarios, kept in its own table so no tool
  can ever read the answer).
* The agent's own audit trail: ``investigations`` (one row per run) and
  ``investigation_steps`` (one row per node event or tool call).

See docs/schema.md for why each index exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    Postgres keeps the offset natively. SQLite stores plain strings and would
    hand back naive datetimes, which then fail to compare with aware ones.
    This normalises to UTC on the way in and re-attaches UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to a UTC column")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# JSONB on Postgres (indexable, binary), plain JSON text on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    team: Mapped[str | None] = mapped_column(String(120))
    tier: Mapped[str | None] = mapped_column(String(20))  # e.g. tier-1 = customer facing
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Span(Base):
    __tablename__ = "spans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # W3C trace context: 32-hex trace id, 16-hex span id.
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    # "POST /checkout" for server spans, "SELECT orders" / "chat gemini-..." for clients.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="INTERNAL")
    start_ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="UNSET")
    status_message: Mapped[str | None] = mapped_column(String(500))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("trace_id", "span_id", name="uq_spans_trace_span"),
        Index("ix_spans_service_start", "service_id", "start_ts"),
        Index("ix_spans_service_name_start", "service_id", "name", "start_ts"),
        Index(
            "ix_spans_errors_service_start",
            "service_id",
            "start_ts",
            sqlite_where=text("status = 'ERROR'"),
            postgresql_where=text("status = 'ERROR'"),
        ),
    )


class LogRecord(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_logs_service_ts", "service_id", "ts"),
        Index("ix_logs_trace", "trace_id"),
    )


class Deploy(Base):
    __tablename__ = "deploys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_version: Mapped[str | None] = mapped_column(String(40))
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    deployed_by: Mapped[str | None] = mapped_column(String(120))
    change_summary: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (Index("ix_deploys_service_deployed", "service_id", "deployed_at"),)


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    alert_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable: a raw alert may not name a service; triage then works it out.
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    endpoint: Mapped[str | None] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="sev2")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    # When the alert fired. Tools treat this as "now", so replaying an old
    # incident sees exactly the data that existed when it was paged.
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    service: Mapped[Service | None] = relationship()
    label: Mapped[IncidentLabel | None] = relationship(back_populates="incident")

    __table_args__ = (
        Index("ix_incidents_service_detected", "service_id", "detected_at"),
        Index("ix_incidents_status", "status"),
    )


class IncidentLabel(Base):
    """Ground truth for synthetic scenarios. Read by the evaluator only."""

    __tablename__ = "incident_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    scenario: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    root_cause_category: Mapped[str] = mapped_column(String(40), nullable=False)
    root_cause_component: Mapped[str] = mapped_column(String(120), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="label")


# --------------------------------------------------------------------------
# Agent audit trail
# --------------------------------------------------------------------------


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    # queued -> running -> completed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    llm_provider: Mapped[str] = mapped_column(String(40), nullable=False)
    llm_model: Mapped[str] = mapped_column(String(120), nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # tool calls made
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    root_cause_category: Mapped[str | None] = mapped_column(String(40))
    root_cause_component: Mapped[str | None] = mapped_column(String(120))
    confidence: Mapped[str | None] = mapped_column(String(10))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    report_markdown: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    incident: Mapped[Incident] = relationship()
    step_rows: Mapped[list[InvestigationStep]] = relationship(
        back_populates="investigation",
        order_by="InvestigationStep.seq",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_investigations_incident_created", "incident_id", "created_at"),
        Index("ix_investigations_status", "status"),
    )


class InvestigationStep(Base):
    __tablename__ = "investigation_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3 ... within a run
    node: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(80))
    evidence_id: Mapped[str | None] = mapped_column(String(10))  # "E3", cited by claims
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    investigation: Mapped[Investigation] = relationship(back_populates="step_rows")

    __table_args__ = (
        # Doubles as the index for "steps after seq N" polling and SSE.
        UniqueConstraint("investigation_id", "seq", name="uq_steps_investigation_seq"),
    )
