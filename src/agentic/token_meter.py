"""Provider-neutral token accounting and partner-report aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import uuid4

from .models import AgentPhase


@dataclass(frozen=True)
class TokenUsage:
    case_id: str
    run_id: str | None
    model: str
    phase: AgentPhase
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int = 0
    latency_ms: int | None = None
    estimated_cost_usd: Decimal = Decimal("0")
    id: str = ""
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.case_id or not self.model:
            raise ValueError("case_id and model are required")
        if not isinstance(self.phase, AgentPhase):
            raise ValueError("phase must be an AgentPhase")
        if min(self.input_tokens, self.output_tokens, self.total_tokens, self.tool_calls) < 0:
            raise ValueError("token and tool-call counts cannot be negative")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if self.id == "":
            object.__setattr__(self, "id", f"token-{uuid4().hex[:12]}")
        if self.recorded_at is None:
            object.__setattr__(self, "recorded_at", datetime.now(timezone.utc))


@dataclass(frozen=True)
class TokenSummary:
    case_id: str | None
    model: str | None
    phase: AgentPhase | None
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int
    estimated_cost_usd: Decimal
    average_latency_ms: float | None


class TokenMeter:
    """Collects provider usage once per model call before it is persisted.

    Google returns counts in ``usageMetadata``.  The meter keeps the provider
    response at this boundary so application code never has to infer tokens from
    characters, which would make partner reports unreliable.
    """

    def __init__(self) -> None:
        self.records: list[TokenUsage] = []

    def record_google_usage(
        self, *, case_id: str, run_id: str | None, phase: AgentPhase, provider_usage: dict[str, Any],
        tool_calls: int = 0,
    ) -> TokenUsage:
        usage = TokenUsage(
            case_id=case_id,
            run_id=run_id,
            model=str(provider_usage["model"]),
            phase=phase,
            input_tokens=int(provider_usage.get("input_tokens", 0)),
            output_tokens=int(provider_usage.get("output_tokens", 0)),
            total_tokens=int(provider_usage.get("total_tokens", 0)),
            latency_ms=provider_usage.get("latency_ms"),
            tool_calls=tool_calls,
        )
        self.records.append(usage)
        return usage


def summarize_token_usage(
    records: Iterable[TokenUsage], *, case_id: str | None = None, model: str | None = None,
    phase: AgentPhase | None = None,
) -> TokenSummary:
    """Aggregate measurements after optional filters, without estimating missing data."""
    selected = [
        record for record in records
        if (case_id is None or record.case_id == case_id)
        and (model is None or record.model == model)
        and (phase is None or record.phase is phase)
    ]
    latencies = [record.latency_ms for record in selected if record.latency_ms is not None]
    return TokenSummary(
        case_id=case_id,
        model=model,
        phase=phase,
        calls=len(selected),
        input_tokens=sum(record.input_tokens for record in selected),
        output_tokens=sum(record.output_tokens for record in selected),
        total_tokens=sum(record.total_tokens for record in selected),
        tool_calls=sum(record.tool_calls for record in selected),
        estimated_cost_usd=sum((record.estimated_cost_usd for record in selected), Decimal("0")),
        average_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
    )
