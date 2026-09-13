"""Composes the four pipeline stages into a single Decision.

Called by docgen.inbox.agent when the pipeline backend is enabled for a
tenant. Returns a canonical Decision (approve / deny / missing_info) that
the inbox agent uses to select which reply template to render.

Design shape:

  attachments ── extract ──▶ raw dict (per-program slots)
                              │
                              ├── _intake_from_extraction ──▶ Intake (canonical)
                              │
                              ├── retrieve(intake)  ─┐
                              │                     ├── asyncio.gather ──▶ (rules, history)
                              ├── mdm.lookup(...) ──┘
                              │
                              └── reason(intake, extras+slots, rules) ──▶ Decision

The retriever's reranker score is stitched back onto rule-typed citations
after reasoning — the reason stage doesn't see the scores (they're not
inputs to its verdict) but the reply letter uses them to order citations by
confidence. History citations are stitched by a second pass; see
:func:`_attach_history_citations` and ADR-0004.

Extras are the extraction fields that don't overlap the canonical Intake
schema (e.g., program-defined `member_id`). Retrieval keys on canonical
fields only, but reasoning sees them merged into the intake JSON so a rule
that cites a program-specific slot still has a value to evaluate against.
History slot values (from the MDM slice) are flattened onto extras as
top-level keys (`slot.name -> slot.value`) so a rule with
`fields: [prior_enrollment_active]` evaluates against the slot the same way
it evaluates against an intake field. The nested `history_slots` list
remains too as an audit view (carries `from_events` linkage).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.history import HistorySlice
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule
from pap_pipeline.stages.extract import ExtractClientConfig, extract
from pap_pipeline.stages.mdm_lookup import (
    MdmSource,
    MdmSourceConfig,
    build_mdm_source,
)
from pap_pipeline.stages.reason import ReasonClientConfig, reason
from pap_pipeline.stages.retrieve import RetrieveClientConfig, retrieve


@dataclass(frozen=True)
class PipelineConfig:
    """Aggregate config for the wired stages.

    Grouped so the orchestrator has a single config seam and callers don't
    hand-thread individual configs through the stack. `mdm` defaults to a
    null source (see ADR-0004) so every case flows through the history
    lookup path even before a real connector is wired.
    """

    extract: ExtractClientConfig
    retrieve: RetrieveClientConfig
    reason: ReasonClientConfig
    mdm: MdmSourceConfig = field(default_factory=lambda: MdmSourceConfig(provider="null"))


async def run_from_intake(
    intake: Intake,
    *,
    config: PipelineConfig,
    program: str | None = None,
    program_family_id: str | None = None,
    tenant_id: str = "",
    mdm_source: MdmSource | None = None,
    extras: dict[str, Any] | None = None,
    rules: list[Rule] | None = None,
    k: int = 4,
) -> Decision:
    """Run retrieve + mdm.lookup → reason against an already-extracted Intake.

    This is the "middle-slice" entry point used by tests, by the demo
    script that feeds `load_canonical_intake()`, and (until Stage 1 lands)
    by the inbox agent when the extract stage is being stubbed.

    `rules` is an explicit corpus that overrides the retrieve stage's YAML
    loader — the inbox agent resolves DB-backed rules via
    `pap_pipeline.rules.load_rules(session=...)` and hands them in so the
    pap_pipeline package stays ignorant of the docgen.store boundary.

    `mdm_source` is the DI seam for tests; production callers rely on
    :func:`build_mdm_source` picking an adapter from ``config.mdm.provider``
    (null / simulated / http). `program_family_id` falls back to `program`
    when not supplied — the family concept is a follow-up (ADR-0004), and
    using the program slug is a safe interim identifier.
    """
    source = mdm_source or build_mdm_source(config.mdm)
    family_id = program_family_id or program or ""

    retrieved, history = await asyncio.gather(
        retrieve(intake, config=config.retrieve, program=program, rules=rules, k=k),
        source.lookup(
            intake=intake,
            program_family_id=family_id,
            tenant_id=tenant_id,
            config=config.mdm,
        ),
    )

    reranked_rules = [rr.rule for rr in retrieved]
    merged_extras = _merge_history_slots(extras, history)
    decision = await reason(
        intake, reranked_rules, config=config.reason, extras=merged_extras
    )
    decision = _attach_retrieval_scores(decision, retrieved)
    return _attach_history_citations(decision, history)


def _merge_history_slots(
    extras: dict[str, Any] | None, history: HistorySlice
) -> dict[str, Any]:
    """Fold derived slot values from the history slice into the extras dict.

    Two forms are emitted:

    - **Flat** — each `slot.name -> slot.value` at the top level, so a rule
      with `fields: [prior_enrollment_active]` treats it like any intake
      field. This is the primary path the reason stage evaluates against.
    - **Nested** — the full `history_slots` list stays too, preserving each
      slot's `from_events` audit trail for downstream (letter/verdict) use.

    Slot keys do *not* override intake keys — the reason stage's own merge
    order lets Intake win on collision, which is intentional: an applicant's
    submitted field beats an MDM-derived slot of the same name. Rename slots
    if the collision would be ambiguous.

    Raw events (`history.events`) are *not* merged — the reason stage's own
    boundary check forbids them
    (see :func:`pap_pipeline.stages.reason._forbid_raw_history_events`).
    """
    merged = dict(extras or {})
    if history.slots:
        merged["history_slots"] = [s.model_dump() for s in history.slots]
        for slot in history.slots:
            merged.setdefault(slot.name, slot.value)
    return merged


def _attach_retrieval_scores(decision: Decision, retrieved) -> Decision:
    """Fold the reranker's per-rule score back onto rule-typed citations.

    The reason stage leaves `score=None` because the scores aren't in its
    input; they're a retrieval artifact. The letter renderer orders
    citations by score to show the strongest rules first, so the
    orchestrator stitches them in as the pipeline's final act. History
    citations (added after this pass) don't carry scores.
    """
    scores = {rr.rule.id: rr.score for rr in retrieved}
    if not scores:
        return decision
    return decision.model_copy(
        update={
            "citations": [
                (
                    c.model_copy(update={"score": scores[c.rule_id]})
                    if c.type == "rule"
                    and c.rule_id in scores
                    and c.score is None
                    else c
                )
                for c in decision.citations
            ]
        }
    )


def _attach_history_citations(
    decision: Decision, history: HistorySlice
) -> Decision:
    """Append history-typed citations for slots that satisfied fired rules.

    Predicate (ADR-0004): a rule citation whose rule declares a
    `references_slots` set that overlaps the fired slots in the slice. For
    each match, append one :class:`Citation` with ``type='history'`` naming
    the event, slot, and snapshot hash.

    Wiring is in place today; the closing predicate — a `references_slots`
    field on :class:`Rule` — is a follow-up. Until then this pass returns
    the decision unchanged even when slots are non-empty, so history
    citations never leak into the ledger before the rules pack has
    declared its dependency on them.
    """
    if not history.slots or not decision.citations:
        return decision
    # Follow-up (ADR-0004): once Rule gains `references_slots`, filter
    # decision.citations by rule type + slot overlap here and append the
    # matching history citations via Citation(type="history", ...).
    return decision


async def run(
    attachments: list[bytes],
    *,
    config: PipelineConfig,
    program: str | None = None,
    program_family_id: str | None = None,
    tenant_id: str = "",
    mdm_source: MdmSource | None = None,
    slots: list[dict[str, Any]] | None = None,
    rules: list[Rule] | None = None,
    k: int = 4,
) -> Decision:
    """Full pipeline — attachments in, Decision out.

    Composes extract → (retrieve ∥ mdm.lookup) → reason and stitches the
    reranker's scores + history citations onto the Decision. The inbox
    agent calls this once per inbound message when `use_pipeline_backend`
    is enabled for the tenant.
    """
    _, _, decision = await run_with_intake(
        attachments,
        config=config,
        program=program,
        program_family_id=program_family_id,
        tenant_id=tenant_id,
        mdm_source=mdm_source,
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
    program_family_id: str | None = None,
    tenant_id: str = "",
    mdm_source: MdmSource | None = None,
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
        program_family_id=program_family_id,
        tenant_id=tenant_id,
        mdm_source=mdm_source,
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
    "Citation",
    "PipelineConfig",
    "run",
    "run_from_intake",
    "run_with_intake",
]
