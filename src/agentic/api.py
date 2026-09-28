"""FastAPI router for the web UI and a Cloudflare Workflow adapter.

There is no direct model-execution endpoint here by design.  The API gates state
and persists audit data; a worker invokes the plan/ReAct services and reports the
validated result through these endpoints.
"""

from __future__ import annotations

import os
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from policy_update.change_management.db import make_session_factory
from payroll.formula.formula_extractor import GemmaAPICompletionClient

from .models import AgentCase, AgentPhase, AgentRunStatus, CaseStatus, PlanProposal
from .planning import ConversationPlanner, StructuredPlanningClient
from .repository import (
    append_message,
    create_case,
    create_run,
    decide_plan,
    finish_run,
    load_case,
    load_plan,
    list_messages,
    record_token_usage,
    submit_plan,
    token_report,
)
from .token_meter import TokenMeter, TokenUsage


router = APIRouter(prefix="/cases", tags=["agentic-cases"])


def get_session() -> Session:
    factory = make_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def current_actor(x_user_id: Annotated[str | None, Header()] = None) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id header is required")
    return x_user_id


def get_agent_client() -> StructuredPlanningClient:
    """Use the existing Gemma client, never expose its API key to the browser."""
    api_key = os.environ.get("GEMMA_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMMA_API_KEY is not configured")
    return GemmaAPICompletionClient(
        api_key=api_key,
        model=os.environ.get("GEMMA_MODEL"),
        api_url=os.environ.get("GEMMA_API_URL"),
        timeout_seconds=float(os.environ.get("GEMMA_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.environ.get("GEMMA_MAX_RETRIES", "3")),
        retry_base_delay_seconds=float(os.environ.get("GEMMA_RETRY_BASE_DELAY_SECONDS", "2")),
    )


class CreateCaseRequest(BaseModel):
    requester_id: str = Field(min_length=1, max_length=128)
    brief: str = Field(min_length=1, max_length=8000)
    changeset_id: str | None = Field(default=None, max_length=64)


class PlanDecisionRequest(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=4000)


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class CreateRunRequest(BaseModel):
    max_steps: int = Field(default=10, ge=1, le=50)


class FinishRunRequest(BaseModel):
    status: AgentRunStatus
    completed_step_ids: list[str] = Field(default_factory=list, max_length=30)
    stop_reason: str | None = Field(default=None, max_length=4000)


class TokenUsageRequest(BaseModel):
    run_id: str | None = None
    model: str = Field(min_length=1, max_length=128)
    phase: AgentPhase
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    tool_calls: int = Field(default=0, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


def _case_response(case: AgentCase) -> dict:
    return {
        "case_id": case.case_id,
        "changeset_id": case.changeset_id,
        "requester_id": case.requester_id,
        "brief": case.brief,
        "status": case.status.value,
        "approved_plan_version": case.approved_plan_version,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }


@router.post("", status_code=201)
def create_agent_case(
    body: CreateCaseRequest, actor_id: str = Depends(current_actor), session: Session = Depends(get_session)
) -> dict:
    # A case may begin before the policy-update ChangeSet is materialized.  Keep a
    # stable placeholder ID so the three required correlation fields always exist.
    changeset_id = body.changeset_id or f"changeset-pending-{uuid4().hex[:12]}"
    case = AgentCase(changeset_id=changeset_id, requester_id=body.requester_id, brief=body.brief)
    try:
        create_case(session, case, actor_id=actor_id)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _case_response(case)


@router.get("/{case_id}")
def get_agent_case(case_id: str, session: Session = Depends(get_session)) -> dict:
    case = load_case(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return _case_response(case)


def _record_provider_usage(
    session: Session, *, case_id: str, phase: AgentPhase, usage: dict, tool_calls: int = 0
) -> None:
    # If a provider omits usageMetadata, record zeros explicitly rather than
    # estimating from characters. This makes the partner report honest.
    meter = TokenMeter()
    record_token_usage(
        session,
        meter.record_google_usage(case_id=case_id, run_id=None, phase=phase, provider_usage=usage, tool_calls=tool_calls),
    )


@router.post("/{case_id}/messages")
def chat_and_maybe_submit_plan(
    case_id: str,
    body: ChatMessageRequest,
    actor_id: str = Depends(current_actor),
    client: StructuredPlanningClient = Depends(get_agent_client),
    session: Session = Depends(get_session),
) -> dict:
    """Conversation phase.  A plan is generated and submitted only when Gemma says
    the brief is ready; its resulting state is REVIEW, never APPROVED."""
    case = load_case(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    if case.status is not CaseStatus.RECEIVED:
        raise HTTPException(status_code=409, detail="messages are only accepted while the case is RECEIVED")
    append_message(session, case_id=case_id, role="user", content=body.content)
    conversation = list_messages(session, case_id)
    planner = ConversationPlanner(client)
    try:
        decision, decision_usage = planner.decide_next_message(conversation)
        _record_provider_usage(session, case_id=case_id, phase=AgentPhase.CONVERSATION, usage=decision_usage)
        append_message(session, case_id=case_id, role="assistant", content=decision.message)
        response: dict = {"decision": decision.model_dump(mode="json"), "plan": None}
        if decision.decision == "present_plan":
            plan, plan_usage = planner.create_plan(list_messages(session, case_id))
            _record_provider_usage(session, case_id=case_id, phase=AgentPhase.PLAN, usage=plan_usage)
            row = submit_plan(session, case_id=case_id, proposal=plan, actor_id=actor_id)
            response["plan"] = {"plan_id": row.plan_id, "version": row.version, "proposal": row.proposal}
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=f"agent planning failed: {str(exc)[:500]}") from exc
    updated_case = load_case(session, case_id)
    assert updated_case is not None
    response["case"] = _case_response(updated_case)
    return response


@router.post("/{case_id}/plans", status_code=201)
def submit_case_plan(
    case_id: str, proposal: PlanProposal, actor_id: str = Depends(current_actor), session: Session = Depends(get_session)
) -> dict:
    try:
        plan = submit_plan(session, case_id=case_id, proposal=proposal, actor_id=actor_id)
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"plan_id": plan.plan_id, "case_id": case_id, "version": plan.version, "status": plan.status, "proposal": plan.proposal}


@router.post("/{case_id}/plan-decision")
def decide_case_plan(
    case_id: str, body: PlanDecisionRequest, actor_id: str = Depends(current_actor), session: Session = Depends(get_session)
) -> dict:
    if not body.approved and not body.note.strip():
        raise HTTPException(status_code=422, detail="a rejected plan requires a note")
    try:
        plan = decide_plan(session, case_id=case_id, approved=body.approved, actor_id=actor_id, note=body.note)
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    case = load_case(session, case_id)
    assert case is not None
    return {"case": _case_response(case), "plan_id": plan.plan_id, "plan_status": plan.status}


@router.post("/{case_id}/runs", status_code=201)
def start_agent_run(
    case_id: str, body: CreateRunRequest, actor_id: str = Depends(current_actor), session: Session = Depends(get_session)
) -> dict:
    try:
        run = create_run(session, case_id=case_id, actor_id=actor_id, max_steps=body.max_steps)
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run.run_id, "case_id": run.case_id, "plan_id": run.plan_id, "status": run.status, "max_steps": run.max_steps}


@router.post("/runs/{run_id}/finish")
def finish_agent_run(
    run_id: str, body: FinishRunRequest, actor_id: str = Depends(current_actor), session: Session = Depends(get_session)
) -> dict:
    try:
        run = finish_run(
            session, run_id=run_id, status=body.status, completed_step_ids=body.completed_step_ids,
            stop_reason=body.stop_reason, actor_id=actor_id,
        )
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run.run_id, "status": run.status, "stop_reason": run.stop_reason}


@router.post("/{case_id}/token-usage", status_code=201)
def add_token_usage(
    case_id: str, body: TokenUsageRequest, _: str = Depends(current_actor), session: Session = Depends(get_session)
) -> dict:
    if body.total_tokens != body.input_tokens + body.output_tokens:
        raise HTTPException(status_code=422, detail="total_tokens must equal input_tokens + output_tokens")
    if load_case(session, case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    usage = TokenUsage(
        case_id=case_id, run_id=body.run_id, model=body.model, phase=body.phase,
        input_tokens=body.input_tokens, output_tokens=body.output_tokens, total_tokens=body.total_tokens,
        tool_calls=body.tool_calls, latency_ms=body.latency_ms,
    )
    record_token_usage(session, usage)
    session.commit()
    return {"id": usage.id}


@router.get("/{case_id}/token-report")
def get_case_token_report(case_id: str, session: Session = Depends(get_session)) -> dict:
    if load_case(session, case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    return token_report(session, case_id=case_id)
