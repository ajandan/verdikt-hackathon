"""Structured-formula evaluator for eligibility rules.

Companion to :mod:`pap_pipeline.rule_evaluator` (the text-criterion
pattern-matcher). This module takes a **structured JSON formula** —
authored at rule-design time via LLM-assisted translation from the
free-form ``criterion`` — and walks the tree deterministically against
the extraction dict.

Design goals:

* **Full grammar.** No "unparseable" as a normal outcome — a formula
  that validates against the pydantic schema will always resolve to
  matched / unmatched / undetermined. (Undetermined only when a
  referenced ``field`` is null / absent in the values dict.)
* **Recursive nesting.** ``AND`` / ``OR`` / ``NOT`` compose arbitrarily
  deep; leaf ops (comparisons, set membership, regex, …) sit at the
  leaves. Same shape as SQL WHERE trees or JSONLogic.
* **Same return shape** as text-criterion path (``Evaluation`` + ``Step``
  list from :mod:`rule_evaluator`) so downstream consumers — the
  orchestrator's ``_attach_rule_evaluator_traces`` + the UI's Why?
  panel — don't have to type-switch.

**Formula shape.** Each node has an ``op`` discriminator + op-specific
fields. The examples below round-trip through
:func:`Formula.model_validate`::

    # Composite
    {"op": "AND", "terms": [<sub-node>, <sub-node>, ...]}
    {"op": "OR",  "terms": [<sub-node>, <sub-node>, ...]}
    {"op": "NOT", "term":  <sub-node>}

    # Comparison against a literal
    {"op": "==", "field": "ins_uninsured", "value": true}
    {"op": "<=", "field": "annual_household_income", "value": 84600}

    # Comparison against another field
    {"op": "field_eq_field", "left": "patient_signature_date",
                             "right": "prescriber_signature_date"}

    # Set membership
    {"op": "in",     "field": "patient_state", "values": ["CA", "NY", ...]}
    {"op": "not_in", "field": "insurance_status", "values": ["denied", "expired"]}

    # Date recency
    {"op": "within_days",   "field": "signature_date", "n": 90}
    {"op": "within_weeks",  "field": "signature_date", "n": 4}
    {"op": "within_months", "field": "prescription_date", "n": 12}
    {"op": "within_years",  "field": "consent_date", "n": 2}

    # String pattern
    {"op": "matches_regex", "field": "prescriber_npi", "pattern": "^\\d{10}$"}
    {"op": "starts_with",   "field": "email", "value": "admin@"}
    {"op": "ends_with",     "field": "npi_display", "value": " (verified)"}
    {"op": "contains",      "field": "notes", "value": "urgent"}

    # Digit count (loose regex over field digits)
    {"op": "digit_count_equals", "field": "prescriber_npi", "n": 10}

    # Near-match for scanned text: equal to ``value`` after case-folding,
    # allowing up to ``n`` single-character edits (0 to 2). Absorbs an OCR
    # misread such as "Cardiovalta" for "Cardiovita" while still rejecting
    # a different drug name.
    {"op": "similar_to", "field": "rx_medication", "value": "Cardiovita", "n": 2}

    # Null checks
    {"op": "is_null",     "field": "denial_letter_url"}
    {"op": "is_not_null", "field": "prescriber_signature"}

Because operand shapes vary by op, validation is enforced by a
pydantic ``model_validator`` — invalid combinations (e.g. ``==`` with
no ``field``) raise at parse time, not at run time.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pap_pipeline.rule_evaluator import Evaluation, Step


# ---------------------------------------------------------------------------
# Op vocabulary. Kept as literal sets so IDE / mypy / pydantic all agree on
# what's a valid discriminator and error messages name the actual allowed
# values.
# ---------------------------------------------------------------------------

_LOGICAL_OPS = {"AND", "OR", "NOT"}
# Bounds on the matches_regex op — tenant admins author these patterns,
# and Python's `re` engine has no timeout, so a pathological pattern
# (nested quantifiers, back-references) would let one admin DoS their
# own tenant. Two-layer cap:
#
#   - _MAX_REGEX_PATTERN_LEN caps the pattern authored on the rule row.
#     500 chars fits every realistic identifier check (NPI, DEA, dates)
#     while rejecting the multi-KB nested-group patterns that produce
#     catastrophic backtracking.
#   - _MAX_REGEX_INPUT_LEN caps the value being matched. Intake string
#     fields top out at hundreds of chars in practice; 4 KB is a
#     generous ceiling that still bounds worst-case backtracking time.
_MAX_REGEX_PATTERN_LEN = 500
_MAX_REGEX_INPUT_LEN = 4096
# similar_to tolerates at most this many single-character edits; beyond two,
# distinct names start to match each other.
_MAX_SIMILAR_DISTANCE = 2

_COMPARISON_OPS = {"==", "!=", "<", "<=", ">", ">="}
_SET_OPS = {"in", "not_in"}
_RECENCY_OPS = {"within_days", "within_weeks", "within_months", "within_years"}
_STRING_OPS = {"starts_with", "ends_with", "contains"}
_NULL_OPS = {"is_null", "is_not_null"}
_MISC_OPS = {"matches_regex", "digit_count_equals", "field_eq_field", "similar_to"}

_ALL_OPS = (
    _LOGICAL_OPS
    | _COMPARISON_OPS
    | _SET_OPS
    | _RECENCY_OPS
    | _STRING_OPS
    | _NULL_OPS
    | _MISC_OPS
)

Op = Literal[
    "AND", "OR", "NOT",
    "==", "!=", "<", "<=", ">", ">=",
    "in", "not_in",
    "within_days", "within_weeks", "within_months", "within_years",
    "matches_regex",
    "starts_with", "ends_with", "contains",
    "is_null", "is_not_null",
    "digit_count_equals",
    "field_eq_field",
    "similar_to",
]

# Approximate day counts per recency unit — matches the text-path
# evaluator's convention so authors get identical results whether their
# rule uses the text form ("within the last 3 months") or the formula
# form ({"op": "within_months", "n": 3}).
_UNIT_TO_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_RECENCY_OP_UNIT = {
    "within_days": "day",
    "within_weeks": "week",
    "within_months": "month",
    "within_years": "year",
}


# ---------------------------------------------------------------------------
# Pydantic AST. One flexible model with optional per-op fields, enforced
# by a single ``model_validator``. Alternative discriminated-union setup
# is verbose for 20+ ops; the model-validator approach gives sharper
# error messages ("op '==' requires field + value").
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """One node in a formula AST — either a logical composer or a leaf op."""

    model_config = ConfigDict(extra="forbid")

    op: Op = Field(..., description="Node operator. See module docstring for the full vocabulary.")

    # Composite: AND / OR use terms; NOT uses term (singular). Optional[…]
    # wraps the forward reference so pydantic can resolve on model_rebuild
    # without the ``str | None`` parse trap that hits `"Node" | None` under
    # Python 3.12's PEP 604 evaluator.
    terms: Optional[list["Node"]] = None
    term: Optional["Node"] = None

    # Field references. Most leaf ops need `field` (single); field_eq_field uses left+right.
    field: str | None = None
    left: str | None = None
    right: str | None = None

    # Literals. `value` for comparisons + string ops; `values` for set ops.
    value: Any | None = None
    values: list[Any] | None = None

    # Numeric parameters. `n` used by recency (window size) + digit_count_equals.
    n: int | None = None

    # Regex pattern for matches_regex.
    pattern: str | None = None

    @model_validator(mode="after")
    def _enforce_per_op_fields(self) -> "Node":
        op = self.op
        if op in _LOGICAL_OPS:
            if op == "NOT":
                if self.term is None or self.terms is not None:
                    raise ValueError("op 'NOT' requires 'term' (single sub-node), not 'terms'")
            else:  # AND / OR
                if not self.terms or len(self.terms) < 1:
                    raise ValueError(f"op '{op}' requires 'terms' (list of ≥1 sub-nodes)")
                if self.term is not None:
                    raise ValueError(f"op '{op}' uses 'terms' (plural), not 'term'")
            for wrong in ("field", "left", "right", "value", "values", "n", "pattern"):
                if getattr(self, wrong) is not None:
                    raise ValueError(f"op '{op}' does not take '{wrong}'")
            return self

        # All non-logical ops forbid `terms` / `term`.
        if self.terms is not None or self.term is not None:
            raise ValueError(f"op '{op}' is a leaf; must not carry 'terms' / 'term'")

        if op in _COMPARISON_OPS:
            if not self.field or self.value is None:
                raise ValueError(f"op '{op}' requires 'field' + 'value'")
        elif op in _SET_OPS:
            if not self.field or self.values is None:
                raise ValueError(f"op '{op}' requires 'field' + 'values' (list)")
            if not isinstance(self.values, list) or len(self.values) == 0:
                raise ValueError(f"op '{op}' requires a non-empty 'values' list")
        elif op in _RECENCY_OPS:
            if not self.field or self.n is None:
                raise ValueError(f"op '{op}' requires 'field' + 'n' (positive integer)")
            if self.n <= 0:
                raise ValueError(f"op '{op}' requires 'n' > 0")
        elif op == "matches_regex":
            if not self.field or not self.pattern:
                raise ValueError("op 'matches_regex' requires 'field' + 'pattern'")
            if len(self.pattern) > _MAX_REGEX_PATTERN_LEN:
                raise ValueError(
                    f"op 'matches_regex' pattern exceeds {_MAX_REGEX_PATTERN_LEN} "
                    f"chars (got {len(self.pattern)}); shorten the regex or split "
                    "the rule — long patterns risk catastrophic backtracking"
                )
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"op 'matches_regex' has invalid regex: {e}") from e
        elif op in _STRING_OPS:
            if not self.field or self.value is None:
                raise ValueError(f"op '{op}' requires 'field' + 'value' (substring)")
            if not isinstance(self.value, str):
                raise ValueError(f"op '{op}' requires 'value' to be a string")
        elif op in _NULL_OPS:
            if not self.field:
                raise ValueError(f"op '{op}' requires 'field'")
            for wrong in ("value", "values", "n", "pattern", "left", "right"):
                if getattr(self, wrong) is not None:
                    raise ValueError(f"op '{op}' does not take '{wrong}'")
        elif op == "digit_count_equals":
            if not self.field or self.n is None:
                raise ValueError("op 'digit_count_equals' requires 'field' + 'n'")
            if self.n <= 0:
                raise ValueError("op 'digit_count_equals' requires 'n' > 0")
        elif op == "similar_to":
            if not self.field or not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("op 'similar_to' requires 'field' + 'value' (non-empty string)")
            if self.n is None or not 0 <= self.n <= _MAX_SIMILAR_DISTANCE:
                raise ValueError(
                    f"op 'similar_to' requires 'n' between 0 and {_MAX_SIMILAR_DISTANCE} "
                    "(maximum single-character edits)"
                )
        elif op == "field_eq_field":
            if not self.left or not self.right:
                raise ValueError("op 'field_eq_field' requires 'left' + 'right' (both field names)")
            if self.field is not None:
                raise ValueError("op 'field_eq_field' uses 'left' + 'right', not 'field'")
        else:  # pragma: no cover — pydantic Literal already rejects unknown ops
            raise ValueError(f"unknown op '{op}'")
        return self


Node.model_rebuild()


class Formula(BaseModel):
    """Top-level container for a rule formula. Just a single root ``Node``.

    Split from ``Node`` so the API is ``Formula.model_validate({"op": ...})``
    with a clear entry point. Storage in the DB is the raw dict.
    """

    model_config = ConfigDict(extra="forbid")
    root: Node


# ---------------------------------------------------------------------------
# Value normalisation. Same predicates the text-path evaluator uses so
# authors get identical semantics whether they author in text or formula.
# ---------------------------------------------------------------------------


def _is_undetermined(v: Any) -> bool:
    """A field is undetermined when the extraction gave us nothing usable.

    Booleans (including False) count as populated — False is a real
    checkbox answer, not "unknown". Same predicate as
    :func:`pap_pipeline.rule_evaluator._is_undetermined` so the two
    evaluators agree on what "missing" means.
    """
    if isinstance(v, bool):
        return False
    return v is None or v == "" or v == []


def _norm_scalar(v: Any) -> Any:
    """Booleans stay booleans; strings get case-folded; everything else through."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().casefold()
    return v


def _eq(a: Any, b: Any) -> bool:
    return _norm_scalar(a) == _norm_scalar(b)


_NUMBER_NOISE = re.compile(r"[\s,$]|USD$", re.IGNORECASE)


def _to_number(v: Any) -> float:
    """Read a number the way a form prints it: ``60000``, ``"60000"``,
    ``"$60,000"`` or ``"60,000 USD"``. Booleans are not numbers.

    Raises ``ValueError`` / ``TypeError`` for anything else, which the
    comparison treats as undetermined rather than false.
    """
    if isinstance(v, bool):
        raise TypeError("boolean is not a number")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return float(_NUMBER_NOISE.sub("", v.strip()))
    raise TypeError(f"not a number: {type(v).__name__}")


def _repr(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f"'{v}'"
    return str(v)


# Date parser — accepts what the extractor emits (ISO + slash-US).
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d")


def _parse_date_value(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Tree-walker evaluator. Returns (result, steps) per node; the caller
# folds the top-level result into an Evaluation.
# ---------------------------------------------------------------------------

# Sentinel for the AND/OR combine tables below — three-valued logic
# (True / False / None-undetermined).
def _combine_and(rs: list[bool | None]) -> bool | None:
    if any(r is False for r in rs):
        return False
    if any(r is None for r in rs):
        return None
    return True


def _combine_or(rs: list[bool | None]) -> bool | None:
    if any(r is True for r in rs):
        return True
    if any(r is None for r in rs):
        return None
    return False


def _eval_node(node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    """Recursively evaluate a node. Returns (result, steps) where steps
    is a flat trace suitable for the reviewer UI (nested logical ops
    show their sub-clause results, not the composite)."""
    op = node.op

    # ---- Logical composers ------------------------------------------------
    if op == "AND":
        sub_results: list[bool | None] = []
        sub_steps: list[Step] = []
        for term in node.terms or []:
            r, s = _eval_node(term, values)
            sub_results.append(r)
            sub_steps.extend(s)
        combined = _combine_and(sub_results)
        return combined, sub_steps
    if op == "OR":
        sub_results = []
        sub_steps = []
        for term in node.terms or []:
            r, s = _eval_node(term, values)
            sub_results.append(r)
            sub_steps.extend(s)
        combined = _combine_or(sub_results)
        return combined, sub_steps
    if op == "NOT":
        assert node.term is not None
        r, s = _eval_node(node.term, values)
        negated: bool | None = None if r is None else (not r)
        # Prepend a synthetic "NOT (...)" step so the trace shows the
        # negation. Sub-step results are what got negated.
        summary = f"NOT (subclause was {_repr_result(r)}) → {_repr_result(negated)}"
        return negated, [Step(text=summary, result=negated), *s]

    # ---- Leaf ops ---------------------------------------------------------
    if op in _COMPARISON_OPS:
        return _eval_comparison(op, node, values)
    if op in _SET_OPS:
        return _eval_set(op, node, values)
    if op in _RECENCY_OPS:
        return _eval_recency(op, node, values)
    if op == "matches_regex":
        return _eval_regex(node, values)
    if op in _STRING_OPS:
        return _eval_string(op, node, values)
    if op in _NULL_OPS:
        return _eval_null(op, node, values)
    if op == "digit_count_equals":
        return _eval_digit_count(node, values)
    if op == "field_eq_field":
        return _eval_field_eq_field(node, values)
    if op == "similar_to":
        return _eval_similar_to(node, values)

    raise RuntimeError(f"unreachable — validator rejects unknown ops ({op!r})")


def _repr_result(r: bool | None) -> str:
    if r is True:
        return "true"
    if r is False:
        return "false"
    return "undetermined"


def _eval_comparison(op: str, node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    literal = node.value
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} {op} {_repr(literal)} → undetermined (no value extracted)",
            result=None,
        )]
    try:
        if op == "==":
            ok = _eq(actual, literal)
        elif op == "!=":
            ok = not _eq(actual, literal)
        elif op == "<":
            ok = _to_number(actual) < _to_number(literal)
        elif op == ">":
            ok = _to_number(actual) > _to_number(literal)
        elif op == "<=":
            ok = _to_number(actual) <= _to_number(literal)
        elif op == ">=":
            ok = _to_number(actual) >= _to_number(literal)
        else:  # pragma: no cover
            raise RuntimeError(f"unknown comparison op {op!r}")
    except (TypeError, ValueError):
        # Numeric op against non-numeric actual — treat as undetermined
        # rather than false; the formula's shape doesn't fit the data.
        return None, [Step(
            text=f"@{field_name} {op} {_repr(literal)} → undetermined (actual {_repr(actual)} is not comparable)",
            result=None,
        )]
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=f"@{field_name} {op} {_repr(literal)} → {verdict_word} (actual: {_repr(actual)})",
        result=ok,
    )]


def _eval_set(op: str, node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    vals = node.values or []
    actual = values.get(field_name)
    display_set = ", ".join(_repr(v) for v in vals)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} {op} {{{display_set}}} → undetermined (no value extracted)",
            result=None,
        )]
    in_set = any(_eq(actual, v) for v in vals)
    ok = in_set if op == "in" else (not in_set)
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=f"@{field_name} {op} {{{display_set}}} → {verdict_word} (actual: {_repr(actual)})",
        result=ok,
    )]


def _eval_recency(op: str, node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    n = node.n or 0
    unit = _RECENCY_OP_UNIT[op]
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} within last {n} {unit}(s) of today → undetermined (no value extracted)",
            result=None,
        )]
    parsed = _parse_date_value(actual)
    if parsed is None:
        return None, [Step(
            text=f"@{field_name} within last {n} {unit}(s) of today → undetermined (value {_repr(actual)} is not a date)",
            result=None,
        )]
    today = date.today()
    window_days = n * _UNIT_TO_DAYS[unit]
    delta_days = (today - parsed).days
    ok = 0 <= delta_days <= window_days
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=(
            f"@{field_name} within last {n} {unit}(s) of today → {verdict_word} "
            f"(actual: {parsed.isoformat()}, {delta_days} day(s) ago; window {window_days} day(s))"
        ),
        result=ok,
    )]


def _eval_regex(node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    pattern = node.pattern or ""
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} matches /{pattern}/ → undetermined (no value extracted)",
            result=None,
        )]
    # Cap the input so a pathological pattern + long value can't burn
    # the reason-stage budget on catastrophic backtracking. Python's re
    # engine has no timeout — bounded input is the only defense.
    actual_str = str(actual)[:_MAX_REGEX_INPUT_LEN]
    ok = bool(re.fullmatch(pattern, actual_str))
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=f"@{field_name} matches /{pattern}/ → {verdict_word} (actual: {_repr(actual)})",
        result=ok,
    )]


def _eval_string(op: str, node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    needle = node.value or ""
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} {op} {_repr(needle)} → undetermined (no value extracted)",
            result=None,
        )]
    haystack = str(actual)
    if op == "starts_with":
        ok = haystack.startswith(str(needle))
    elif op == "ends_with":
        ok = haystack.endswith(str(needle))
    elif op == "contains":
        ok = str(needle) in haystack
    else:  # pragma: no cover
        raise RuntimeError(f"unknown string op {op!r}")
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=f"@{field_name} {op} {_repr(needle)} → {verdict_word} (actual: {_repr(actual)})",
        result=ok,
    )]


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (insert / delete / substitute)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _eval_similar_to(node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    literal = str(node.value)
    limit = node.n or 0
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} similar to {_repr(literal)} → undetermined (no value extracted)",
            result=None,
        )]
    distance = _edit_distance(str(_norm_scalar(actual)), str(_norm_scalar(literal)))
    ok = distance <= limit
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=(
            f"@{field_name} similar to {_repr(literal)} (within {limit} edit(s)) → {verdict_word} "
            f"(actual: {_repr(actual)}, {distance} edit(s) away)"
        ),
        result=ok,
    )]


def _eval_null(op: str, node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    actual = values.get(field_name)
    is_null = _is_undetermined(actual)
    if op == "is_null":
        ok = is_null
        text = f"@{field_name} is_null → {'matched' if ok else 'did not match'} (actual: {_repr(actual)})"
    else:  # is_not_null
        ok = not is_null
        text = f"@{field_name} is_not_null → {'matched' if ok else 'did not match'} (actual: {_repr(actual)})"
    return ok, [Step(text=text, result=ok)]


def _eval_digit_count(node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    field_name = node.field or ""
    n = node.n or 0
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return None, [Step(
            text=f"@{field_name} digit_count_equals {n} → undetermined (no value extracted)",
            result=None,
        )]
    src = str(actual)
    digits_only = re.sub(r"\D", "", src)
    only_digits_and_seps = bool(re.fullmatch(r"[\d\s\-]+", src))
    ok = only_digits_and_seps and len(digits_only) == n
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=(
            f"@{field_name} digit_count_equals {n} → {verdict_word} "
            f"(actual: {_repr(actual)}, {len(digits_only)} digit(s))"
        ),
        result=ok,
    )]


def _eval_field_eq_field(node: Node, values: dict[str, Any]) -> tuple[bool | None, list[Step]]:
    left, right = node.left or "", node.right or ""
    a, b = values.get(left), values.get(right)
    if _is_undetermined(a) or _is_undetermined(b):
        return None, [Step(
            text=f"@{left} == @{right} → undetermined (one side null: @{left}={_repr(a)}, @{right}={_repr(b)})",
            result=None,
        )]
    ok = _eq(a, b)
    verdict_word = "matched" if ok else "did not match"
    return ok, [Step(
        text=f"@{left} == @{right} → {verdict_word} (values: {_repr(a)}, {_repr(b)})",
        result=ok,
    )]


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def evaluate_formula(formula: dict[str, Any] | Node, values: dict[str, Any]) -> Evaluation:
    """Evaluate a structured formula against a values dict.

    ``formula`` may be a raw dict (JSON round-trip shape) or an already-
    validated :class:`Node`. Raw dicts pass through pydantic validation
    first; malformed shapes raise :class:`pydantic.ValidationError` at
    that step — treat those as an authoring error, not a runtime one
    (the rule editor's save-time validator should have caught it).

    Returns an :class:`Evaluation` with:

    * ``verdict`` — ``matched`` / ``unmatched`` / ``undetermined``.
      A structured formula never returns ``unparseable`` at runtime;
      that only happens for text-criterion rules the pattern matcher
      can't recognise.
    * ``steps`` — one per leaf clause evaluated, in traversal order.
      Nested composites (``AND``/``OR``/``NOT``) flatten so the reviewer
      UI can render a clean bullet list.
    * ``justification`` — single reader-friendly sentence composed of
      the verdict + step trace, ready to drop into the Why? panel.
    """
    node = formula if isinstance(formula, Node) else Formula.model_validate({"root": formula}).root
    result, steps = _eval_node(node, values)
    verdict: str
    if result is True:
        verdict = "matched"
        headline = "matched — all conditions satisfied."
    elif result is False:
        verdict = "unmatched"
        headline = "not matched — at least one condition failed."
    else:
        verdict = "undetermined"
        headline = "undetermined — required data is missing."
    justification = f"{headline} " + " ".join(s.text for s in steps)
    return Evaluation(verdict=verdict, steps=steps, justification=justification.strip())


__all__ = [
    "Formula",
    "Node",
    "Op",
    "evaluate_formula",
]
