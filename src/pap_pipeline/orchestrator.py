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
from pap_pipeline.stages.triage import TriageConfig, TriageDecision, triage
from pap_pipeline.verify import dual_extract, verify_decision


@dataclass(frozen=True)
class PipelineConfig:
    """Aggregate config for the wired stages.

    Grouped so the orchestrator has a single config seam and callers don't
    hand-thread individual configs through the stack. `mdm` defaults to a
    null source (see ADR-0004) so every case flows through the history
    lookup path even before a real connector is wired.

    ``extract_verifier`` (Phase C) is optional — when set, the pipeline
    runs a second Extract call in parallel with the primary using this
    config's vision_model, then diffs the two intake dicts. Any
    disagreement lands as a ``VerifierFinding`` on the Decision and
    escalates the case to HumanLoop. None disables the check.
    """

    extract: ExtractClientConfig
    retrieve: RetrieveClientConfig
    reason: ReasonClientConfig
    mdm: MdmSourceConfig = field(default_factory=lambda: MdmSourceConfig(provider="null"))
    extract_verifier: ExtractClientConfig | None = None
    declared_outcomes: list[str] | None = None
    # Triage runs after extract, before retrieve. Additive today — the
    # returned TriageDecision is attached to the pipeline result but does
    # not yet reroute execution. A follow-up will honor
    # ``TriageDecision.route`` for short-circuit paths (refill, appeal,
    # missing-info reply). ``None`` = triage disabled (unchanged behavior).
    triage: TriageConfig | None = None


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
    decision = _attach_history_citations(decision, history, reranked_rules)
    return _attach_verifier_findings(
        decision,
        intake=intake,
        extras=merged_extras,
        retrieved_rules=reranked_rules,
        declared_outcomes=config.declared_outcomes,
    )


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


def _attach_verifier_findings(
    decision: Decision,
    *,
    intake: Intake,
    extras: dict[str, Any],
    retrieved_rules: list[Rule],
    declared_outcomes: list[str] | None,
) -> Decision:
    """Run the deterministic Phase-D checks and stitch findings onto Decision.

    Combines the reason stage's ``matched`` bit + retrieved rule set + the
    intake into a set of consistency checks. Extraction-disagreement
    findings from :func:`dual_extract` are stitched in at the top of
    :func:`run_with_intake` and preserved through this call — we append
    to the list, never overwrite.
    """
    intake_dict = dict(intake.model_dump())
    # Merge non-Intake fields the reason stage saw (extras) so
    # missing_field checks can see program-specific slots too.
    for k, v in (extras or {}).items():
        intake_dict.setdefault(k, v)
    retrieved_ids = [r.id for r in retrieved_rules]
    new_findings = verify_decision(
        decision,
        intake_dict=intake_dict,
        retrieved_rule_ids=retrieved_ids,
        retrieved_rules=retrieved_rules,
        declared_outcomes=declared_outcomes,
    )
    if not new_findings:
        return decision
    return decision.model_copy(
        update={
            "verifier_findings": [*decision.verifier_findings, *new_findings],
        }
    )


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
    decision: Decision, history: HistorySlice, rules: list[Rule]
) -> Decision:
    """Append history-typed citations for slots that satisfied fired rules.

    Predicate (ADR-0004): for each rule citation whose rule declares
    ``references_slots`` overlapping a *truthy* slot in the slice, append
    one :class:`Citation` per (slot × source event) pair with
    ``type='history'`` naming the event, slot, and snapshot hash.

    "Truthy" gates on the slot's value: a False/None/0 slot means "no
    history event supports this fact" — citing an absent event would be
    misleading. This mirrors how the reason stage treats the flattened
    slot value in ``extras``.

    Deduped by (rule, slot, event) so a rule referencing a slot that fired
    on two events produces two distinct citations (one per event), and
    two rules citing the same fired slot don't produce duplicates for the
    same event.
    """
    if not history.slots or not decision.citations:
        return decision

    fired_slots = {s.name: s for s in history.slots if _slot_is_truthy(s.value)}
    if not fired_slots:
        return decision

    refs_by_rule: dict[str, set[str]] = {
        r.id: set(r.references_slots or []) for r in rules
    }

    cited_rules = [
        c for c in decision.citations
        if c.type == "rule" and c.rule_id in refs_by_rule and c.matched
    ]
    if not cited_rules:
        return decision

    seen: set[tuple[str, str]] = set()
    additions: list[Citation] = []
    for cit in cited_rules:
        for slot_name in refs_by_rule[cit.rule_id]:
            slot = fired_slots.get(slot_name)
            if slot is None:
                continue
            for event_id in slot.from_events or []:
                key = (slot_name, event_id)
                if key in seen:
                    continue
                seen.add(key)
                additions.append(
                    Citation(
                        type="history",
                        event_id=event_id,
                        slot_name=slot_name,
                        snapshot_hash=history.snapshot_hash,
                    )
                )

    if not additions:
        return decision
    return decision.model_copy(
        update={"citations": [*decision.citations, *additions]}
    )


def _slot_is_truthy(value: object) -> bool:
    """A slot value that warrants a citation.

    False / None / 0 / "" all mean "no supporting event"; only positive
    values earn a history citation. Splitting this out (rather than
    inlining `bool(value)`) keeps the "why" comment attached to the
    predicate so a future kind (e.g., ``"last_reason_code"``) doesn't
    have to relitigate it.
    """
    if value is None or value is False:
        return False
    if isinstance(value, int) and value == 0:
        return False
    if isinstance(value, str) and not value:
        return False
    return True


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
    subject: str | None = None,
) -> tuple[dict[str, Any], Intake, Decision]:
    """Same as :func:`run` but also returns the raw extraction dict + Intake.

    `slots` is the caller's per-program extraction schema (from
    `programs.extraction_schema`). The extract stage builds its VLM prompt
    from those slot definitions; the returned dict is then split at this
    boundary into (a) an :class:`Intake` for the retrieve stage's fixed-field
    query builder and (b) a leftover *extras* dict that flows through to
    reasoning so program-specific slots still participate in the verdict.
    The raw dict is returned as-is for persistence + UI display.

    ``subject`` is the incoming email/message subject, forwarded to the
    Triage stage as the strongest classification signal. Callers that
    don't have a subject (JSON/API intake) can leave it None; Triage
    will fall back to the extras / intake-shape signals.
    """
    if config.extract_verifier is not None:
        raw, extract_findings = await dual_extract(
            attachments,
            primary_config=config.extract,
            verifier_config=config.extract_verifier,
            slots=slots,
        )
    else:
        raw = await extract(attachments, config=config.extract, slots=slots)
        extract_findings = []

    intake = _intake_from_extraction(raw)
    extras = _extras_from_extraction(raw)

    decision = await run_triaged_from_intake(
        intake,
        config=config,
        program=program,
        program_family_id=program_family_id,
        tenant_id=tenant_id,
        mdm_source=mdm_source,
        extras=extras,
        rules=rules,
        k=k,
        subject=subject,
    )

    if extract_findings:
        decision = decision.model_copy(
            update={
                "verifier_findings": [*extract_findings, *decision.verifier_findings],
            }
        )
    return raw, intake, decision


async def run_triaged_from_intake(
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
    subject: str | None = None,
) -> Decision:
    """Triage → (Decide or skip-Decide) against an already-parsed Intake.

    Shared middle-slice used by every channel that arrives at the pipeline
    with an Intake already in hand:

    * ``run_with_intake`` — after extract runs on inbound attachments.
    * JSON intake API (``pap_pipeline.api_hook.run_json_intake_sync``) —
      the caller submitted structured JSON, so extract is bypassed but the
      case still must pass through Triage to pick its route / skip-Decide
      flag.

    Behaviour:

    * ``config.triage is None`` — skip Triage entirely, run Decide. Preserves
      pre-Triage behaviour for callers that haven't wired it.
    * ``config.triage`` set — run Triage. If ``decision_needed`` is True,
      run the full Decide path via :func:`run_from_intake`. Otherwise emit
      a stub Decision that names the route so the caller can dispatch.
    * Either way, the final ``Decision.triage`` field carries the
      classifier's evidence trail so the audit log and reviewer UI can see
      what happened without re-running the stage.
    """
    triage_decision = None
    if config.triage is not None:
        triage_decision = await triage(
            intake,
            config=config.triage,
            subject=subject,
            extras=extras,
        )

    if triage_decision is None or triage_decision.decision_needed:
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
    else:
        # Skip Decide (and Deliver — the caller sees decision.triage.route
        # and dispatches accordingly). Stub Decision keeps the audit trail
        # complete; outcome="missing_info" is the closest existing Outcome
        # value and the rationale explains it's a route, not missing info.
        decision = Decision(
            outcome="missing_info",
            citations=[],
            missing_fields=[],
            rationale=(
                f"Triaged as '{triage_decision.case_type}'. Decide skipped; "
                f"case routed to '{triage_decision.route}'."
            ),
        )

    if triage_decision is not None:
        decision = _attach_triage_result(decision, triage_decision)
    return decision


def _attach_triage_result(
    decision: Decision, triage_decision: TriageDecision
) -> Decision:
    """Fold the triage classification onto the ``decision.triage`` field.

    Preserves the classifier's evidence trail (case_type, route, reason,
    signals) on the Decision so the audit log and reviewer UI can see
    what Triage decided without re-running the stage. Downstream systems
    that want to short-circuit off the Decide path read this field.
    """
    from pap_pipeline.schemas.decision import TriageResult

    return decision.model_copy(
        update={
            "triage": TriageResult(
                case_type=triage_decision.case_type,
                route=triage_decision.route,
                decision_needed=triage_decision.decision_needed,
                reason=triage_decision.reason,
                signals=dict(triage_decision.signals),
            )
        }
    )


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
