"""Audit events owned by Person 2's domain — mục 12:
"Audit event bắt buộc: ... classification, validation, ..., review decision, ...,
authorization failure."

Reuses Phase 1's `log_audit(event_type, payload, actor, path)` as the sink so every
module in the codebase writes to the same audit trail/format, rather than inventing a
second logging convention. Only the `event_type` vocabulary is defined here.
"""

from __future__ import annotations

from typing import Any

from payroll.audit import log_audit


class ChangeSetAuditEvent:
    CLASSIFICATION = "policy_update.classification"
    VALIDATION = "policy_update.validation"
    CHANGESET_BUILT = "policy_update.changeset_built"
    REVIEW_DECISION = "policy_update.review_decision"
    FORMULA_CANDIDATE_PROPOSED = "policy_update.formula_candidate_proposed"
    FORMULA_REVIEW_DECISION = "policy_update.formula_review_decision"
    AUTHORIZATION_FAILURE = "policy_update.authorization_failure"


def record(event_type: str, payload: dict[str, Any], actor: str, audit_log_path: str) -> dict[str, Any]:
    return log_audit(event_type, payload, actor, audit_log_path)


def record_review_decision(
    *, changeset_id: str, decision: str, reviewer: str, note: str, audit_log_path: str
) -> dict[str, Any]:
    return record(
        ChangeSetAuditEvent.REVIEW_DECISION,
        {"changeset_id": changeset_id, "decision": decision, "note": note},
        actor=reviewer,
        audit_log_path=audit_log_path,
    )


def record_authorization_failure(
    *, changeset_id: str, attempted_action: str, actor: str, reason: str, audit_log_path: str
) -> dict[str, Any]:
    return record(
        ChangeSetAuditEvent.AUTHORIZATION_FAILURE,
        {"changeset_id": changeset_id, "attempted_action": attempted_action, "reason": reason},
        actor=actor,
        audit_log_path=audit_log_path,
    )
