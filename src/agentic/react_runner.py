"""Plan-scoped ReAct runner.

The runner deliberately never decides whether a case was approved.  Its caller must
only construct it after the case state machine has reached APPROVED.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .loop_guard import LoopDetected, LoopGuard
from .models import AgentDecision, PlanProposal


class StructuredAgentClient(Protocol):
    def complete_structured(self, *, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]: ...


class Tool(Protocol):
    def __call__(self, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ReActResult:
    status: str  # COMPLETED | NEEDS_REVIEW
    completed_step_ids: tuple[str, ...]
    stop_reason: str | None
    user_message: str | None
    turns: int


class ReActRunner:
    def __init__(self, *, client: StructuredAgentClient, tools: Mapping[str, Tool], loop_guard: LoopGuard | None = None,
                 on_usage: Callable[[dict[str, Any], int], None] | None = None) -> None:
        self._client = client
        self._tools = dict(tools)
        self._guard = loop_guard or LoopGuard()
        self._on_usage = on_usage

    def run(self, *, plan: PlanProposal, initial_context: dict[str, Any]) -> ReActResult:
        steps = {step.id: step for step in plan.steps}
        completed: set[str] = set()
        history: list[dict[str, Any]] = []
        turns = 0

        while True:
            prompt = json.dumps(
                {
                    "plan": plan.model_dump(mode="json"),
                    "completed_step_ids": sorted(completed),
                    "initial_context": initial_context,
                    "history": history[-8:],
                    "instruction": (
                        "Choose exactly one next action. Only select a plan_step_id from the plan. "
                        "Only call a tool listed in that step's allowed_tools. Escalate instead of guessing."
                    ),
                },
                ensure_ascii=False,
                default=str,
            )
            payload, usage = self._client.complete_structured(
                system="You are an execution agent. Follow the approved plan and return JSON only.",
                user=prompt,
                schema=AgentDecision.model_json_schema(),
            )
            turns += 1
            if self._on_usage:
                self._on_usage(usage, 0)
            try:
                decision = AgentDecision.model_validate(payload)
            except Exception as exc:
                return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), f"invalid structured decision: {exc}", None, turns)

            if decision.plan_step_id not in steps:
                return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), "agent selected an unknown plan step", None, turns)
            if decision.decision == "escalate":
                return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), decision.reason, decision.user_message, turns)
            if decision.decision == "complete":
                completed.add(decision.plan_step_id)
                if set(steps) != completed:
                    return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), "agent attempted completion before every plan step completed", None, turns)
                return ReActResult("COMPLETED", tuple(sorted(completed)), None, decision.user_message, turns)

            assert decision.tool_name is not None and decision.tool_input is not None  # validated above
            step = steps[decision.plan_step_id]
            if decision.tool_name not in step.allowed_tools or decision.tool_name not in self._tools:
                return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), "tool is not allowed by the approved plan", None, turns)
            try:
                self._guard.before_action(
                    plan_step_id=decision.plan_step_id, tool_name=decision.tool_name, tool_input=decision.tool_input
                )
                observation = self._tools[decision.tool_name](decision.tool_input)
                self._guard.after_observation(observation, made_progress=decision.mark_step_complete)
            except LoopDetected as exc:
                return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), str(exc), None, turns)
            except Exception as exc:
                return ReActResult("NEEDS_REVIEW", tuple(sorted(completed)), f"tool execution failed: {exc}", None, turns)

            if decision.mark_step_complete:
                completed.add(decision.plan_step_id)
            history.append(
                {
                    "decision": decision.model_dump(mode="json"),
                    "observation": observation,
                }
            )
            if self._on_usage:
                self._on_usage({}, 1)
