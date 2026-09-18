"""Person 2's call site into Person 1's `RAGAdapter.search()`.

`RAGAdapter.search(query, context, top_k)` is a free-text search, not "give me
evidence for diff X" — this module is the small adapter that turns one PolicyDiff
into a query and an `AccessContext`, so `classifier.py` only ever deals with
`list[RetrievedEvidence]`, never with how it was fetched.

IMPORTANT — open decision, not something Person 2 should decide unilaterally:
what `PermissionLevel` should the *automated* classification pipeline run at? Policy
clauses about salary/allowances plausibly sit behind INTERNAL or CONFIDENTIAL in the
access_filter.py contract. `DEFAULT_PIPELINE_PERMISSION_LEVEL` below is a placeholder
— confirm the real value with whoever owns the permission taxonomy before relying on
this in production; get it wrong and the pipeline either sees too little evidence
(under-permissioned) or evidence that shouldn't be used to auto-classify (over-
permissioned since AccessFilter is company/scope/date-correct but this is a step
beyond individual human clearance).
"""

from __future__ import annotations

from datetime import date

from rag.access_filter import AccessContext, PermissionLevel
from rag.rag_adapter import RAGAdapter, RetrievedEvidence
from .contracts import PolicyDiff

# Placeholder — see module docstring. Do not treat as a final security decision.
DEFAULT_PIPELINE_PERMISSION_LEVEL = PermissionLevel.INTERNAL

DEFAULT_EVIDENCE_TOP_K = 5


def build_pipeline_access_context(
    company_id: str, *, scopes: frozenset[str] = frozenset(), as_of_date: date | None = None
) -> AccessContext:
    """AccessContext for the *automated* classification pipeline (not a specific
    human reviewer) — used when fetching evidence to feed the Classifier."""
    return AccessContext(
        company=company_id,
        scopes=scopes,
        permission_level=DEFAULT_PIPELINE_PERMISSION_LEVEL,
        as_of_date=as_of_date or date.today(),
    )


def fetch_evidence_for_diff(
    diff: PolicyDiff,
    rag_adapter: RAGAdapter,
    *,
    access_context: AccessContext,
    top_k: int = DEFAULT_EVIDENCE_TOP_K,
) -> list[RetrievedEvidence]:
    """One PolicyDiff -> its supporting evidence. Prefers new_text as the query
    (what the policy says *now*) and falls back to old_text for a pure removal."""
    query = diff.new_text or diff.old_text
    if not query:
        raise ValueError(f"PolicyDiff {diff.id} has neither old_text nor new_text to query with")
    response = rag_adapter.search(query, context=access_context, top_k=top_k)
    return response.evidence
