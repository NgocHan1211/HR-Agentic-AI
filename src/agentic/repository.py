"""Persistence for agentic cases, plans, runs, audit events, and token usage."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from policy_update.change_management.db_models import (
    AgentCaseEventORM,
    AgentCaseORM,
    AgentMessageORM,
    AgentPlanORM,
    AgentRunORM,
    TokenUsageORM,
)

from .models import AgentCase, AgentPhase, AgentRunStatus, CaseStatus, PlanProposal, PlanStatus, assert_case_transition
from .token_meter import TokenUsage, summarize_token_usage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(proposal: PlanProposal) -> str:
    raw = json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_case(session: Session, case: AgentCase, *, actor_id: str) -> AgentCase:
    if session.get(AgentCaseORM, case.case_id) is not None:
        raise ValueError(f"case {case.case_id} already exists")
    session.add(
        AgentCaseORM(
            case_id=case.case_id,
            changeset_id=case.changeset_id,
            requester_id=case.requester_id,
            brief=case.brief,
            status=case.status.value,
            approved_plan_version=case.approved_plan_version,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )
    )
    record_event(session, case.case_id, "case.created", actor_id, {"changeset_id": case.changeset_id})
    session.flush()
    return case


def load_case(session: Session, case_id: str) -> AgentCase | None:
    row = session.get(AgentCaseORM, case_id)
    if row is None:
        return None
    return AgentCase(
        case_id=row.case_id,
        changeset_id=row.changeset_id,
        requester_id=row.requester_id,
        brief=row.brief,
        status=CaseStatus(row.status),
        approved_plan_version=row.approved_plan_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def save_case(session: Session, case: AgentCase) -> None:
    row = session.get(AgentCaseORM, case.case_id)
    if row is None:
        raise ValueError(f"case {case.case_id} does not exist")
    row.status = case.status.value
    row.approved_plan_version = case.approved_plan_version
    row.updated_at = case.updated_at
    session.flush()


def submit_plan(session: Session, *, case_id: str, proposal: PlanProposal, actor_id: str) -> AgentPlanORM:
    case = load_case(session, case_id)
    if case is None:
        raise LookupError("case not found")
    if case.status is not CaseStatus.RECEIVED:
        raise ValueError("a plan can only be submitted for a RECEIVED case")
    version = (session.scalar(select(AgentPlanORM.version).where(AgentPlanORM.case_id == case_id).order_by(AgentPlanORM.version.desc()).limit(1)) or 0) + 1
    row = AgentPlanORM(
        plan_id=f"plan-{uuid4().hex[:12]}",
        case_id=case_id,
        version=version,
        status=PlanStatus.SUBMITTED.value,
        proposal=proposal.model_dump(mode="json"),
        content_hash=_content_hash(proposal),
    )
    session.add(row)
    case.transition_to(CaseStatus.REVIEW)
    save_case(session, case)
    record_event(session, case_id, "plan.submitted", actor_id, {"plan_id": row.plan_id, "version": version, "content_hash": row.content_hash})
    session.flush()
    return row


def load_plan(session: Session, plan_id: str) -> AgentPlanORM | None:
    return session.get(AgentPlanORM, plan_id)


def decide_plan(session: Session, *, case_id: str, approved: bool, actor_id: str, note: str = "") -> AgentPlanORM:
    case = load_case(session, case_id)
    if case is None:
        raise LookupError("case not found")
    if case.status is not CaseStatus.REVIEW:
        raise ValueError("a plan decision requires a case in REVIEW")
    plan = session.scalar(
        select(AgentPlanORM).where(AgentPlanORM.case_id == case_id, AgentPlanORM.status == PlanStatus.SUBMITTED.value)
        .order_by(AgentPlanORM.version.desc()).limit(1)
    )
    if plan is None:
        raise ValueError("there is no submitted plan to decide")
    target = CaseStatus.APPROVED if approved else CaseStatus.REJECTED
    assert_case_transition(case.status, target)
    case.status = target
    case.updated_at = _utcnow()
    plan.status = (PlanStatus.APPROVED if approved else PlanStatus.REJECTED).value
    plan.decided_at = _utcnow()
    plan.decided_by = actor_id
    plan.decision_note = note
    if approved:
        case.approved_plan_version = plan.version
    save_case(session, case)
    record_event(session, case_id, "plan.approved" if approved else "plan.rejected", actor_id, {"plan_id": plan.plan_id, "note": note})
    session.flush()
    return plan


def create_run(session: Session, *, case_id: str, actor_id: str, max_steps: int = 10) -> AgentRunORM:
    case = load_case(session, case_id)
    if case is None:
        raise LookupError("case not found")
    if case.status is not CaseStatus.APPROVED or case.approved_plan_version is None:
        raise ValueError("execution requires an APPROVED case and plan")
    plan = session.scalar(
        select(AgentPlanORM).where(
            AgentPlanORM.case_id == case_id, AgentPlanORM.version == case.approved_plan_version,
            AgentPlanORM.status == PlanStatus.APPROVED.value,
        )
    )
    if plan is None:
        raise ValueError("approved plan cannot be found")
    row = AgentRunORM(
        run_id=f"run-{uuid4().hex[:12]}", case_id=case_id, plan_id=plan.plan_id,
        status=AgentRunStatus.PENDING.value, max_steps=max_steps,
    )
    session.add(row)
    record_event(session, case_id, "run.created", actor_id, {"run_id": row.run_id, "plan_id": plan.plan_id})
    session.flush()
    return row


def finish_run(session: Session, *, run_id: str, status: AgentRunStatus, completed_step_ids: list[str],
               stop_reason: str | None, actor_id: str) -> AgentRunORM:
    row = session.get(AgentRunORM, run_id)
    if row is None:
        raise LookupError("run not found")
    if status not in {AgentRunStatus.COMPLETED, AgentRunStatus.NEEDS_REVIEW, AgentRunStatus.FAILED}:
        raise ValueError("finish_run only accepts terminal run statuses")
    if status is AgentRunStatus.COMPLETED:
        plan = session.get(AgentPlanORM, row.plan_id)
        if plan is None:
            raise ValueError("run plan cannot be found")
        required_step_ids = {str(step["id"]) for step in (plan.proposal or {}).get("steps", [])}
        if set(completed_step_ids) != required_step_ids or len(completed_step_ids) != len(required_step_ids):
            raise ValueError("a COMPLETED run must include every approved plan step exactly once")
    row.status = status.value
    row.completed_step_ids = completed_step_ids
    row.stop_reason = stop_reason
    row.ended_at = _utcnow()
    if row.started_at is None:
        row.started_at = row.ended_at
    if status is AgentRunStatus.COMPLETED:
        case = load_case(session, row.case_id)
        if case is None:
            raise LookupError("case not found")
        case.transition_to(CaseStatus.DONE)
        save_case(session, case)
    record_event(session, row.case_id, "run.finished", actor_id, {"run_id": run_id, "status": status.value, "stop_reason": stop_reason})
    session.flush()
    return row


def record_event(session: Session, case_id: str, event_type: str, actor_id: str, payload: dict) -> AgentCaseEventORM:
    row = AgentCaseEventORM(
        event_id=f"event-{uuid4().hex[:12]}", case_id=case_id, event_type=event_type,
        actor_id=actor_id, payload=payload,
    )
    session.add(row)
    return row


def append_message(session: Session, *, case_id: str, role: str, content: str) -> AgentMessageORM:
    if role not in {"user", "assistant", "system"}:
        raise ValueError("unsupported message role")
    row = AgentMessageORM(
        message_id=f"message-{uuid4().hex[:12]}", case_id=case_id, role=role, content=content,
    )
    session.add(row)
    session.flush()
    return row


def list_messages(session: Session, case_id: str) -> list[dict[str, str]]:
    rows = session.scalars(
        select(AgentMessageORM).where(AgentMessageORM.case_id == case_id).order_by(AgentMessageORM.created_at)
    )
    return [{"role": row.role, "content": row.content} for row in rows]


def record_token_usage(session: Session, usage: TokenUsage) -> TokenUsageORM:
    row = TokenUsageORM(
        id=usage.id, case_id=usage.case_id, run_id=usage.run_id, model=usage.model,
        phase=usage.phase.value, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens, tool_calls=usage.tool_calls, latency_ms=usage.latency_ms,
        estimated_cost_usd=float(usage.estimated_cost_usd), recorded_at=usage.recorded_at,
    )
    session.add(row)
    session.flush()
    return row


def token_report(session: Session, *, case_id: str | None = None) -> dict:
    statement = select(TokenUsageORM)
    if case_id:
        statement = statement.where(TokenUsageORM.case_id == case_id)
    rows = list(session.scalars(statement))
    records = [
        TokenUsage(
            id=row.id, case_id=row.case_id, run_id=row.run_id, model=row.model,
            phase=AgentPhase(row.phase), input_tokens=row.input_tokens, output_tokens=row.output_tokens,
            total_tokens=row.total_tokens, tool_calls=row.tool_calls, latency_ms=row.latency_ms,
            estimated_cost_usd=Decimal(str(row.estimated_cost_usd)), recorded_at=row.recorded_at,
        )
        for row in rows
    ]
    summary = summarize_token_usage(records, case_id=case_id)
    return {
        "case_id": summary.case_id,
        "calls": summary.calls,
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
        "total_tokens": summary.total_tokens,
        "tool_calls": summary.tool_calls,
        "estimated_cost_usd": str(summary.estimated_cost_usd),
        "average_latency_ms": summary.average_latency_ms,
    }
