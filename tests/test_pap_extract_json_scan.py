"""Edge-case coverage for the balanced-brace JSON extractor.

``_extract_first_json_object`` in :mod:`pap_pipeline.stages.extract` is
a hand-rolled scanner that unwraps the reasoning-mode VLM's
``<think>{...}</think>`` preamble from the real JSON answer. It's the
kind of small parser that's easy to write and easy to get wrong at
the corners — nested braces, escaped quotes, JSON inside strings, no
object at all. This file locks its contract.
"""
from __future__ import annotations

from pap_pipeline.stages.extract import (
    _extract_first_json_object,
    _try_parse_json_object,
)


def test_bare_object_parses():
    """Happy path: a single well-formed JSON object at column 0."""
    assert _extract_first_json_object('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


def test_unwraps_reasoning_preamble():
    """VLM reasoning mode prepends `<think>...</think>` before the real
    JSON. The scanner must find the outermost balanced object AFTER the
    preamble text — a naive `text.find('{')` picks the `<think>` prose
    content, and a greedy regex would splice think + answer."""
    text = (
        "<think>Let me consider. The patient looks 45. </think>\n"
        '{"patient_name": "Jane Doe", "age": 45}'
    )
    assert _extract_first_json_object(text) == {"patient_name": "Jane Doe", "age": 45}


def test_braces_inside_string_do_not_confuse_depth():
    """A `{` or `}` inside a JSON string literal must not bump the depth
    counter; the outer object closes only at the true matching brace."""
    text = '{"note": "value with { and } in it", "ok": true}'
    assert _extract_first_json_object(text) == {
        "note": "value with { and } in it",
        "ok": True,
    }


def test_escaped_quote_stays_inside_string():
    r"""An escaped `\"` in a JSON string must keep the parser in
    string-mode; otherwise the character after `\"` would be
    interpreted as JSON structure and the object would appear
    unbalanced."""
    text = '{"escaped": "she said \\"hi\\"", "n": 1}'
    parsed = _extract_first_json_object(text)
    assert parsed is not None
    assert parsed == {"escaped": 'she said "hi"', "n": 1}


def test_first_valid_object_wins_over_later_ones():
    """When the text contains multiple valid objects, the scanner returns
    the FIRST one that parses — matches the module docstring's contract
    and the reasoning-preamble use case (where the preamble may itself
    be a JSON-shaped hint)."""
    text = '{"first": true} then {"second": true}'
    assert _extract_first_json_object(text) == {"first": True}


def test_broken_first_object_falls_through_to_valid_second():
    """A `{...}` slice that closes syntactically but doesn't parse (bad
    JSON inside) shouldn't stop the scan — the parser restarts from the
    next `{` and returns the first candidate that does parse."""
    # First object has unquoted key → json.loads rejects it.
    text = '{unquoted_key: 1} {"good": 42}'
    assert _extract_first_json_object(text) == {"good": 42}


def test_returns_none_when_no_object_present():
    assert _extract_first_json_object("") is None
    assert _extract_first_json_object("   \n\t ") is None
    assert _extract_first_json_object("just prose, no braces here") is None
    assert _extract_first_json_object("[not, an, object]") is None


def test_returns_none_when_object_never_closes():
    """A truncated response (`{"a": 1, "b":`) has an unclosed brace and
    no valid parse anywhere — must return None, not raise."""
    assert _extract_first_json_object('{"a": 1, "b":') is None


def test_finds_dict_inside_array_wrapper():
    """The scanner walks left-to-right and returns the first BALANCED
    dict slice — a dict nested inside an array wrapper is still found.
    This is a documented contract (see the module docstring): the caller
    wants a dict, and the scanner returns the first one it can parse,
    even if the caller's outer wrapper isn't itself a dict."""
    assert _extract_first_json_object('[{"a": 1}]') == {"a": 1}


def test_returns_none_when_only_array_present():
    """When there's no dict anywhere in the text — only arrays — the
    scanner correctly returns None."""
    assert _extract_first_json_object('[1, 2, 3]') is None
    assert _extract_first_json_object('[[[]]]') is None


def test_nested_objects_return_outermost():
    """A well-formed object containing sub-objects should parse as the
    outermost dict, not the first inner one — depth-tracking must find
    the matching outer `}`, not the first inner one."""
    text = '{"outer": true, "inner": {"a": 1, "b": {"c": 2}}}'
    parsed = _extract_first_json_object(text)
    assert parsed is not None
    assert parsed["outer"] is True
    assert parsed["inner"] == {"a": 1, "b": {"c": 2}}


def test_try_parse_json_object_rejects_non_dict():
    """Direct-parse helper must return None for arrays, numbers, strings
    — anything that isn't a dict. Downstream callers rely on the
    dict-only guarantee."""
    assert _try_parse_json_object("null") is None
    assert _try_parse_json_object("42") is None
    assert _try_parse_json_object('"just a string"') is None
    assert _try_parse_json_object('[1, 2, 3]') is None
    assert _try_parse_json_object('{"ok": true}') == {"ok": True}


def test_try_parse_json_object_rejects_malformed():
    """Non-JSON input returns None rather than raising."""
    assert _try_parse_json_object("") is None
    assert _try_parse_json_object("{not: json}") is None
    assert _try_parse_json_object("<think>...</think>") is None
