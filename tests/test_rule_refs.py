"""@field parser + validator — the single source of truth for rule vocab.

Every write path (rule authoring save handler, copy-to-program wizard,
reasoner prompt-builder) parses ``@fieldname`` tokens the same way. If
this behavior drifts, rules that name a slot the target program doesn't
declare silently break the pipeline. Locking it here so a refactor of
the regex or the confidence floor is loud."""
from __future__ import annotations

import pytest

from pap_pipeline.rule_refs import (
    ValidationError,
    parse_field_refs,
    substitute_refs,
    validate_field_refs,
)

# ---------------------------------------------------------------------------
# parse_field_refs — grammar edge cases
# ---------------------------------------------------------------------------

def test_parse_empty_and_none_return_empty_list():
    """Missing / empty inputs must not crash — writers pass raw column
    values which are frequently blank strings."""
    assert parse_field_refs("") == []
    assert parse_field_refs(None) == []  # type: ignore[arg-type]
    assert parse_field_refs("", "") == []


def test_parse_single_reference_in_criterion():
    assert parse_field_refs("@insurance_status is uninsured") == ["insurance_status"]


def test_parse_multiple_dedupes_preserves_first_seen_order():
    """Criterion → exception scan order matters for the UI so operators
    see refs in the order they read the rule."""
    refs = parse_field_refs(
        "@drug_name AND @npi",
        "@drug_name only when @date is recent",
    )
    assert refs == ["drug_name", "npi", "date"]


def test_parse_case_sensitive():
    """Slot names in the extraction schema are case-sensitive; the parser
    must not fold @Foo and @foo together."""
    assert parse_field_refs("@Foo and @foo") == ["Foo", "foo"]


def test_parse_ignores_bare_at_signs():
    """A stray '@' with no following identifier (e.g. email address in
    prose) is just prose, not a field reference."""
    assert parse_field_refs("email @example.com is contact") == ["example"]
    # Above: '@example' IS a valid identifier ref — email addresses in
    # rule text will get flagged. Documented tradeoff: rule authors
    # backslash-escape or rephrase.


def test_parse_underscore_and_digit_identifiers():
    assert parse_field_refs("@x1 @_hidden @foo_bar_2") == ["x1", "_hidden", "foo_bar_2"]


def test_parse_stops_at_non_identifier_chars():
    """Punctuation ends the identifier — @foo. @bar, is [foo, bar] not
    ['foo.', 'bar,']."""
    assert parse_field_refs("@foo. @bar, @baz;") == ["foo", "bar", "baz"]


# ---------------------------------------------------------------------------
# validate_field_refs — write-path gate
# ---------------------------------------------------------------------------

def test_validate_all_present_returns_no_errors():
    errors = validate_field_refs(
        ["insurance_status", "prescribed_drug"],
        ["insurance_status", "prescribed_drug", "other_slot"],
    )
    assert errors == []


def test_validate_unknown_field_returns_error_with_none_suggestion():
    """Unrelated names get no suggestion — a bad suggestion is worse
    than an honest 'no match' hint."""
    errors = validate_field_refs(["quantum_flux"], ["insurance_status", "drug"])
    assert len(errors) == 1
    assert errors[0].field == "quantum_flux"
    assert errors[0].suggestion is None


def test_validate_typo_returns_closest_match_suggestion():
    """A one- or two-char typo should get a 'did you mean' hint."""
    errors = validate_field_refs(["insurance_stauts"], ["insurance_status"])
    assert errors[0].suggestion == "insurance_status"


def test_validate_typo_may_return_suggestion_or_none():
    """A near-typo may or may not get a suggestion — the Levenshtein
    cutoff is deliberately conservative to avoid confidently-wrong hints.
    Invariant: we return a ValidationError, and if a suggestion is
    returned it must be one of the candidates (not fabricated)."""
    candidates = ["ins_uninsured", "insurance_status", "insurance_details"]
    errors = validate_field_refs(["insr"], candidates)
    assert len(errors) == 1
    if errors[0].suggestion is not None:
        assert errors[0].suggestion in candidates


def test_validate_empty_slot_list_returns_error_no_suggestion():
    """Empty haystack case — the wizard could hit this if the target
    program has no extraction_schema declared yet."""
    errors = validate_field_refs(["foo"], [])
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)
    assert errors[0].suggestion is None


# ---------------------------------------------------------------------------
# substitute_refs — the copy-to-program rewrite
# ---------------------------------------------------------------------------

def test_substitute_rewrites_mapped_refs_leaves_others_alone():
    text = "@insurance_status is uninsured AND @drug_name is Cardiovita"
    mapping = {"insurance_status": "ins_uninsured", "drug_name": "rx_medication"}
    assert substitute_refs(text, mapping) == (
        "@ins_uninsured is uninsured AND @rx_medication is Cardiovita"
    )


def test_substitute_leaves_unmapped_refs_intact():
    """Refs not in the mapping stay as-is so a partial mapping doesn't
    silently drop tokens — the validator will flag them later."""
    text = "@a and @b"
    assert substitute_refs(text, {"a": "x"}) == "@x and @b"


def test_substitute_empty_inputs_are_noops():
    assert substitute_refs("", {"a": "b"}) == ""
    assert substitute_refs("no refs here", {}) == "no refs here"


def test_substitute_handles_repeated_refs():
    """Every occurrence of @foo becomes @bar, not just the first one —
    otherwise a rule that names the same field twice would end up half-
    rewritten and unevaluable."""
    text = "@x > 5 AND @x < 100"
    assert substitute_refs(text, {"x": "value"}) == "@value > 5 AND @value < 100"


# ---------------------------------------------------------------------------
# End-to-end round-trip
# ---------------------------------------------------------------------------

def test_round_trip_parse_substitute_parse():
    """Substituting via a mapping then re-parsing gives the mapped names
    — this is what the wizard's post-substitution validator runs to
    prove the rewrite landed cleanly."""
    text = "@insurance_status is uninsured AND @drug is Cardiovita"
    refs_before = parse_field_refs(text)
    mapping = {"insurance_status": "ins_uninsured", "drug": "rx_medication"}
    new_text = substitute_refs(text, mapping)
    refs_after = parse_field_refs(new_text)
    assert refs_before == ["insurance_status", "drug"]
    assert refs_after == ["ins_uninsured", "rx_medication"]
