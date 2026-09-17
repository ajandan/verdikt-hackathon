"""Structured decision emitted by the reason stage.

The Decision is the pipeline's contract with the inbox agent — outcome picks
which reply template to render; citations and missing_fields flow into the
template as payload data so the letter can name specific rules and specific
gaps rather than a generic response.

Citations discriminate on `type`:

- ``type="rule"`` — one rule the reason stage evaluated. Carries `rule_id`,
  `matched`, and (after orchestrator stitching) `score`.
- ``type="history"`` — one prior event that satisfied a slot referenced by a
  rule. Carries `event_id`, `slot_name`, and `snapshot_hash`. Added by the
  orchestrator's ``_attach_history_citations`` pass, not by the reason stage.

See ADR-0004 for why the two shapes share one class instead of a
discriminated union.

``verifier_findings`` is populated by the orchestrator *after* Nemotron
returns, not by the reason stage itself. It carries deterministic
consistency-check flags (hallucinated citations, verdict/citation mismatch)
and dual-VLM extraction disagreements. Non-empty findings force the case
into HumanLoop rather than auto-shipping the reply. See
:mod:`pap_pipeline.verify` for the checks that populate it.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Outcome = Literal["approve", "deny", "missing_info"]
CitationType = Literal["rule", "history"]
FindingType = Literal[
    "extract_disagreement",
    "hallucinated_citation",
    "cited_rule_not_retrieved",
    "outcome_missing_info_but_no_gaps",
    "outcome_missing_info_gap_not_null_in_intake",
    "missing_field_not_referenced_by_cited_rule",
    "outcome_approve_no_matched_citation",
    "outcome_deny_no_unmatched_citation",
    "outcome_outside_declared_vocabulary",
]
FindingSeverity = Literal["critical", "warning"]


class VerifierFinding(BaseModel):
    """One deterministic-verifier flag on the Decision.

    Populated post-hoc by :mod:`pap_pipeline.verify`. ``detail`` carries
    type-specific context (rule id, field name, both extraction values, etc.)
    so the reviewer sees exactly what disagreed and why.
    """

    type: FindingType
    severity: FindingSeverity = "critical"
    detail: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    """One item in the decision's evidence trail.

    Two shapes on one class so consumers that iterate
    ``decision.citations`` don't grow a type-switch overnight. The
    per-type field set is enforced by a model validator so a bad shape
    fails fast instead of leaking to downstream systems.
    """

    type: CitationType = "rule"

    # rule-typed fields
    rule_id: str | None = None
    matched: bool | None = None
    score: float | None = None

    # history-typed fields
    event_id: str | None = None
    slot_name: str | None = None
    snapshot_hash: str | None = None

    @model_validator(mode="after")
    def _enforce_per_type_fields(self) -> "Citation":
        if self.type == "rule":
            if self.rule_id is None or self.matched is None:
                raise ValueError(
                    "rule citation requires rule_id and matched"
                )
            if (
                self.event_id is not None
                or self.slot_name is not None
                or self.snapshot_hash is not None
            ):
                raise ValueError(
                    "rule citation must not carry history-typed fields"
                )
        else:  # type == "history"
            if (
                self.event_id is None
                or self.slot_name is None
                or self.snapshot_hash is None
            ):
                raise ValueError(
                    "history citation requires event_id, slot_name, snapshot_hash"
                )
            if (
                self.rule_id is not None
                or self.matched is not None
                or self.score is not None
            ):
                raise ValueError(
                    "history citation must not carry rule-typed fields"
                )
        return self


class Decision(BaseModel):
    """Pipeline decision + evidence trail.

    Field semantics:
      - outcome         approve | deny | missing_info; picks the reply template.
      - citations       every rule the reason stage evaluated, with match verdicts.
                        Approvals list the rules that passed; denials list at
                        least one that failed; missing_info lists the ones that
                        could not be evaluated. May also carry history-typed
                        citations added by the orchestrator after reasoning.
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
    # Populated post-hoc by pap_pipeline.verify — extraction disagreements
    # (dual-VLM cross-check) and deterministic citation-consistency flags.
    # Non-empty findings escalate the case to HumanLoop.
    verifier_findings: list[VerifierFinding] = Field(default_factory=list)
    # Populated by the Triage stage (runs after extract, before retrieve).
    # Optional so pre-Triage callers and stored Decisions from before the
    # Triage rollout still validate. Non-null carries the case_type +
    # downstream route the classifier chose; downstream systems that want
    # to short-circuit off the Decide path read this field.
    triage: "TriageResult | None" = None


class TriageResult(BaseModel):
    """Serialized :class:`pap_pipeline.stages.triage.TriageDecision`.

    Stored on the Decision so the audit log preserves *why* a case was
    routed off the Decide path (or confirmed on it). The stage module
    (``stages.triage``) owns the classifier; this model owns the wire /
    persistence shape so schemas stay in one place.

    Kept as plain strings (not Literals) so the schema doesn't need to
    move in lockstep with the classifier if new case_types are added.
    """

    case_type: str
    route: str
    # Whether the case still needs to run the Decide stage. When False,
    # the orchestrator emits a stub Decision and downstream systems
    # dispatch directly to ``route``. Default True to preserve
    # backwards-compat with stored Decisions from before the flag existed.
    decision_needed: bool = True
    reason: str = ""
    signals: dict[str, Any] = Field(default_factory=dict)


Decision.model_rebuild()
