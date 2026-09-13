"""Eligibility rule as stored in the retrieval knowledge base.

One Rule per eligibility criterion for a given program. Rules are authored as
YAML in `rules/`, loaded at startup, embedded (the concatenated title +
criterion is what goes into the embedding), and stored in pgvector. The `id`
and `program` fields are stable identifiers; the reason stage cites them by id
in the Decision so the letter can name specific rules rather than a generic
"your application was reviewed".

The rule body is split into three short, capped fields so the same block is
readable by a coordinator and precise enough for an LLM to reason over without
hallucinating around lengthy prose:

- `title`     one-line human label (the citation heading)
- `criterion` the checkable claim in a stable phrasing (uses backticked
              intake field names)
- `exception` optional short escape clause; blank when N/A
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RuleKind = Literal["financial", "insurance", "residency", "clinical", "history"]


class Rule(BaseModel):
    """One eligibility rule for one program.

    - `id`        stable identifier cited by the Decision (e.g. "CV-INC-01").
    - `program`   namespace — retrieval filters on this so multi-program
                  deployments never bleed rules across programs.
    - `kind`      coarse taxonomy the reason stage uses to build a checklist.
    - `title`     one-line label rendered in the picker, library table, and
                  decision-letter citation. Cap 80 chars.
    - `criterion` the checkable claim in canonical phrasing — this is the
                  smallest surface area for the LLM to reason over. Cap 200
                  chars.
    - `exception` optional short escape clause. Cap 200 chars.
    - `fields`    the intake field names this rule needs to be evaluable. Used
                  by the reason stage to compute `missing_fields` when a rule
                  can't be checked because a field is None.
    """

    id: str
    program: str
    kind: RuleKind
    title: str = Field(max_length=200)
    criterion: str = Field(max_length=400)
    exception: str = Field(default="", max_length=400)
    fields: list[str] = Field(default_factory=list)

    def as_prompt_block(self) -> str:
        """Format the rule as the exact block the LLM sees.

        Kept in sync with the human-facing UI so there is one source of
        truth — anything the reviewer reads on the rule detail card is what
        the retriever embeds and what the reason stage receives.
        """
        lines = [f"[{self.id}] {self.title}", f"  Criterion: {self.criterion}"]
        if self.exception:
            lines.append(f"  Exception: {self.exception}")
        else:
            lines.append("  Exception: n/a")
        return "\n".join(lines)
