"""Deterministic evaluator for the boolean subset of rule criteria.

Rules in the ``criterion`` string are free-form natural language, but a
sizeable chunk follow a small mechanical pattern: ``@field <op> <literal>``
subclauses joined by ``AND`` / ``OR``. This module parses that subset and
returns a verdict plus a plain-English step trace. Rules outside the
subset (prose predicates like "10-digit number" or "within 12 months",
arithmetic expressions, function calls, or nested conditions inside set
literals) are reported as **unparseable** — the LLM's verdict for those
stands untouched.

The point is not to replace the LLM. It's to catch its obvious reasoning
mistakes on rules simple enough to evaluate mechanically. When the
deterministic verdict disagrees with the LLM's ``matched=true/false``,
the orchestrator flags a verifier finding so the reviewer sees both
answers side-by-side.

**Justification trail.** Every evaluation also produces a one-sentence
plain-English summary + per-subclause step list, both suitable for
rendering directly in the reviewer UI's Why? panel. Reviewers get a
"here's exactly what the numbers said" without having to read the raw
criterion + reconcile it against the extraction dict themselves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Verdict = Literal["matched", "unmatched", "undetermined", "unparseable"]


@dataclass
class Step:
    """One line in the evaluation trace.

    ``text`` is the plain-English "what happened" for the subclause,
    e.g. ``"@insurance_status = 'uninsured' → matched (actual: 'uninsured')"``.
    ``result`` is None when the subclause is undetermined (a referenced
    field is null/absent) or when the subclause itself couldn't parse.
    """

    text: str
    result: bool | None


@dataclass
class Evaluation:
    """Deterministic evaluator output for one rule.

    ``verdict`` is the load-bearing signal — matched/unmatched are
    trustworthy when parseable; undetermined means a referenced field
    is missing; unparseable means the criterion uses shapes outside
    this evaluator's grammar (arithmetic, function calls, prose
    predicates) and the LLM's verdict for the rule should be trusted
    over this module's silence.

    ``justification`` is a single reader-friendly sentence built from
    the steps — safe to drop straight into the UI without extra
    formatting.
    """

    verdict: Verdict
    steps: list[Step] = field(default_factory=list)
    justification: str = ""


# ---------------------------------------------------------------------------
# Criterion normalisation — strip common wrappers so the boolean core is
# what gets parsed. "If X, deny" is really about evaluating X; the
# ", deny" trailer is the outcome the rule fires, not part of its
# condition.
# ---------------------------------------------------------------------------

_STRIP_PREFIXES: list[re.Pattern[str]] = [
    re.compile(r"^\s*If\s+", re.IGNORECASE),
]
_STRIP_SUFFIXES: list[re.Pattern[str]] = [
    # Outcome directives at the tail of a rule ("..., deny — because X",
    # "..., approve on the renewal track", "..., matched"). Word-boundary
    # + `.*$` on each so any trailing prose after the outcome word is
    # sliced off along with it. Real rule criteria don't use "deny" /
    # "approve" as data operators — always as outcome markers — so a
    # broad match is safe.
    re.compile(r",?\s*deny\b.*$", re.IGNORECASE),
    re.compile(r",?\s*approve\b.*$", re.IGNORECASE),
    re.compile(r",?\s*matched?\b.*$", re.IGNORECASE),
]


def _strip_wrappers(text: str) -> str:
    t = text.strip().rstrip(".").strip()
    for pat in _STRIP_PREFIXES:
        t = pat.sub("", t, count=1)
    for pat in _STRIP_SUFFIXES:
        t = pat.sub("", t, count=1)
    return t.strip()


# ---------------------------------------------------------------------------
# Literal parsing. Values that appear in rule criteria after operators.
# ---------------------------------------------------------------------------

_BOOL_TRUE = {"true", "yes", "t"}
_BOOL_FALSE = {"false", "no", "f"}
_NULLISH = {"null", "none", "nil"}


def _parse_literal(raw: str) -> Any:
    r = raw.strip()
    if not r:
        return None
    lower = r.lower()
    if lower in _BOOL_TRUE:
        return True
    if lower in _BOOL_FALSE:
        return False
    if lower in _NULLISH:
        return None
    if (r.startswith("'") and r.endswith("'")) or (r.startswith('"') and r.endswith('"')):
        return r[1:-1]
    # Numeric?
    try:
        if "." in r:
            return float(r)
        return int(r)
    except ValueError:
        # Bare identifier — treat as string literal so `∈ {uninsured, ...}`
        # style enum values compare cleanly against extracted strings.
        return r


def _norm_scalar(v: Any) -> Any:
    """Normalise a scalar for comparison. Booleans stay booleans;
    strings get case-folded so 'US' == 'us' == 'Us'."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().casefold()
    return v


def _eq(a: Any, b: Any) -> bool:
    return _norm_scalar(a) == _norm_scalar(b)


def _is_undetermined(v: Any) -> bool:
    """A referenced field is undetermined when the extraction gave us
    nothing to compare against. Same predicate as the UI's Why-chip
    logic so backend + frontend agree on what "missing" means."""
    return v is None or v == "" or v == []


def _repr(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f"'{v}'"
    return str(v)


# ---------------------------------------------------------------------------
# Subclause matchers. Each returns (matched, step) or None if the pattern
# didn't apply. The evaluator tries them in order and takes the first hit.
# ---------------------------------------------------------------------------

# @field <op> literal   ops: =, is, ==, !=, <=, >=, <, >, ≤, ≥
_OP_RE = re.compile(
    r"^@([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(<=|>=|!=|=|<|>|≤|≥|is not|is)\s*"
    r"(.+?)\s*$",
    re.IGNORECASE,
)

# @field ∈ {a, b, c}    (or `in {…}`)
_SET_RE = re.compile(
    r"^@([A-Za-z_][A-Za-z0-9_]*)\s*(∈|in)\s*\{([^{}]*)\}\s*$",
    re.IGNORECASE,
)

# @field is a N-digit number  — regex check on the raw value string. Common
# in rules that assert identifier shapes (NPI = 10 digits, DEA = 9 chars,
# SSN last-4 = 4 digits). Whitespace/dashes in the actual value are
# stripped before counting so "555-12-3456" doesn't false-fail.
_DIGIT_RE = re.compile(
    r"^@([A-Za-z_][A-Za-z0-9_]*)\s+is\s+a\s+(\d+)-digit\s+number\s*$",
    re.IGNORECASE,
)

# @field is within the last N (day|week|month|year)s of today
# Date recency check — parses the actual value as a date and compares
# against today - N units. Common in rules that gate on "recent
# prescription" or "denial letter within last 6 months".
_WITHIN_RE = re.compile(
    r"^@([A-Za-z_][A-Za-z0-9_]*)\s+is\s+within\s+the\s+last\s+"
    r"(\d+)\s+(day|week|month|year)s?\s+of\s+today\s*$",
    re.IGNORECASE,
)


def _eval_op_clause(m: re.Match[str], values: dict[str, Any]) -> tuple[bool | None, Step] | None:
    field_name, op, rhs = m.group(1), m.group(2).strip().lower(), m.group(3).strip()
    # Reject rhs that itself contains something too rich (arithmetic,
    # function calls, another @-field reference we can't resolve
    # in the value dict). Better to punt than to answer wrongly.
    if any(sym in rhs for sym in ("%", "×", "*", "/", "(", ")")):
        return None
    # Reject prose RHS ("a 10-digit number", "within the last 12 months").
    # A well-formed literal is a bool, number, quoted string, or a single
    # bare identifier. Anything else (multi-word unquoted, article-led)
    # is prose we can't evaluate — punt to the LLM.
    is_quoted = (rhs.startswith("'") and rhs.endswith("'")) or (
        rhs.startswith('"') and rhs.endswith('"')
    )
    if not is_quoted and " " in rhs:
        return None
    lit = _parse_literal(rhs)
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return (
            None,
            Step(
                text=f"@{field_name} {op} {_repr(lit)} → undetermined (no value extracted)",
                result=None,
            ),
        )
    ok: bool
    try:
        if op in ("=", "==", "is"):
            ok = _eq(actual, lit)
        elif op in ("!=", "is not"):
            ok = not _eq(actual, lit)
        elif op in ("<", ):
            ok = float(actual) < float(lit)
        elif op in (">",):
            ok = float(actual) > float(lit)
        elif op in ("<=", "≤"):
            ok = float(actual) <= float(lit)
        elif op in (">=", "≥"):
            ok = float(actual) >= float(lit)
        else:
            return None
    except (TypeError, ValueError):
        # Numeric compare against a non-numeric actual — unparseable
        # (rule shape doesn't fit the values). Better to punt.
        return None
    verdict_word = "matched" if ok else "did not match"
    return (
        ok,
        Step(
            text=f"@{field_name} {op} {_repr(lit)} → {verdict_word} (actual: {_repr(actual)})",
            result=ok,
        ),
    )


def _eval_set_clause(m: re.Match[str], values: dict[str, Any]) -> tuple[bool | None, Step] | None:
    field_name = m.group(1)
    raw_items = m.group(3)
    # Reject sets whose entries carry nested conditions (`commercial with
    # @has_denial_letter=true`) — evaluating partial matches would be
    # misleading. Punt to LLM.
    if "@" in raw_items or " with " in raw_items.lower() or "(" in raw_items:
        return None
    items = [i.strip() for i in raw_items.split(",") if i.strip()]
    if not items:
        return None
    lits = [_parse_literal(i) for i in items]
    actual = values.get(field_name)
    lits_repr = ", ".join(_repr(lit) for lit in lits)
    if _is_undetermined(actual):
        return (
            None,
            Step(
                text=(
                    f"@{field_name} ∈ {{{lits_repr}}} "
                    "→ undetermined (no value extracted)"
                ),
                result=None,
            ),
        )
    ok = any(_eq(actual, lit) for lit in lits)
    verdict_word = "matched" if ok else "did not match"
    return (
        ok,
        Step(
            text=(
                f"@{field_name} ∈ {{{lits_repr}}} "
                f"→ {verdict_word} (actual: {_repr(actual)})"
            ),
            result=ok,
        ),
    )


# Date parser: accepts the shapes the extractor actually emits — ISO
# (YYYY-MM-DD) and US slash (M/D/YYYY or MM/DD/YYYY). Returns a date or
# None if parsing fails. `date`s and `datetime`s pass through unchanged.
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


# Approximate day counts per unit — good enough for "within the last N
# months" recency checks; real-world PAP forms don't need calendar-exact
# semantics ("was this Rx written in the last 12 months?" is a fuzzy
# question). A day = 1, week = 7, month = 30, year = 365.
_UNIT_TO_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _eval_digit_clause(m: re.Match[str], values: dict[str, Any]) -> tuple[bool | None, Step] | None:
    field_name = m.group(1)
    n = int(m.group(2))
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return (
            None,
            Step(
                text=f"@{field_name} is a {n}-digit number → undetermined (no value extracted)",
                result=None,
            ),
        )
    # "N-digit number" = the source is only digits + optional
    # separator noise (dashes/spaces), and the digit projection is
    # exactly N chars long. So "3167245083" (10 digits) matches
    # "10-digit number"; "555-12-3456" (9 digits) matches "9-digit"
    # even though the string is 11 chars.
    src = str(actual)
    digits_only = re.sub(r"\D", "", src)
    only_digits_and_seps = bool(re.fullmatch(r"[\d\s\-]+", src))
    ok = only_digits_and_seps and len(digits_only) == n
    verdict_word = "matched" if ok else "did not match"
    return (
        ok,
        Step(
            text=(
                f"@{field_name} is a {n}-digit number → {verdict_word} "
                f"(actual: {_repr(actual)}, {len(digits_only)} digit(s))"
            ),
            result=ok,
        ),
    )


def _eval_within_clause(m: re.Match[str], values: dict[str, Any]) -> tuple[bool | None, Step] | None:
    field_name = m.group(1)
    n = int(m.group(2))
    unit = m.group(3).lower()
    actual = values.get(field_name)
    if _is_undetermined(actual):
        return (
            None,
            Step(
                text=(
                    f"@{field_name} is within the last {n} {unit}(s) of today "
                    "→ undetermined (no value extracted)"
                ),
                result=None,
            ),
        )
    parsed = _parse_date_value(actual)
    if parsed is None:
        # Actual value isn't parseable as a date — the rule shape doesn't
        # fit the data; punt so the LLM's opinion isn't overridden.
        return None
    today = date.today()
    window_days = n * _UNIT_TO_DAYS[unit]
    delta_days = (today - parsed).days
    # "Within the last N units" = the parsed date is between today and
    # (today - window_days), inclusive on both ends. Future-dated
    # values count as "not within the last window" — the check is
    # backwards-looking.
    ok = 0 <= delta_days <= window_days
    verdict_word = "matched" if ok else "did not match"
    return (
        ok,
        Step(
            text=(
                f"@{field_name} is within the last {n} {unit}(s) of today "
                f"→ {verdict_word} (actual: {parsed.isoformat()}, "
                f"{delta_days} day(s) ago; window {window_days} day(s))"
            ),
            result=ok,
        ),
    )


def _eval_clause(text: str, values: dict[str, Any]) -> tuple[bool | None, Step] | None:
    """Return (result, step) for one subclause. None means the shape
    isn't recognised — caller treats the whole rule as unparseable.

    Ordered try: specific patterns first (set-membership, N-digit,
    date-recency) then the general `@field <op> literal`. Otherwise
    `_OP_RE` would greedily match `is a 10-digit number` and reject
    the RHS as prose.
    """
    t = text.strip().rstrip(",.").strip()
    if not t:
        return None
    m = _SET_RE.match(t)
    if m:
        return _eval_set_clause(m, values)
    m = _DIGIT_RE.match(t)
    if m:
        return _eval_digit_clause(m, values)
    m = _WITHIN_RE.match(t)
    if m:
        return _eval_within_clause(m, values)
    m = _OP_RE.match(t)
    if m:
        return _eval_op_clause(m, values)
    return None


# ---------------------------------------------------------------------------
# Expression: single-level AND or OR chain. Mixed connectives are punted
# to unparseable (avoids implicit precedence bugs; if a rule really needs
# both, it should be split into two rules).
# ---------------------------------------------------------------------------

_AND_SPLIT = re.compile(r"\s+AND\s+", re.IGNORECASE)
_OR_SPLIT = re.compile(r"\s+OR\s+", re.IGNORECASE)


def _split_by(op_re: re.Pattern[str], text: str) -> list[str]:
    return [p.strip() for p in op_re.split(text) if p.strip()]


def _combine(parts_results: list[bool | None], op: str) -> bool | None:
    """Combine subclause results. AND: any False → False; any None → None
    (only when no False beats it); all True → True. OR: any True → True;
    any None → None (only when no True beats it); all False → False."""
    if op == "AND":
        if any(r is False for r in parts_results):
            return False
        if any(r is None for r in parts_results):
            return None
        return True
    # OR
    if any(r is True for r in parts_results):
        return True
    if any(r is None for r in parts_results):
        return None
    return False


def evaluate(criterion: str, values: dict[str, Any]) -> Evaluation:
    """Parse the criterion and evaluate it against the values dict.

    ``values`` is the merged intake + extras dict (same shape the reason
    stage was given). Returns an Evaluation with verdict, per-subclause
    trace, and a one-sentence plain-English justification.

    Never raises — a criterion this module can't parse simply lands as
    ``verdict="unparseable"`` with an empty steps list. Callers should
    trust the LLM's answer when they see that.
    """
    if not criterion or not criterion.strip():
        return Evaluation(verdict="unparseable", justification="")

    core = _strip_wrappers(criterion)

    # Reject anything with mixed AND/OR — precedence is ambiguous
    # without parens, and rules that need both should be split.
    has_and = bool(_AND_SPLIT.search(core))
    has_or = bool(_OR_SPLIT.search(core))
    if has_and and has_or:
        return Evaluation(verdict="unparseable")

    op = "AND" if has_and else ("OR" if has_or else "SINGLE")
    parts = (
        _split_by(_AND_SPLIT, core)
        if has_and
        else (_split_by(_OR_SPLIT, core) if has_or else [core])
    )

    steps: list[Step] = []
    results: list[bool | None] = []
    for p in parts:
        parsed = _eval_clause(p, values)
        if parsed is None:
            # Whole rule punts — LLM answer stands.
            return Evaluation(verdict="unparseable")
        result, step = parsed
        steps.append(step)
        results.append(result)

    if op == "SINGLE":
        combined = results[0]
    else:
        combined = _combine(results, op)

    verdict: Verdict
    if combined is True:
        verdict = "matched"
    elif combined is False:
        verdict = "unmatched"
    else:
        verdict = "undetermined"

    # Single sentence for the UI. Reads: "matched: @X = 'foo' and @Y = true"
    # or "not matched: @X = 'foo' but @Y = 'bar' (actual: 'baz')".
    if verdict == "matched":
        headline = "matched — all conditions satisfied."
    elif verdict == "unmatched":
        headline = "not matched — at least one condition failed."
    else:
        headline = "undetermined — required data is missing."
    trace_summary = " ".join(s.text for s in steps)
    justification = f"{headline} {trace_summary}".strip()

    return Evaluation(verdict=verdict, steps=steps, justification=justification)


__all__ = ["Evaluation", "Step", "Verdict", "evaluate"]
