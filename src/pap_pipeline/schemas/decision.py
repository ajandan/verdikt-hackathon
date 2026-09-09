"""Structured decision emitted by the reason stage.

The Decision is the pipeline's contract with the inbox agent — outcome picks
which reply template to render; citations and missing_fields flow into the
template as payload data so the letter can name specific rules and specific
gaps rather than a generic response.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Outcome = Literal["approve", "deny", "missing_info"]


class Citation(BaseModel):
    """One rule that fired during reasoning.

    `matched` is the reason stage's boolean verdict on that rule against the
    intake; `score` is the reranker's relevance score preserved through from
    retrieval, which lets the letter order citations by confidence.
    """

    rule_id: str
    matched: bool
    score: float | None = None


class Decision(BaseModel):
    """Pipeline decision + evidence trail.

    Field semantics:
      - outcome         approve | deny | missing_info; picks the reply template.
      - citations       every rule the reason stage evaluated, with match verdicts.
                        Approvals list the rules that passed; denials list at
                        least one that failed; missing_info lists the ones that
                        could not be evaluated.
      - missing_fields  intake field names to ask for. Empty unless outcome
                        is missing_info.
      - rationale       one-sentence human-readable reason rendered into the
                        letter body. Not a legal justification — the citations
                        carry that.
    """

    outcome: Outcome
    citations: list[Citation] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    rationale: str = ""
