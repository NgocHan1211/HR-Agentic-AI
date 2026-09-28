"""Structured Conversation → Plan phase for the Gemma agent."""

from __future__ import annotations

import json
from typing import Any, Protocol

from .models import ConversationDecision, PlanProposal


class StructuredPlanningClient(Protocol):
    def complete_structured(self, *, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]: ...


class ConversationPlanner:
    def __init__(self, client: StructuredPlanningClient) -> None:
        self._client = client

    def decide_next_message(self, conversation: list[dict[str, str]]) -> tuple[ConversationDecision, dict[str, Any]]:
        payload, usage = self._client.complete_structured(
            system=(
                "You are an HR change-request assistant. Determine whether the HR brief has enough "
                "information for a safe plan. Ask concise clarification questions when scope, target, "
                "or expected outcome is unknown. Never claim an action was performed."
            ),
            user=json.dumps({"conversation": conversation}, ensure_ascii=False),
            schema=ConversationDecision.model_json_schema(),
        )
        return ConversationDecision.model_validate(payload), usage

    def create_plan(self, conversation: list[dict[str, str]]) -> tuple[PlanProposal, dict[str, Any]]:
        payload, usage = self._client.complete_structured(
            system=(
                "You create an execution plan for HR approval. Return only the requested JSON schema. "
                "Use small, observable steps. Each step must state its allowed tools; omit a tool rather "
                "than inventing one. Include uncertainties in assumptions or risks. Do not execute anything."
            ),
            user=json.dumps({"conversation": conversation}, ensure_ascii=False),
            schema=PlanProposal.model_json_schema(),
        )
        return PlanProposal.model_validate(payload), usage
