"""ChangeSet state machine — mục 3.1 of the plan.

Every transition is either system-driven (deterministic, code decides) or
actor-driven (a human decision arrives via `review_changeset`/`apply_changeset`/
`rollback_update_run`). This module only enforces *which* transitions are legal
and *which* role may trigger them; it does not decide business logic like risk
or evidence completeness (see `changeset_builder.py` / `validation.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .changeset_schema import ChangeSet, ChangeSetStatus


class Actor(str, Enum):
    """Role matrix — mục 3.1 ("Ai được chuyển trạng thái") + mục 11 ("Role nào được
    approve/apply/rollback?"). Kept intentionally small; extend once payroll/HR
    confirms the full role matrix (mục 13 Go/No-Go item)."""

    SYSTEM = "system"
    ANALYST = "analyst"  # resolves NEEDS_CLARIFICATION
    REVIEWER = "reviewer"  # IN_REVIEW decisions: reject / request changes
    APPROVER = "approver"  # APPROVED / ROLLED_BACK — must differ from submitter (mục 7 SoD)


class StateMachineError(ValueError):
    """Raised on an illegal transition, wrong actor, or a locked-content violation."""


@dataclass(frozen=True)
class _Transition:
    to: ChangeSetStatus
    allowed_actors: frozenset[Actor]


# mục 3.1, one row per transition. NEEDS_CLARIFICATION can go back to DRAFT (system
# rebuilds after the analyst supplies missing info) or straight to REJECTED/blocked
# if the ambiguity can't be resolved.
_TRANSITIONS: dict[ChangeSetStatus, tuple[_Transition, ...]] = {
    ChangeSetStatus.DRAFT: (
        _Transition(ChangeSetStatus.READY_FOR_REVIEW, frozenset({Actor.SYSTEM})),
        _Transition(ChangeSetStatus.NEEDS_CLARIFICATION, frozenset({Actor.SYSTEM})),
    ),
    ChangeSetStatus.NEEDS_CLARIFICATION: (
        _Transition(ChangeSetStatus.DRAFT, frozenset({Actor.ANALYST, Actor.SYSTEM})),
        _Transition(ChangeSetStatus.REJECTED, frozenset({Actor.ANALYST, Actor.REVIEWER})),
    ),
    ChangeSetStatus.READY_FOR_REVIEW: (
        _Transition(ChangeSetStatus.IN_REVIEW, frozenset({Actor.REVIEWER, Actor.SYSTEM})),
    ),
    ChangeSetStatus.IN_REVIEW: (
        _Transition(ChangeSetStatus.APPROVED, frozenset({Actor.APPROVER})),
        _Transition(ChangeSetStatus.REJECTED, frozenset({Actor.REVIEWER, Actor.APPROVER})),
        _Transition(ChangeSetStatus.NEEDS_CLARIFICATION, frozenset({Actor.REVIEWER, Actor.APPROVER})),
    ),
    ChangeSetStatus.APPROVED: (
        _Transition(ChangeSetStatus.APPLYING, frozenset({Actor.SYSTEM})),
    ),
    ChangeSetStatus.APPLYING: (
        _Transition(ChangeSetStatus.COMPLETED, frozenset({Actor.SYSTEM})),
        _Transition(ChangeSetStatus.FAILED, frozenset({Actor.SYSTEM})),
    ),
    ChangeSetStatus.FAILED: (
        _Transition(ChangeSetStatus.ROLLED_BACK, frozenset({Actor.APPROVER, Actor.SYSTEM})),
    ),
    ChangeSetStatus.COMPLETED: (
        _Transition(ChangeSetStatus.ROLLED_BACK, frozenset({Actor.APPROVER, Actor.SYSTEM})),
    ),
    # REJECTED and ROLLED_BACK are terminal for this ChangeSet id; fix-ups go through
    # a new revision (mục 3.1: "tạo revision mới và duyệt lại").
    ChangeSetStatus.REJECTED: (),
    ChangeSetStatus.ROLLED_BACK: (),
}


def can_transition(current: ChangeSetStatus, target: ChangeSetStatus, actor: Actor) -> bool:
    for transition in _TRANSITIONS.get(current, ()):
        if transition.to is target and actor in transition.allowed_actors:
            return True
    return False


def transition(changeset: ChangeSet, target: ChangeSetStatus, actor: Actor, *, reason: str = "") -> ChangeSet:
    """Apply a status change in place, enforcing mục 3.1's rules. Returns the same
    ChangeSet for convenient chaining. Does not persist — caller commits to DB."""
    current = changeset.status
    if not can_transition(current, target, actor):
        raise StateMachineError(
            f"actor {actor.value!r} cannot move ChangeSet {changeset.id} from "
            f"{current.value!r} to {target.value!r}"
        )
    if target in (ChangeSetStatus.REJECTED,) and not reason.strip():
        raise StateMachineError("a REJECTED transition requires a reason")
    if current is ChangeSetStatus.DRAFT and target is ChangeSetStatus.NEEDS_CLARIFICATION:
        if not any(item.clarifying_question for item in changeset.items):
            raise StateMachineError(
                "cannot move to NEEDS_CLARIFICATION without at least one item carrying "
                "a clarifying_question"
            )
    changeset.status = target
    if target is ChangeSetStatus.APPROVED:
        from datetime import datetime, timezone

        changeset.approved_at = datetime.now(timezone.utc)
    return changeset


def assert_baseline_unchanged(changeset: ChangeSet, *, current_baseline_snapshot: dict) -> None:
    """Mục 3.1: 'Approval tự hết hạn nếu policy baseline, workbook baseline hoặc scope
    đã thay đổi.' Caller (apply_changeset) should call this right before APPLYING and
    move to FAILED / back to NEEDS_CLARIFICATION on mismatch rather than applying stale
    approval."""
    if changeset.status is not ChangeSetStatus.APPROVED:
        raise StateMachineError("baseline check only applies to an APPROVED ChangeSet")
    if changeset.approved_baseline_snapshot is None:
        return  # nothing recorded at approval time; caller decides how strict to be
    if changeset.approved_baseline_snapshot != current_baseline_snapshot:
        raise StateMachineError(
            f"ChangeSet {changeset.id} approval has expired: baseline changed since APPROVED"
        )
