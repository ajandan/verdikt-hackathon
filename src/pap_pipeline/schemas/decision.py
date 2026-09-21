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

from pydantic import BaseModel, Field, field_validator, model_validator

# DoS bounds — every field a runaway Nemotron response could inflate
# gets capped at the schema boundary so downstream consumers (audit
# log, reply subject, PDF slot dict) can trust the shape. Numbers
# chosen an order of magnitude above realistic use: a typical decision
# has 1–5 citations against ~10 rules; 128 is a hard ceiling nothing
# legitimate hits. Module-level rather than class-level so the
# @field_validator classmethods can reference them without going
# through Pydantic's class-attribute machinery (which turns
# underscore-prefixed class attrs into PrivateAttr descriptors).
_MAX_RATIONALE_CHARS = 2048
_MAX_CITATIONS = 128
_MAX_MISSING_FIELDS = 128
_MAX_MISSING_FIELD_CHARS = 200
_MAX_RULE_TRACES = 128


Outcome = Literal["approve", "deny", "missing_info", "routed"]
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
    "reason_disagrees_with_deterministic_evaluator",
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
    # See the _cap_* validators below. The four Nemotron-authored fields
    # (rationale, citations, missing_fields, rule_traces) all flow into
    # audit notes, reply subjects, and PDF template slots — each has a
    # length cap that truncates silently rather than raising so a runaway
    # model output degrades usefully rather than failing the whole case.
    rationale: str = ""
    # Populated post-hoc by pap_pipeline.verify — extraction disagreements
    # (dual-VLM cross-check) and deterministic citation-consistency flags.
    # Non-empty findings escalate the case to HumanLoop.
    verifier_findings: list[VerifierFinding] = Field(default_factory=list)
    # Per-rule deterministic evaluator output, keyed by rule_id.
    # Populated after the reason stage by the orchestrator when a rule's
    # criterion falls within the boolean subset the evaluator handles
    # (see pap_pipeline.rule_evaluator). Each entry carries:
    #   {"verdict": matched|unmatched|undetermined|unparseable,
    #    "steps": [{"text": str, "result": bool|None}, ...],
    #    "justification": str,
    #    "agrees_with_reason": bool|None}
    # "agrees_with_reason" is None when the evaluator was unparseable
    # (no comparison possible); False cases surface as verifier
    # findings so the reviewer sees both answers side-by-side.
    rule_traces: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Populated by the Triage stage (runs after extract, before retrieve).
    # Optional so pre-Triage callers and stored Decisions from before the
    # Triage rollout still validate. Non-null carries the case_type +
    # downstream route the classifier chose; downstream systems that want
    # to short-circuit off the Decide path read this field.
    triage: "TriageResult | None" = None

    @field_validator("rationale", mode="before")
    @classmethod
    def _cap_rationale(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)[:_MAX_RATIONALE_CHARS]

    @field_validator("citations", mode="before")
    @classmethod
    def _cap_citations(cls, v: Any) -> Any:
        """Drop citations past a sanity cap so a runaway response can't
        emit a 10k-item citation list that then rides into every
        audit entry and the JSON verdict. Truncate rather than raise —
        the first N citations are still useful evidence."""
        if isinstance(v, list) and len(v) > _MAX_CITATIONS:
            return v[:_MAX_CITATIONS]
        return v

    @field_validator("missing_fields", mode="before")
    @classmethod
    def _cap_missing_fields(cls, v: Any) -> Any:
        """Cap list length + per-entry char count. Legitimate missing_fields
        are Intake field names (~20 chars); a model that emits 500-char
        prose strings here is misbehaving and the letter body would render
        it verbatim otherwise."""
        if not isinstance(v, list):
            return v
        capped = v[:_MAX_MISSING_FIELDS]
        return [str(item)[:_MAX_MISSING_FIELD_CHARS] for item in capped]

    @field_validator("rule_traces", mode="before")
    @classmethod
    def _cap_rule_traces(cls, v: Any) -> Any:
        """Cap the number of rule_traces entries. A trace-per-rule map
        that grew to thousands would balloon the persisted Decision JSON
        — 128 rules per case is already a huge corpus by demo standards."""
        if isinstance(v, dict) and len(v) > _MAX_RULE_TRACES:
            keys = list(v.keys())[:_MAX_RULE_TRACES]
            return {k: v[k] for k in keys}
        return v


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
