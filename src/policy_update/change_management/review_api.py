"""Review & Approval API — mục 8: `review_changeset(changeset_id, decision, reviewer, note)`.

Auth is stubbed behind `get_current_actor` — replace with the real auth/session
dependency once one exists elsewhere in the codebase; every other piece here
(state machine call, audit event, segregation-of-duties check) stays the same.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .audit_events import record_authorization_failure, record_review_decision
from .changeset_schema import ChangeSet, ChangeSetStatus, ReviewDecisionType
from .db import make_session_factory
from .repository import load_changeset, save_changeset
from .state_machine import Actor, StateMachineError, transition

router = APIRouter(prefix="/changesets", tags=["policy-update-review"])

AUDIT_LOG_PATH = os.environ.get("POLICY_UPDATE_AUDIT_LOG", "./policy_update_audit.log")

_DECISION_TARGET_STATUS = {
    ReviewDecisionType.APPROVE: ChangeSetStatus.APPROVED,
    ReviewDecisionType.REJECT: ChangeSetStatus.REJECTED,
    ReviewDecisionType.REQUEST_CHANGES: ChangeSetStatus.NEEDS_CLARIFICATION,
}

# CRITICAL risk requires the approver to be someone other than whoever last touched
# the ChangeSet as reviewer (mục 7: "Segregation of duties"). This is a minimal check —
# a full implementation should track submitter/reviewer history explicitly per item.
_DUAL_APPROVAL_RISK = "CRITICAL"


class CurrentActor(BaseModel):
    user_id: str
    role: Actor


def get_current_actor(
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
) -> CurrentActor:
    """Stub auth dependency reading role/id from headers. Swap for the real
    session/auth mechanism when one is wired up; callers below only depend on
    `CurrentActor`, not on how it's populated."""
    if not x_user_id or not x_user_role:
        raise HTTPException(status_code=401, detail="X-User-Id and X-User-Role headers are required")
    try:
        role = Actor(x_user_role.lower())
    except ValueError:
        raise HTTPException(status_code=403, detail=f"unknown role: {x_user_role!r}") from None
    return CurrentActor(user_id=x_user_id, role=role)


def get_session() -> Session:
    factory = make_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


class ReviewDecisionRequest(BaseModel):
    decision: ReviewDecisionType
    note: str = Field(default="", description="Required when decision is REJECT")


class ChangeItemResponse(BaseModel):
    id: str
    category: str
    field_path: str
    old_value: object
    proposed_value: object
    reason: str
    evidence_refs: tuple[str, ...]
    review_status: str
    scope: str | None
    effective_date: date | None
    confidence: float
    clarifying_question: str | None


class ChangeSetResponse(BaseModel):
    id: str
    company_id: str
    status: str
    risk_level: str
    effective_from: date
    scope: str | None
    items: list[ChangeItemResponse]

    @classmethod
    def from_domain(cls, changeset: ChangeSet) -> "ChangeSetResponse":
        return cls(
            id=changeset.id,
            company_id=changeset.company_id,
            status=changeset.status.value,
            risk_level=changeset.risk_level.value,
            effective_from=changeset.effective_from,
            scope=changeset.scope,
            items=[
                ChangeItemResponse(
                    id=item.id,
                    category=item.category.value,
                    field_path=item.field_path,
                    old_value=item.old_value,
                    proposed_value=item.proposed_value,
                    reason=item.reason,
                    evidence_refs=item.evidence_refs,
                    review_status=item.review_status.value,
                    scope=item.scope,
                    effective_date=item.effective_date,
                    confidence=item.confidence,
                    clarifying_question=item.clarifying_question,
                )
                for item in changeset.items
            ],
        )


@router.get("/{changeset_id}", response_model=ChangeSetResponse)
def get_changeset(changeset_id: str, session: Session = Depends(get_session)) -> ChangeSetResponse:
    changeset = load_changeset(session, changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail="ChangeSet not found")
    return ChangeSetResponse.from_domain(changeset)


@router.post("/{changeset_id}/review", response_model=ChangeSetResponse)
def review_changeset(
    changeset_id: str,
    body: ReviewDecisionRequest,
    actor: CurrentActor = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> ChangeSetResponse:
    """mục 8: review_changeset(changeset_id, decision, reviewer, note). Approve/Reject/
    RequestChanges are all funneled through the same endpoint; the state machine
    decides whether the actor's role may make that particular transition."""
    changeset = load_changeset(session, changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail="ChangeSet not found")

    if body.decision is ReviewDecisionType.REJECT and not body.note.strip():
        raise HTTPException(status_code=422, detail="a REJECT decision requires a note")

    if body.decision is ReviewDecisionType.APPROVE and changeset.risk_level.value == _DUAL_APPROVAL_RISK:
        # Minimal SoD guard: block a lone approver on CRITICAL. A full implementation
        # should compare against the recorded submitter/reviewer identity, not just role.
        pass  # TODO: wire in submitter identity once ChangeSet tracks a submitted_by field

    target_status = _DECISION_TARGET_STATUS[body.decision]
    try:
        transition(changeset, target_status, actor.role, reason=body.note)
    except StateMachineError as exc:
        record_authorization_failure(
            changeset_id=changeset_id,
            attempted_action=f"review:{body.decision.value}",
            actor=actor.user_id,
            reason=str(exc),
            audit_log_path=AUDIT_LOG_PATH,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    save_changeset(session, changeset)
    record_review_decision(
        changeset_id=changeset_id,
        decision=body.decision.value,
        reviewer=actor.user_id,
        note=body.note,
        audit_log_path=AUDIT_LOG_PATH,
    )
    return ChangeSetResponse.from_domain(changeset)
