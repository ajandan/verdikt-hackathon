"""Eligibility rule as stored in the retrieval knowledge base.

One Rule per eligibility criterion for a given program. Rules are authored as
YAML in `rules/`, loaded at startup, embedded (rule.text is what goes into the
embedding), and stored in pgvector. The `id` and `program` fields are stable
identifiers; the reason stage cites them by id in the Decision so the letter
can name specific rules rather than a generic "your application was reviewed".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RuleKind = Literal["financial", "insurance", "residency", "clinical"]


class Rule(BaseModel):
    """One eligibility rule for one program.

    - `id`      stable identifier cited by the Decision (e.g. "CV-INC-01").
    - `program` namespace — retrieval filters on this so multi-program
                deployments never bleed rules across programs.
    - `kind`    coarse taxonomy the reason stage uses to build a checklist.
    - `text`    the human-readable rule statement; this is what gets embedded.
    - `fields`  the intake field names this rule needs to be evaluable. Used
                by the reason stage to compute `missing_fields` when a rule
                can't be checked because a field is None.
    """

    id: str
    program: str
    kind: RuleKind
    text: str
    fields: list[str] = Field(default_factory=list)
