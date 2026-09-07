"""AI provider adapter interface.

Design-time only. Sees blank templates; never sees runtime payload data.
The deterministic render path has no dependency on this module — enforced by
package boundaries (`core/` doesn't import `providers/`).
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from docgen.core.schema_infer import FieldSpec


class Suggestion(BaseModel):
    placeholder: str
    jinja: str
    field: FieldSpec
    # "ai" for provider-proposed rows; "orphan_backfill" for rows synthesized from
    # preflight-detected variables the AI missed. UI badges the latter so users know
    # to review the auto-guessed type/description.
    source: str = "ai"
    # One-sentence explanation of *why* the model chose this field name/type. Optional
    # so the stub provider and older serialized proposals still validate. When present,
    # the mapping wizard surfaces it as a subtitle beneath the field name so reviewers
    # can see the model's justification without re-running inference.
    reasoning: str | None = None


class MappingProposal(BaseModel):
    suggestions: list[Suggestion]
    notes: str | None = None

    def fields(self) -> list[FieldSpec]:
        seen: set[str] = set()
        out: list[FieldSpec] = []
        for s in self.suggestions:
            if s.field.name in seen:
                continue
            seen.add(s.field.name)
            out.append(s.field)
        return out


class AIProvider(Protocol):
    name: str

    async def propose_mapping(self, template_text: str) -> MappingProposal: ...
