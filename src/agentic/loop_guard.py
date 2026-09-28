"""Deterministic cycle detection for ReAct runs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


class LoopDetected(RuntimeError):
    """Raised before an unsafe/no-progress next action can run."""


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class LoopGuard:
    """Tracks the minimum signals needed to stop an unproductive ReAct loop.

    A repeated action is checked *before* execution.  Repeated observations and
    no-progress turns are checked after execution.  The default limits map to the
    agreed design: two identical consecutive actions, three identical observations,
    three turns without completing a plan step, and ten total turns.
    """

    max_steps: int = 10
    max_same_action: int = 2
    max_same_observation: int = 3
    max_no_progress: int = 3
    _steps: int = 0
    _last_action_hash: str | None = None
    _same_action_count: int = 0
    _observation_counts: Counter[str] = field(default_factory=Counter)
    _no_progress_count: int = 0

    def before_action(self, *, plan_step_id: str, tool_name: str, tool_input: dict[str, Any]) -> str:
        if self._steps >= self.max_steps:
            raise LoopDetected(f"maximum ReAct steps reached ({self.max_steps})")
        action_hash = _stable_hash({"plan_step_id": plan_step_id, "tool_name": tool_name, "tool_input": tool_input})
        consecutive = self._same_action_count + 1 if action_hash == self._last_action_hash else 1
        if consecutive >= self.max_same_action:
            raise LoopDetected("identical tool action repeated consecutively")
        self._last_action_hash = action_hash
        self._same_action_count = consecutive
        self._steps += 1
        return action_hash

    def after_observation(self, observation: Any, *, made_progress: bool) -> str:
        observation_hash = _stable_hash(observation)
        self._observation_counts[observation_hash] += 1
        if self._observation_counts[observation_hash] >= self.max_same_observation:
            raise LoopDetected("identical tool observation repeated")
        self._no_progress_count = 0 if made_progress else self._no_progress_count + 1
        if self._no_progress_count >= self.max_no_progress:
            raise LoopDetected("no plan-step progress across consecutive ReAct turns")
        return observation_hash
