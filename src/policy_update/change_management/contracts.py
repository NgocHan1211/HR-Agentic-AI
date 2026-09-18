"""Contracts Person 2 receives from Person 1.

`PolicyDiff` is the Diff Engine's output — still owned/defined here since no
concrete Diff Engine module exists yet (mục 9 mock contract).

`RetrievedEvidence` is NOT redefined here anymore. Person 1's `rag_adapter.py`
publishes its own `RetrievedEvidence`/`SearchResponse`/`RAGAdapter` as the single
public entry point for evidence (see module docstring there: "Người 2 không cần
và không nên import trực tiếp Indexer/Chunk/RetrievedChunk"). Re-exported here so
the rest of this package has one place to import it from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from rag.rag_adapter import RetrievedEvidence, SearchResponse  # re-exported, see module docstring above

__all__ = ["DiffChangeType", "PolicyDiff", "RetrievedEvidence", "SearchResponse"]


class DiffChangeType(str, Enum):
    """Layer-1/2 diff Person 1 produces: structural + textual, before any business
    classification. Not to be confused with `ChangeCategory` (mục 4), which is what
    Person 2's Classifier assigns on top of this."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class PolicyDiff:
    """One aligned unit of comparison between two policy versions (clause, sentence,
    or table row/cell). Fields per plan mục 5.2."""

    id: str
    old_policy_id: str
    new_policy_id: str
    change_type: DiffChangeType
    old_text: str | None
    new_text: str | None
    old_evidence_ref: str | None  # section_path/page_ref into the old PolicyVersion
    new_evidence_ref: str | None  # section_path/page_ref into the new PolicyVersion
    confidence: float
    section_path: str | None = None  # e.g. "3.2.MEAL_ALLOWANCE" — heading -> clause path
    table_context: dict[str, Any] | None = None  # row/col key when the diff is a table cell

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.old_text is None and self.new_text is None:
            raise ValueError("a PolicyDiff must carry at least old_text or new_text")
