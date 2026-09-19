"""Rule text uses ``@fieldname`` tokens to reference program schema slots.

The rule vocabulary is program-scoped: every ``@identifier`` in a rule's
criterion or exception text must resolve to a slot declared on the
attached program's ``extraction_schema``. This module is the single
source of truth for parsing those references and validating them against
a slot list — used by:

* Rule authoring (save handler validates before persist)
* Copy-to-program flow (source rule's refs → target program's slots)
* Reasoner prompt-builder (pulls the slot list from the already-cached
  ``rules.field_refs`` column instead of re-parsing on every case)

Grammar (intentionally minimal):

* ``@`` followed by an ASCII identifier: ``[A-Za-z_][A-Za-z0-9_]*``
* Case-sensitive (slot names are case-sensitive in the schema)
* Not escaped inside strings — a stray ``@`` in prose (email addresses,
  Slack handles) is treated as a reference. Rule authors get a validation
  error which is a small price for a simple parser.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# One canonical regex. Compiled at import time so hot paths (reasoner
# prompt-build) don't re-compile every call.
_FIELD_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")


def parse_field_refs(*texts: str) -> list[str]:
    """Extract every ``@fieldname`` token from one or more text blocks.

    Deduplicates but preserves first-seen order — the caller usually
    passes ``criterion`` then ``exception`` so the criterion's tokens
    come first, matching how a reader scans the rule top-to-bottom.
    Empty / None inputs are ignored so callers can pass raw column
    values without pre-filtering.
    """
    seen: dict[str, None] = {}
    for t in texts:
        if not t:
            continue
        for m in _FIELD_REF.finditer(t):
            seen.setdefault(m.group(1), None)
    return list(seen.keys())


@dataclass(frozen=True)
class ValidationError:
    """One unknown ``@field`` in the rule text with a suggested fix.

    ``suggestion`` is None when no similarly-named slot exists on the
    target schema — the operator has to hand-map. Otherwise it's the
    closest-matching declared slot name (case-insensitive prefix +
    Levenshtein), suitable for a "did you mean?" hint.
    """

    field: str
    suggestion: str | None


def validate_field_refs(
    refs: Iterable[str],
    slot_names: Iterable[str],
) -> list[ValidationError]:
    """Return an error per referenced field that's missing from the schema.

    Empty list = every reference resolves. This is the write-path gate
    for rule save: reject the payload if any errors come back so the
    reasoner never sees a rule that names a slot which won't be in the
    intake dict.
    """
    slot_list = list(slot_names)
    slot_set = set(slot_list)
    errors: list[ValidationError] = []
    for ref in refs:
        if ref in slot_set:
            continue
        errors.append(
            ValidationError(field=ref, suggestion=_suggest(ref, slot_list))
        )
    return errors


def _suggest(needle: str, haystack: list[str]) -> str | None:
    """Cheap "did-you-mean" — case-insensitive substring match first,
    then a Levenshtein-distance fallback. Returns the single best hit
    or None if nothing is remotely close.

    Deliberately not fancy: this is a hint on a validation error, not
    the copy-to-program auto-mapper (which lives in
    ``pap_pipeline.rule_mapping`` and does the three-pass matching).
    """
    n = needle.lower()
    # Substring hit — e.g., "ins" matches "ins_uninsured"
    subs = [s for s in haystack if n in s.lower() or s.lower() in n]
    if subs:
        return min(subs, key=lambda s: abs(len(s) - len(needle)))
    # Fallback: Levenshtein-lite. Distance capped so unrelated names
    # don't get suggested.
    if not haystack:
        return None
    best = min(haystack, key=lambda s: _levenshtein(n, s.lower()))
    if _levenshtein(n, best.lower()) <= max(2, len(needle) // 3):
        return best
    return None


def _levenshtein(a: str, b: str) -> int:
    """Small Levenshtein distance implementation. Fine for slot names
    which are short (< 40 chars) — no need for optimized libraries."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(min(
                curr[j - 1] + 1,          # insertion
                prev[j] + 1,              # deletion
                prev[j - 1] + cost,       # substitution
            ))
        prev = curr
    return prev[-1]


def substitute_refs(text: str, mapping: dict[str, str]) -> str:
    """Rewrite every ``@old`` in ``text`` to ``@new`` per ``mapping``.

    Refs not in the mapping are left untouched — the caller's job is to
    ensure the mapping is complete before calling (typically after the
    copy-to-program wizard's auto-map + user review). Rewriting is done
    with a single regex pass so overlapping / adjacent tokens don't
    interfere with each other.
    """
    if not mapping or not text:
        return text
    def _replace(m: re.Match[str]) -> str:
        name = m.group(1)
        target = mapping.get(name)
        return f"@{target}" if target else m.group(0)
    return _FIELD_REF.sub(_replace, text)
