"""Composes the three pipeline stages into a single Decision.

Called by docgen.inbox.agent when the pipeline backend is enabled for a
tenant. Returns a canonical Decision (approve / deny / missing_info) that
the inbox agent uses to select which reply template to render.

Design shape:

  attachments ── extract ──▶ raw dict (per-program slots)
                              │
                              ├── _intake_from_extraction ──▶ Intake (canonical)
                              │
                              ├── retrieve(intake) ──▶ [RetrievedRule]
                              │
                              └── reason(intake, extras, rules) ──▶ Decision

The retriever's reranker score is stitched back into the Decision citations
after reasoning — the reason stage doesn't see the scores (they're not
inputs to its verdict) but the reply letter uses them to order citations by
confidence.

Extras are the extraction fields that don't overlap the canonical Intake
schema (e.g., program-defined `member_id`). Retrieval keys on canonical
fields only, but reasoning sees them merged into the intake JSON so a rule
that cites a program-specific slot still has a value to evaluate against.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule
from pap_pipeline.stages.extract import ExtractClientConfig, extract
from pap_pipeline.stages.reason import ReasonClientConfig, reason
from pap_pipeline.stages.retrieve import RetrieveClientConfig, retrieve


@dataclass(frozen=True)
class PipelineConfig:
    """Aggregate config for the wired stages.

    Grouped so the orchestrator has a single config seam and callers don't
    hand-thread three individual configs through the stack.
    """

    extract: ExtractClientConfig
    retrieve: RetrieveClientConfig
    reason: ReasonClientConfig


async def run_from_intake(
    intake: Intake,
    *,
    config: PipelineConfig,
    program: str | None = None,
    extras: dict[str, Any] | None = None,
    rules: list[Rule] | None = None,
    k: int = 4,
) -> Decision:
    """Run retrieve → reason against an already-extracted Intake.

    This is the "middle-slice" entry point used by tests, by the demo
    script that feeds `load_canonical_intake()`, and (until Stage 1 lands)
    by the inbox agent when the extract stage is being stubbed.

    `rules` is an explicit corpus that overrides the retrieve stage's YAML
    loader — the inbox agent resolves DB-backed rules via
    `pap_pipeline.rules.load_rules(session=...)` and hands them in so the
    pap_pipeline package stays ignorant of the docgen.store boundary.
    """
    retrieved = await retrieve(
        intake, config=config.retrieve, program=program, rules=rules, k=k
    )
    reranked_rules = [rr.rule for rr in retrieved]
    decision = await reason(
        intake, reranked_rules, config=config.reason, extras=extras
    )
    return _attach_retrieval_scores(decision, retrieved)


def _attach_retrieval_scores(decision: Decision, retrieved) -> Decision:
    """Fold the reranker's per-rule score back onto Decision.citations.

    The reason stage leaves `score=None` because the scores aren't in its
    input; they're a retrieval artifact. The letter renderer orders
    citations by score to show the strongest rules first, so the
    orchestrator stitches them in as the pipeline's final act.
    """
    scores = {rr.rule.id: rr.score for rr in retrieved}
    if not scores:
        return decision
    return decision.model_copy(
        update={
            "citations": [
                (
                    c.model_copy(update={"score": scores[c.rule_id]})
                    if c.rule_id in scores and c.score is None
                    else c
                )
                for c in decision.citations
            ]
        }
    )


async def run(
    attachments: list[bytes],
    *,
    config: PipelineConfig,
    program: str | None = None,
    slots: list[dict[str, Any]] | None = None,
    rules: list[Rule] | None = None,
    k: int = 4,
) -> Decision:
    """Full pipeline — attachments in, Decision out.

    Composes extract → retrieve → reason and stitches the reranker's scores
    onto the Decision's citations. The inbox agent calls this once per
    inbound message when `use_pipeline_backend` is enabled for the tenant.
    """
    _, _, decision = await run_with_intake(
        attachments,
        config=config,
        program=program,
        slots=slots,
        rules=rules,
        k=k,
    )
    return decision


async def run_with_intake(
    attachments: list[bytes],
    *,
    config: PipelineConfig,
    program: str | None = None,
    slots: list[dict[str, Any]] | None = None,
    rules: list[Rule] | None = None,
    k: int = 4,
) -> tuple[dict[str, Any], Intake, Decision]:
    """Same as :func:`run` but also returns the raw extraction dict + Intake.

    `slots` is the caller's per-program extraction schema (from
    `programs.extraction_schema`). The extract stage builds its VLM prompt
    from those slot definitions; the returned dict is then split at this
    boundary into (a) an :class:`Intake` for the retrieve stage's fixed-field
    query builder and (b) a leftover *extras* dict that flows through to
    reasoning so program-specific slots still participate in the verdict.
    The raw dict is returned as-is for persistence + UI display.
    """
    raw = await extract(attachments, config=config.extract, slots=slots)
    intake = _intake_from_extraction(raw)
    extras = _extras_from_extraction(raw)
    decision = await run_from_intake(
        intake,
        config=config,
        program=program,
        extras=extras,
        rules=rules,
        k=k,
    )
    return raw, intake, decision


def _intake_from_extraction(raw: dict[str, Any]) -> Intake:
    """Coerce the dynamic extraction dict into an :class:`Intake`.

    Filters to Intake's known field names, then validates. On a
    ValidationError (e.g., the VLM returned a value that fails a field
    validator), retries field-by-field and drops the offending keys — one
    bad value from the model shouldn't wipe the rest of the intake.
    """
    known = set(Intake.model_fields.keys())
    clean: dict[str, Any] = {k: v for k, v in (raw or {}).items() if k in known}
    try:
        return Intake.model_validate(clean)
    except ValidationError:
        good: dict[str, Any] = {}
        for k, v in clean.items():
            try:
                Intake.model_validate({k: v})
            except ValidationError:
                continue
            good[k] = v
        return Intake.model_validate(good)


def _extras_from_extraction(raw: dict[str, Any]) -> dict[str, Any]:
    """Non-Intake fields from the raw extraction dict.

    These are the program-specific slots (e.g., `member_id`, `pcp_name`)
    that don't map onto :class:`Intake`. Fed to the reason stage so the LLM
    sees the full extracted context, not just the canonical subset.
    """
    known = set(Intake.model_fields.keys())
    return {k: v for k, v in (raw or {}).items() if k not in known}


__all__ = [
    "PipelineConfig",
    "Citation",
    "run",
    "run_from_intake",
    "run_with_intake",
]
