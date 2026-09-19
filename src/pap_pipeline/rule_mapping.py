"""Copy-a-library-rule-to-a-program auto-mapping.

When an operator copies a library rule into a program's vocabulary, each
``@fieldname`` in the source rule needs to be rewritten to a slot name
declared on the target program. This module runs the three-pass matcher
that produces the "best guess" mapping the wizard shows to the operator.

Three passes, cheapest first, so the operator sees an exact match where
one exists and only reviews the ambiguous rows:

1. **Exact** — source ref name == target slot name. Confidence 1.0.
2. **Normalized fuzzy** — strip common form-vocabulary prefixes/suffixes,
   compare on the reduced tokens with Levenshtein + token overlap.
   Confidence 0.5–0.95 depending on similarity.
3. **Unresolved** — no automatic match found. The wizard forces the
   operator to pick one from the schema before save.

The wizard displays confidence + the resolution mode so the operator
knows which rows deserve a second look. Every row is editable regardless
of the auto-suggestion — the algorithm is a hint, not a decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pap_pipeline.rule_refs import levenshtein

# Ranked so the operator sees exact matches first, then fuzzy hints, then
# rows that need a decision. Kept as a plain string literal instead of an
# Enum so JSON serialization in the wizard route is trivial.
Confidence = Literal["exact", "fuzzy", "unresolved"]

# Form-vocabulary prefixes that we strip before fuzzy comparison — this
# is what makes "patient_zip" match "postal_code" and "ins_uninsured"
# match "insurance_status". Ordered longest-first so the greedy prefix
# strip does the right thing on collisions.
_STRIP_PREFIXES = (
    "internal_", "prescriber_", "practice_", "clinical_", "patient_",
    "caregiver_", "consent_", "contact_", "insurance_", "ins_", "rx_",
    "dx_", "svc_", "ship_", "req_",
)
_STRIP_SUFFIXES = ("_yes", "_no", "_id")

# Curated alias buckets — pure token-overlap can't bridge names like
# `postal_code` ↔ `patient_zip` because they share zero tokens. This
# table encodes the well-known Intake ↔ form-native mappings so the
# wizard suggests a defensible target even when the string similarity
# is zero. Keys and values are all case-sensitive and normalized to
# lowercase for lookup.
#
# Bucket semantics: two names are considered "aliases" if they appear
# in the same bucket. Adding a bucket for a new concept is a one-line
# change and doesn't require code touching the scoring path.
_ALIAS_BUCKETS: tuple[frozenset[str], ...] = (
    # Postal / ZIP
    frozenset({"postal_code", "patient_zip", "zip", "zipcode", "zip_code"}),
    # State
    frozenset({"mailing_state", "patient_state", "state"}),
    # City
    frozenset({"city", "patient_city"}),
    # DOB
    frozenset({"date_of_birth", "patient_dob", "dob"}),
    # Full patient name
    frozenset({"patient_name"}),
    # Family / last name
    frozenset({"family_name", "patient_last_name", "last_name", "surname"}),
    # First name (not in canonical Intake but common on forms)
    frozenset({"patient_first_name", "first_name", "given_name"}),
    # Insurance enum ↔ form booleans
    frozenset({
        "insurance_status", "ins_uninsured", "ins_medicare_partd",
        "ins_primary_payer", "insurance_type",
    }),
    # Prescribed drug
    frozenset({"prescribed_drug", "rx_medication", "drug", "medication"}),
    # Prescription date
    frozenset({"prescription_date", "rx_date_written", "rx_date"}),
    # Prescriber NPI (already exact in Alderwood; kept for other forms)
    frozenset({"prescriber_npi", "npi"}),
    # Referral / application ID
    frozenset({"referral_id", "internal_referral_id", "application_id", "reference_id"}),
    # Address lines
    frozenset({"address_line1", "patient_address_line1", "street_address"}),
    frozenset({"address_line2", "patient_address_line2", "apt", "unit"}),
    # Household
    frozenset({"household_size", "family_size"}),
    frozenset({"annual_household_income", "annual_income", "household_income"}),
    # Residency
    frozenset({"country_of_residence", "country", "patient_country"}),
    # Denial letter marker
    frozenset({"has_denial_letter", "denial_letter"}),
)


def _alias_score(a: str, b: str) -> float:
    """Return 0.85 if `a` and `b` share a curated alias bucket, else 0.

    The confidence is deliberately below 1.0 (which is reserved for exact
    string match) so the UI still shows "fuzzy" and prompts the operator
    to eyeball the row — an alias hit is a strong hint, not proof.
    """
    a_lc, b_lc = a.lower(), b.lower()
    if a_lc == b_lc:
        return 1.0
    for bucket in _ALIAS_BUCKETS:
        if a_lc in bucket and b_lc in bucket:
            return 0.85
    return 0.0


@dataclass(frozen=True)
class Match:
    """One row on the wizard: the source ref + the auto-suggested target,
    plus a confidence label so the UI can decorate the row (green for
    exact, amber for fuzzy, red for unresolved)."""

    source: str
    target: str | None
    confidence: Confidence
    score: float  # 0.0–1.0; useful for sorting and tests


def auto_map(
    source_refs: list[str],
    target_slots: list[str],
) -> list[Match]:
    """Run the three-pass matcher over every source reference.

    Returns one :class:`Match` per source ref in the input order. The
    caller (wizard route) can then serialize this into the review table
    with pre-filled dropdowns.

    Deliberately does no LLM call. That's Phase D — when the fuzzy pass
    misses AND the operator has a strong opinion, an LLM suggestion can
    be added as a fourth pass without changing this signature.
    """
    slot_set = set(target_slots)
    matches: list[Match] = []
    for ref in source_refs:
        if ref in slot_set:
            matches.append(Match(source=ref, target=ref, confidence="exact", score=1.0))
            continue
        target, score = _fuzzy(ref, target_slots)
        if target is not None:
            matches.append(Match(source=ref, target=target, confidence="fuzzy", score=score))
        else:
            matches.append(Match(source=ref, target=None, confidence="unresolved", score=0.0))
    return matches


def _fuzzy(source: str, targets: list[str]) -> tuple[str | None, float]:
    """Return the best fuzzy match + score in [0, 1], or (None, 0) when
    no candidate clears the confidence floor.

    Scoring blends three signals:
      * Token-overlap on the stripped names (dominant for semantic matches
        like "postal_code" vs "patient_zip" where the meaningful tokens
        are "postal"/"zip" and "code")
      * Length-normalized Levenshtein (secondary; helps for typo matches
        like "insurance_status" vs "insurance_stauts")
      * Substring hit (bonus; "ins_uninsured" contains the concept name
        "uninsured" and "insurance_status" contains "insurance")
    """
    if not targets:
        return None, 0.0
    src_norm = _strip(source).lower()
    src_tokens = set(_tokens(src_norm))
    best: tuple[str | None, float] = (None, 0.0)
    for t in targets:
        # Curated alias hit is the strongest fuzzy signal — bridges
        # semantically-different names like postal_code ↔ patient_zip
        # that share no tokens and have high Levenshtein distance.
        alias = _alias_score(source, t)
        if alias > 0:
            if alias > best[1]:
                best = (t, alias)
            continue
        tgt_norm = _strip(t).lower()
        tgt_tokens = set(_tokens(tgt_norm))
        if not tgt_tokens:
            continue
        overlap = (
            len(src_tokens & tgt_tokens) / max(len(src_tokens | tgt_tokens), 1)
        )
        lev = levenshtein(src_norm, tgt_norm)
        lev_score = 1.0 - (lev / max(len(src_norm), len(tgt_norm), 1))
        bonus = 0.15 if (src_norm and (src_norm in tgt_norm or tgt_norm in src_norm)) else 0.0
        score = 0.6 * overlap + 0.3 * max(lev_score, 0.0) + bonus
        if score > best[1]:
            best = (t, score)
    # Confidence floor: below this we'd rather flag as unresolved than
    # confidently mis-suggest. 0.45 empirically catches the useful pairs
    # (postal_code↔patient_zip, insurance_status↔ins_uninsured) without
    # matching totally unrelated slots.
    if best[1] < 0.45:
        return None, 0.0
    return best


def _strip(name: str) -> str:
    """Remove common form-vocabulary prefixes/suffixes so fuzzy matching
    compares the meaningful part of the name.

    ``patient_zip`` → ``zip``,
    ``ins_uninsured`` → ``uninsured``,
    ``rx_date_written`` → ``date_written``.
    """
    n = name
    for pre in _STRIP_PREFIXES:
        if n.startswith(pre):
            n = n[len(pre):]
            break  # greedy: don't strip twice
    for suf in _STRIP_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n


def _tokens(name: str) -> list[str]:
    """Split on underscores + camelCase-ish boundaries. All-lowercase
    tokens for comparison. Empty parts dropped."""
    return [p for p in name.replace("-", "_").split("_") if p]
