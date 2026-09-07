from __future__ import annotations

from jinja2 import Environment

from docgen.core.schema_infer import FieldSpec
from docgen.designtime.mapper import _backfill_orphan_suggestions, _infer_variable_types
from docgen.providers.base import MappingProposal, Suggestion


def _mk(jinja: str, name: str, source: str = "ai") -> Suggestion:
    return Suggestion(
        placeholder=jinja,
        jinja=jinja,
        field=FieldSpec(name=name, type="string", required=True),
        source=source,
    )


def test_backfill_appends_missed_variables() -> None:
    text = "Hello {{ first_name }}, income is {{ Annual_Income__c }}."
    proposal = MappingProposal(suggestions=[_mk("{{ first_name }}", "first_name")])

    _backfill_orphan_suggestions(proposal, text)

    names = [s.field.name for s in proposal.suggestions]
    assert names == ["first_name", "Annual_Income__c"]
    backfill = proposal.suggestions[-1]
    assert backfill.source == "orphan_backfill"
    assert backfill.placeholder == backfill.jinja == "{{ Annual_Income__c }}"


def test_backfill_noop_when_all_covered() -> None:
    text = "{{ a }} {{ b }}"
    proposal = MappingProposal(
        suggestions=[_mk("{{ a }}", "a"), _mk("{{ b }}", "b")]
    )

    _backfill_orphan_suggestions(proposal, text)

    assert [s.field.name for s in proposal.suggestions] == ["a", "b"]
    assert all(s.source == "ai" for s in proposal.suggestions)


def test_backfill_recognizes_dotted_attribute_chains() -> None:
    """AI's {{ contact.name }} covers preflight-detected var 'contact'."""
    text = "{{ contact.name }} {{ orphan_var }}"
    proposal = MappingProposal(suggestions=[_mk("{{ contact.name }}", "contact")])

    _backfill_orphan_suggestions(proposal, text)

    names = [s.field.name for s in proposal.suggestions]
    assert names == ["contact", "orphan_var"]


def test_backfill_handles_empty_template() -> None:
    proposal = MappingProposal(suggestions=[])
    _backfill_orphan_suggestions(proposal, "no jinja here")
    assert proposal.suggestions == []


def test_backfill_survives_bad_suggestion_jinja() -> None:
    """A malformed jinja in an AI suggestion mustn't crash the backfill pass."""
    text = "{{ good }} {{ other }}"
    bad = Suggestion(
        placeholder="x", jinja="{{ unclosed", field=FieldSpec(name="x"), source="ai"
    )
    proposal = MappingProposal(suggestions=[bad])

    _backfill_orphan_suggestions(proposal, text)

    names = sorted(s.field.name for s in proposal.suggestions)
    assert names == ["good", "other", "x"]


# ---------------------------------------------------------------------------
# Type inference from Jinja AST usage
# ---------------------------------------------------------------------------


def _parse(text: str):
    return Environment().parse(text)


def test_infer_boolean_for_if_only_var() -> None:
    ast = _parse("{% if Proof_Pay_Stubs__c %}checked{% endif %}")
    assert _infer_variable_types(ast, ["Proof_Pay_Stubs__c"]) == {
        "Proof_Pay_Stubs__c": "boolean"
    }


def test_infer_string_when_also_displayed() -> None:
    """Displayed AND if-checked → keep as string; user is rendering the value."""
    ast = _parse("{% if status %}{{ status }}{% endif %}")
    assert _infer_variable_types(ast, ["status"]) == {}  # default (string) not returned


def test_infer_list_for_for_iter() -> None:
    ast = _parse("{% for item in items %}{{ item }}{% endfor %}")
    assert _infer_variable_types(ast, ["items"]) == {"items": "list"}


def test_infer_backfill_defaults_boolean_for_checkbox_pattern() -> None:
    """End-to-end: backfilled row for {% if X %}-only var comes out as boolean."""
    text = (
        "{% if Proof_Other__c %}[X]{% else %}[ ]{% endif %} Other\n"
        "{% if Proof_Pay_Stubs__c %}[X]{% else %}[ ]{% endif %} Pay stubs"
    )
    proposal = MappingProposal(suggestions=[])
    _backfill_orphan_suggestions(proposal, text)

    by_name = {s.field.name: s.field.type for s in proposal.suggestions}
    assert by_name == {"Proof_Other__c": "boolean", "Proof_Pay_Stubs__c": "boolean"}
    assert all(s.source == "orphan_backfill" for s in proposal.suggestions)


def test_infer_mixed_usage_stays_string() -> None:
    text = "{% for x in xs %}{{ x }}{% endfor %} Total: {{ xs_total }}"
    proposal = MappingProposal(suggestions=[])
    _backfill_orphan_suggestions(proposal, text)
    by_name = {s.field.name: s.field.type for s in proposal.suggestions}
    # xs is only in for-iter → list; xs_total is only in output → string
    assert by_name == {"xs": "list", "xs_total": "string"}
