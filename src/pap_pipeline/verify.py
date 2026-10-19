"""Deterministic post-verification of the pipeline output.

Two related checks, both producing :class:`VerifierFinding` entries that
land on :attr:`Decision.verifier_findings`:

- :func:`dual_extract` (Phase C) — runs a second Extract stage with a
  different VLM in parallel and diffs the two intake dicts. Any field
  where both models produced non-null, unequal values is flagged as an
  ``extract_disagreement``.

- :func:`verify_decision` (Phase D) — runs seven deterministic
  consistency invariants against Nemotron's decision. Catches
  hallucinated citations, verdict/citation contradictions,
  `missing_info` misuse, and outcome-vocabulary violations. Cannot
  catch predicate-interpretation errors (rules are natural-language
  strings, not machine-evaluable predicates in this codebase — Nemotron
  citing CV-INC-01 as "matched=True" when the underlying income rule
  should have failed cannot be verified in pure Python. That class of
  error requires either a structured-predicate schema on the rule row
  or a second LLM reason pass; both are out of scope for this pass).

Both checks are additive: findings never modify the Decision's outcome
or citations. Escalation to HumanLoop is the sole action taken on
non-empty findings, and that lives in ``inbox_hook`` / ``api_hook``.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from pap_pipeline.schemas.decision import Decision, VerifierFinding
from pap_pipeline.schemas.rule import Rule
from pap_pipeline.stages.extract import ExtractClientConfig, extract


async def dual_extract(
    attachments: list[bytes],
    *,
    primary_config: ExtractClientConfig,
    verifier_config: ExtractClientConfig,
    slots: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[VerifierFinding]]:
    """Run primary + verifier Extract in parallel; return primary + findings.

    Runs both models against the same attachments and same slot schema so
    the two dicts have identical keys. For every slot where both models
    produced a non-null value:

    - **Both agree** — nothing emitted.
    - **Both disagree** (values differ, both non-null) — one
      ``extract_disagreement`` finding is emitted with both values.
    - **One is null** — treated as an *extraction gap* on the null side, not
      a disagreement. The null side may be a genuine missing field that
      HumanLoop already handles via the ``missing_info`` verdict path.

    The primary extraction is what downstream stages consume; the verifier
    output is used only for the diff and then discarded. Verifier failure
    (network error, timeout, JSON parse) is caught locally and emitted as
    a single ``extract_disagreement`` with ``severity="warning"`` so the
    pipeline never blocks on a flaky verifier.
    """
    async def _run_verifier() -> dict[str, Any] | None:
        try:
            return await extract(attachments, config=verifier_config, slots=slots)
        except Exception as e:
            return {"__verifier_error__": f"{type(e).__name__}: {e}"}

    primary_raw, verifier_raw = await asyncio.gather(
        extract(attachments, config=primary_config, slots=slots),
        _run_verifier(),
    )

    findings: list[VerifierFinding] = []
    if verifier_raw is None:
        return primary_raw, findings

    if "__verifier_error__" in verifier_raw:
        findings.append(
            VerifierFinding(
                type="extract_disagreement",
                severity="warning",
                detail={"reason": "verifier_call_failed", "error": verifier_raw["__verifier_error__"]},
            )
        )
        return primary_raw, findings

    all_keys = set(primary_raw.keys()) | set(verifier_raw.keys())
    for key in sorted(all_keys):
        p_val = primary_raw.get(key)
        v_val = verifier_raw.get(key)
        # Skip null-vs-null (both extractors agreed nothing's there).
        if p_val is None and v_val is None:
            continue
        # Skip null-vs-value (extraction gap, not a disagreement).
        if p_val is None or v_val is None:
            continue
        # Both non-null but unequal → disagreement.
        if not _values_agree(p_val, v_val):
            findings.append(
                VerifierFinding(
                    type="extract_disagreement",
                    severity="critical",
                    detail={
                        "field": key,
                        "primary": p_val,
                        "verifier": v_val,
                    },
                )
            )

    return primary_raw, findings


def _normalize(value: Any) -> Any:
    """Value comparison that ignores trivial noise between VLMs.

    Strings are stripped + lowercased; numeric strings that parse cleanly
    become the number so ``"60000"`` and ``60000`` compare equal. Everything
    else is compared as-is. This keeps the diff signal-focused; two VLMs
    disagreeing on "  Devon Okafor" vs "Devon Okafor" isn't a real
    disagreement.
    """
    if isinstance(value, str):
        stripped = value.strip()
        try:
            # Numbers-as-strings shouldn't count as text disagreements.
            if stripped and stripped.replace(".", "", 1).lstrip("-").isdigit():
                return float(stripped)
        except ValueError:
            pass
        return stripped.lower()
    return value


_NUMBER_WITH_UNIT = re.compile(r"\s*(-?\d+(?:\.\d+)?)\s+[A-Za-z][A-Za-z .]*")


def _values_agree(a: Any, b: Any) -> bool:
    """Whether two extractors read the same value, allowing for how VLMs
    differ in writing it down.

    Beyond :func:`_normalize`, two forms are treated as the same reading:

    - a bare number and that number with a unit (``"30"`` / ``"30 tablets"``).
      Two different units (``"30 mg"`` / ``"30 tablets"``) still disagree.
    - a name with the words in another order or with a middle initial on one
      side only (``"Mei L Chen"`` / ``"Chen Mei"``). Only for values with no
      digits, so dates and IDs never compare by sorted parts, and only when
      two or more full words remain, so ``"Y"`` / ``"N"`` or ``"Type A"`` /
      ``"Type B"`` are never reduced to a match. Conflicting initials still
      disagree.
    - the same text with different spacing or punctuation (``"Oaks ville"`` /
      ``"Oaksville"``, ``"Apt 3B"`` / ``"Apt3B"``). Never for numbers, and every
      run of digits must survive intact, so ``"-5"`` / ``"5"``, ``"$10.50"`` /
      ``"1050"`` and ``"1,000"`` / ``"1000"`` still disagree.

    Anything else that differs is a disagreement and goes to a reviewer.
    """
    na, nb = _normalize(a), _normalize(b)
    if na == nb:
        return True
    for num, other in ((na, b), (nb, a)):
        if isinstance(num, (int, float)) and not isinstance(num, bool) and isinstance(other, str):
            m = _NUMBER_WITH_UNIT.fullmatch(other)
            if m and float(m.group(1)) == num:
                return True
    if (isinstance(a, str) and isinstance(b, str)
            and not isinstance(na, float) and not isinstance(nb, float)
            and re.sub(r"[^a-z0-9]", "", na) == re.sub(r"[^a-z0-9]", "", nb)
            and re.findall(r"\d+", a) == re.findall(r"\d+", b)):
        return True
    if isinstance(a, str) and isinstance(b, str) and not any(c.isdigit() for c in a + b):
        wa, wb = re.findall(r"[a-z]+", a.lower()), re.findall(r"[a-z]+", b.lower())
        full_a, full_b = sorted(w for w in wa if len(w) > 1), sorted(w for w in wb if len(w) > 1)
        init_a, init_b = {w for w in wa if len(w) == 1}, {w for w in wb if len(w) == 1}
        if len(full_a) >= 2 and full_a == full_b and (init_a <= init_b or init_b <= init_a):
            return True
    return False


def verify_decision(
    decision: Decision,
    *,
    intake_dict: dict[str, Any],
    retrieved_rule_ids: list[str],
    retrieved_rules: list[Rule],
    declared_outcomes: list[str] | None = None,
) -> list[VerifierFinding]:
    """Run seven consistency invariants against a Nemotron Decision.

    All checks are deterministic — no LLM call. They catch the failure
    modes we CAN detect without a machine-evaluable rule predicate:

    1. ``hallucinated_citation`` — a cited rule_id that isn't in the
       retrieved rule set (Nemotron invented an ID).
    2. ``cited_rule_not_retrieved`` — a cited rule_id that IS a real rule
       but wasn't in Nemotron's retrieval context (should be caught by
       (1) unless the caller passes a broader rule set).
    3. ``outcome_missing_info_but_no_gaps`` — outcome is ``missing_info``
       but ``missing_fields`` is empty.
    4. ``outcome_missing_info_gap_not_null_in_intake`` — Nemotron says a
       field is missing but the intake actually has a value for it.
    5. ``missing_field_not_referenced_by_cited_rule`` — Nemotron asks for
       a field that no cited rule declares in its ``fields`` list.
    6. ``outcome_approve_no_matched_citation`` — approve verdict but no
       cited rule shows ``matched=True``.
    7. ``outcome_deny_no_unmatched_citation`` — deny verdict but every
       cited rule shows ``matched=True``.
    8. ``outcome_outside_declared_vocabulary`` — outcome is not in the
       program's declared outcomes list (when passed).

    Returns findings; does not modify the Decision.
    """
    findings: list[VerifierFinding] = []
    retrieved_ids = set(retrieved_rule_ids)
    fields_by_rule = {r.id: set(r.fields or []) for r in retrieved_rules}

    rule_citations = [c for c in decision.citations if c.type == "rule"]

    # Check 1 + 2 collapsed — anything Nemotron cited must be in the retrieval
    # set the reason stage saw. If the caller passes a broader rule corpus in
    # `retrieved_rules` than `retrieved_rule_ids`, both check numbers converge.
    for cit in rule_citations:
        if cit.rule_id not in retrieved_ids:
            findings.append(
                VerifierFinding(
                    type="hallucinated_citation",
                    detail={
                        "cited_rule_id": cit.rule_id,
                        "retrieved_rule_ids": sorted(retrieved_ids),
                    },
                )
            )

    # Check 3 — missing_info verdicts must actually list missing fields.
    if decision.outcome == "missing_info" and not decision.missing_fields:
        findings.append(
            VerifierFinding(
                type="outcome_missing_info_but_no_gaps",
                detail={"rationale": decision.rationale},
            )
        )

    # Check 4 — every "missing" field must actually be null/empty in the intake.
    for field in decision.missing_fields:
        val = intake_dict.get(field)
        if val is not None and val != "":
            findings.append(
                VerifierFinding(
                    type="outcome_missing_info_gap_not_null_in_intake",
                    detail={"field": field, "intake_value": val},
                )
            )

    # Check 5 — every missing field must be referenced by at least one cited rule.
    cited_rule_ids = {c.rule_id for c in rule_citations}
    fields_declared_by_cited = set()
    for rid in cited_rule_ids:
        fields_declared_by_cited.update(fields_by_rule.get(rid, set()))
    for field in decision.missing_fields:
        if field not in fields_declared_by_cited:
            findings.append(
                VerifierFinding(
                    type="missing_field_not_referenced_by_cited_rule",
                    severity="warning",
                    detail={
                        "field": field,
                        "cited_rules": sorted(cited_rule_ids),
                    },
                )
            )

    # Check 6 — approve verdicts must have at least one matched=True citation.
    if decision.outcome == "approve" and rule_citations:
        if not any(c.matched for c in rule_citations):
            findings.append(
                VerifierFinding(
                    type="outcome_approve_no_matched_citation",
                    detail={
                        "matched_states": [c.matched for c in rule_citations],
                    },
                )
            )

    # Check 7 — deny verdicts must have at least one matched=False citation.
    if decision.outcome == "deny" and rule_citations:
        if all(c.matched for c in rule_citations):
            findings.append(
                VerifierFinding(
                    type="outcome_deny_no_unmatched_citation",
                    detail={
                        "matched_states": [c.matched for c in rule_citations],
                    },
                )
            )

    # Check 8 — outcome vocabulary. Only when the program declared its set.
    # ``routed`` is a pipeline-internal sentinel emitted by the Triage
    # skip-Decide path, never authored on a Program, so it's exempt from
    # the declared-outcomes check.
    if (
        declared_outcomes
        and decision.outcome != "routed"
        and decision.outcome not in declared_outcomes
    ):
        findings.append(
            VerifierFinding(
                type="outcome_outside_declared_vocabulary",
                detail={
                    "outcome": decision.outcome,
                    "declared": list(declared_outcomes),
                },
            )
        )

    return findings


__all__ = [
    "dual_extract",
    "verify_decision",
]
