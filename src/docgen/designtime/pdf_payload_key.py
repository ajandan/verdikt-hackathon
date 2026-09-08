"""Shortened snake_case payload keys for PDF placements.

Python port of the JS `slugifyKey` / `shortenPayloadKey` in
`placement_editor.html`. The manual UI's "AI Label fields" button uses
those to build compact identifier-safe keys from an AI-suggested
section + label. The inbox agent's PDF ingest path calls this so
email-ingested templates get the same keys as templates labeled through
the UI — otherwise the frozen schema fields keep raw AcroForm names
like "Text Field 167" instead of "first_name".

Keep this in sync with the JS side. The pair of implementations exists
because we don't want to trigger a JS runtime on the worker just to
slug a few strings.
"""
from __future__ import annotations

import re
import unicodedata

_COMBINING_RE = re.compile(r"[\u0300-\u036f]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")
_TRIM_UNDERSCORE_RE = re.compile(r"^_+|_+$")
_LEADING_DIGIT_RE = re.compile(r"^[0-9]")
_ALL_DIGITS_RE = re.compile(r"^\d+$")

KEY_STOP_WORDS: frozenset[str] = frozenset({
    "the", "of", "to", "for", "with", "a", "an", "in", "on", "at", "by",
    "is", "are", "be", "your", "and", "or",
    "please", "print", "optional", "required", "section",
})

# Multi-token contractions applied to the joined slug before splitting.
# Order matters — longest patterns first.
KEY_MULTIWORD: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmm_dd_yyyy\b"), ""),
    (re.compile(r"\bsocial_security_number\b"), "ssn"),
    (re.compile(r"\bdate_of_birth\b"), "dob"),
    (re.compile(r"\bphone_number\b"), "phone"),
    (re.compile(r"\bzip_code\b"), "zip"),
    (re.compile(r"\bmiddle_initial\b"), "mi"),
    (re.compile(r"\bhealthcare_provider\b"), "hcp"),
]

KEY_ABBREV: dict[str, str] = {
    "information": "info",
    "authorization": "auth",
    "authorized": "auth",
    "representative": "rep",
    "medication": "med",
    "medications": "meds",
    "signature": "sig",
    "prescription": "rx",
    "prescriber": "rx",
    "provider": "prov",
    "healthcare": "hc",
    "number": "num",
    "certification": "cert",
    "agreement": "agmt",
    "insurance": "ins",
}

KEY_MAX_LEN = 48
SECTION_MAX_TOKENS = 2
SECTION_MAX_LEN = 16


def slugify_key(text: str | None) -> str:
    """Slug any human-readable text into a snake_case identifier."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = _COMBINING_RE.sub("", s).lower()
    s = _NON_ALNUM_RE.sub("_", s)
    s = _MULTI_UNDERSCORE_RE.sub("_", s)
    s = _TRIM_UNDERSCORE_RE.sub("", s)
    if not s:
        return "field"
    if _LEADING_DIGIT_RE.match(s):
        s = "f_" + s
    return s


def unique_name(base: str, taken: set[str]) -> str:
    """Return a variant of `base` not present in `taken`, appending _2, _3, ..."""
    if base not in taken:
        return base
    for i in range(2, 10000):
        cand = f"{base}_{i}"
        if cand not in taken:
            return cand
    # Fallback matches JS Date.now() intent — just needs to be unique.
    import time
    return f"{base}_{int(time.time() * 1000)}"


def _apply_contractions(slug: str) -> str:
    s = slug
    for pat, repl in KEY_MULTIWORD:
        s = pat.sub(repl, s)
    s = _MULTI_UNDERSCORE_RE.sub("_", s)
    s = _TRIM_UNDERSCORE_RE.sub("", s)
    return s


def _dedupe_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def shorten_payload_key(
    section_slug: str, label_slug: str, taken: set[str]
) -> str:
    """Build a compact snake_case payload key from an AI section + label.

    Mirrors the JS `shortenPayloadKey`: contractions → abbrev → drop
    stop words → dedupe tokens within/across section+label → cap length.
    """
    section_slug = _apply_contractions(section_slug or "")
    label_slug = _apply_contractions(label_slug or "")

    section_tokens = _dedupe_tokens([
        KEY_ABBREV.get(t, t)
        for t in section_slug.split("_")
        if t and KEY_ABBREV.get(t, t) not in KEY_STOP_WORDS
    ])
    section_tokens = section_tokens[:SECTION_MAX_TOKENS]
    while len(section_tokens) > 1 and len("_".join(section_tokens)) > SECTION_MAX_LEN:
        section_tokens.pop()

    label_tokens = _dedupe_tokens([
        KEY_ABBREV.get(t, t)
        for t in label_slug.split("_")
        if t and KEY_ABBREV.get(t, t) not in KEY_STOP_WORDS
    ])

    section_set = set(section_tokens)
    deduped_label = [t for t in label_tokens if t not in section_set]
    final_label = deduped_label if deduped_label else label_tokens

    tokens = [*section_tokens, *final_label]
    if not tokens:
        tokens = [t for t in (label_slug or section_slug or "field").split("_") if t]

    base = "_".join(tokens)

    # Length cap: prefer dropping short middle tokens over head/tail.
    if len(base) > KEY_MAX_LEN and len(tokens) > 3:
        kept = [
            t for i, t in enumerate(tokens)
            if i == 0
            or i == len(tokens) - 1
            or len(t) > 3
            or _ALL_DIGITS_RE.match(t)
        ]
        if len(kept) >= 2:
            base = "_".join(kept)

    if len(base) > KEY_MAX_LEN:
        base = base[:KEY_MAX_LEN]
        base = _TRIM_UNDERSCORE_RE.sub("", base)

    if not base:
        base = "field"
    if _LEADING_DIGIT_RE.match(base):
        base = "f_" + base

    return unique_name(base, taken)
