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
    re.compile(r",?\s*deny\.?\s*$", re.IGNORECASE),
    re.compile(r",?\s*approve[^.]*\.?\s*$", re.IGNORECASE),
    re.compile(r",?\s*matched?\.?\s*$", re.IGNORECASE),
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
    if _is_undetermined(actual):
        return (
            None,
            Step(
                text=f"@{field_name} ∈ {{{', '.join(_repr(l) for l in lits)}}} → undetermined (no value extracted)",
                result=None,
            ),
        )
    ok = any(_eq(actual, l) for l in lits)
    verdict_word = "matched" if ok else "did not match"
    return (
        ok,
        Step(
            text=(
                f"@{field_name} ∈ {{{', '.join(_repr(l) for l in lits)}}} "
                f"→ {verdict_word} (actual: {_repr(actual)})"
            ),
            result=ok,
        ),
    )


def _eval_clause(text: str, values: dict[str, Any]) -> tuple[bool | None, Step] | None:
    """Return (result, step) for one subclause. None means the shape
    isn't recognised — caller treats the whole rule as unparseable."""
    t = text.strip().rstrip(",.").strip()
    if not t:
        return None
    m = _SET_RE.match(t)
    if m:
        return _eval_set_clause(m, values)
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
