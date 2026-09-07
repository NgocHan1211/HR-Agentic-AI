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
