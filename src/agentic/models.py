"""Domain contracts for the human-in-the-loop agent workflow.

The case state is intentionally small.  Operational outcomes such as a loop
guard stop belong to ``AgentRunStatus`` so that they never bypass an HR approval
or create undocumented case-state transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class CaseStatus(str, Enum):
    RECEIVED = "RECEIVED"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DONE = "DONE"


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AgentRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class AgentPhase(str, Enum):
    CONVERSATION = "conversation"
    PLAN = "plan"
    REACT = "react"
    EXECUTE = "execute"
    REVIEW = "review"


class CaseTransitionError(ValueError):
    pass


_CASE_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.RECEIVED: frozenset({CaseStatus.REVIEW}),
    CaseStatus.REVIEW: frozenset({CaseStatus.APPROVED, CaseStatus.REJECTED}),
    CaseStatus.APPROVED: frozenset({CaseStatus.DONE}),
    CaseStatus.REJECTED: frozenset(),
    CaseStatus.DONE: frozenset(),
}


def assert_case_transition(current: CaseStatus, target: CaseStatus) -> None:
    if target not in _CASE_TRANSITIONS[current]:
        raise CaseTransitionError(f"cannot move a case from {current.value} to {target.value}")


class PlanStep(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=4000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)
    expected_result: str = Field(min_length=1, max_length=2000)
    risk: str | None = Field(default=None, max_length=1000)


class PlanProposal(BaseModel):
    """The LLM contract shown to HR before anything can execute."""

    goal: str = Field(min_length=1, max_length=4000)
    summary_for_hr: str = Field(min_length=1, max_length=4000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)
    steps: list[PlanStep] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _steps_have_unique_ids(self) -> "PlanProposal":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        return self


class ConversationDecision(BaseModel):
    decision: Literal["ask_clarification", "present_plan"]
    message: str = Field(min_length=1, max_length=4000)
    missing_information: list[str] = Field(default_factory=list, max_length=20)


class AgentDecision(BaseModel):
    """One ReAct turn.  It is validated before a tool can ever be called."""

    decision: Literal["tool_call", "complete", "escalate"]
    plan_step_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    tool_name: str | None = Field(default=None, max_length=100)
    tool_input: dict[str, Any] | None = None
    mark_step_complete: bool = False
    user_message: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _tool_call_contract(self) -> "AgentDecision":
        if self.decision == "tool_call" and (not self.tool_name or self.tool_input is None):
            raise ValueError("tool_call requires tool_name and tool_input")
        if self.decision != "tool_call" and (self.tool_name is not None or self.tool_input is not None):
            raise ValueError("only tool_call may include tool_name or tool_input")
        if self.decision == "complete" and not self.mark_step_complete:
            raise ValueError("complete must mark its plan step complete")
        return self


@dataclass
class AgentCase:
    changeset_id: str
    requester_id: str
    brief: str
    case_id: str = field(default_factory=lambda: f"case-{uuid4().hex[:12]}")
    status: CaseStatus = CaseStatus.RECEIVED
    approved_plan_version: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def transition_to(self, target: CaseStatus) -> None:
        assert_case_transition(self.status, target)
        self.status = target
        self.updated_at = datetime.now(timezone.utc)
