"""SQLAlchemy 2.0 ORM models for the Person-2-owned tables.

No shared `Base`/session setup existed anywhere in the codebase at the time this was
written (confirmed: nothing in `src/infrastructure/` or `payroll/config.py`), so this
declares its own `Base`. If/when a shared Base is introduced elsewhere, swap the
import here — the table definitions themselves don't need to change.

Postgres is the target (per team's stack choice); JSON columns use the generic
`sqlalchemy.JSON` type so the same models also run against SQLite in tests without a
Postgres server. In production, consider migrating these to `JSONB` via an Alembic
migration for indexing/query performance.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChangeSetORM(Base):
    __tablename__ = "policy_update_changesets"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_changeset_idempotency_key"),
        Index("ix_changeset_company_status", "company_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_formula_version: Mapped[int | None] = mapped_column(nullable=True)
    baseline_workbook_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW")
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_of: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("policy_update_changesets.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_baseline_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    items: Mapped[list["ChangeItemORM"]] = relationship(
        back_populates="changeset", cascade="all, delete-orphan"
    )


class ChangeItemORM(Base):
    __tablename__ = "policy_update_change_items"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_change_item_confidence_range"),
        Index("ix_change_item_changeset", "changeset_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    changeset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy_update_changesets.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    policy_diff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposed_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(String(2048), nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    dependency_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    clarifying_question: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    formula_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    changeset: Mapped[ChangeSetORM] = relationship(back_populates="items")
    operations: Mapped[list["UpdateOperationORM"]] = relationship(
        back_populates="change_item", cascade="all, delete-orphan"
    )


class UpdateOperationORM(Base):
    __tablename__ = "policy_update_update_operations"
    __table_args__ = (Index("ix_update_operation_change_item", "change_item_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    change_item_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy_update_change_items.id"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workbook_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sheet: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cell_or_row_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    precondition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    change_item: Mapped[ChangeItemORM] = relationship(back_populates="operations")


# Agentic case tables share this Base with the existing policy-update tables.  A
# Case can point to a ChangeSet, but does not use ChangeSet's detailed state
# machine: its user-facing lifecycle is intentionally only RECEIVED → REVIEW →
# APPROVED/REJECTED → DONE.
class AgentCaseORM(Base):
    __tablename__ = "agent_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RECEIVED', 'REVIEW', 'APPROVED', 'REJECTED', 'DONE')",
            name="ck_agent_case_status",
        ),
        Index("ix_agent_case_status_updated", "status", "updated_at"),
    )

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    changeset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(128), nullable=False)
    brief: Mapped[str] = mapped_column(String(8000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RECEIVED")
    approved_plan_version: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    plans: Mapped[list["AgentPlanORM"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    runs: Mapped[list["AgentRunORM"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    events: Mapped[list["AgentCaseEventORM"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    messages: Mapped[list["AgentMessageORM"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class AgentMessageORM(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_agent_message_role"),
        Index("ix_agent_message_case_created", "case_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_cases.case_id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String(8000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped[AgentCaseORM] = relationship(back_populates="messages")


class AgentPlanORM(Base):
    __tablename__ = "agent_plans"
    __table_args__ = (
        UniqueConstraint("case_id", "version", name="uq_agent_plan_case_version"),
        Index("ix_agent_plan_case_status", "case_id", "status"),
    )

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_cases.case_id"), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    proposal: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(4000), nullable=True)

    case: Mapped[AgentCaseORM] = relationship(back_populates="plans")
    runs: Mapped[list["AgentRunORM"]] = relationship(back_populates="plan")


class AgentRunORM(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_run_case_status", "case_id", "status"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_cases.case_id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_plans.plan_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    max_steps: Mapped[int] = mapped_column(nullable=False, default=10)
    completed_step_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    stop_reason: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    case: Mapped[AgentCaseORM] = relationship(back_populates="runs")
    plan: Mapped[AgentPlanORM] = relationship(back_populates="runs")


class AgentCaseEventORM(Base):
    __tablename__ = "agent_case_events"
    __table_args__ = (Index("ix_agent_case_event_case_created", "case_id", "created_at"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_cases.case_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped[AgentCaseORM] = relationship(back_populates="events")


class TokenUsageORM(Base):
    __tablename__ = "agent_token_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0", name="ck_agent_token_nonnegative"),
        Index("ix_agent_token_usage_case_recorded", "case_id", "recorded_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_cases.case_id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False)
    output_tokens: Mapped[int] = mapped_column(nullable=False)
    total_tokens: Mapped[int] = mapped_column(nullable=False)
    tool_calls: Mapped[int] = mapped_column(nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
