"""Synthetic payload generation for the inbox agent test-render step."""
from __future__ import annotations

from docgen.core.schema_infer import TemplateSchema, validate_payload
from docgen.inbox.synth import synth_payload


def test_covers_all_scalar_types():
    schema = {
        "fields": [
            {"name": "member_name", "type": "string"},
            {"name": "amount", "type": "integer"},
            {"name": "coinsurance", "type": "number"},
            {"name": "is_approved", "type": "boolean"},
            {"name": "claim_date", "type": "date"},
        ]
    }
    payload = synth_payload(schema)
    assert isinstance(payload["member_name"], str) and payload["member_name"]
    assert isinstance(payload["amount"], int)
    assert isinstance(payload["coinsurance"], float)
    assert isinstance(payload["is_approved"], bool)
    assert payload["claim_date"] == "2026-01-15"


def test_list_scalar():
    schema = {"fields": [{"name": "codes", "type": "list", "item_type": "string"}]}
    payload = synth_payload(schema)
    assert isinstance(payload["codes"], list)
    assert len(payload["codes"]) == 2
    assert all(isinstance(c, str) for c in payload["codes"])


def test_list_of_object():
    schema = {
        "fields": [
            {
                "name": "line_items",
                "type": "list",
                "fields": [
                    {"name": "code", "type": "string"},
                    {"name": "amount", "type": "integer"},
                ],
            }
        ]
    }
    payload = synth_payload(schema)
    assert isinstance(payload["line_items"], list) and payload["line_items"]
    assert payload["line_items"][0]["code"]
    assert isinstance(payload["line_items"][0]["amount"], int)


def test_object_nested():
    schema = {
        "fields": [
            {
                "name": "address",
                "type": "object",
                "fields": [
                    {"name": "street", "type": "string"},
                    {"name": "zip", "type": "string"},
                ],
            }
        ]
    }
    payload = synth_payload(schema)
    assert isinstance(payload["address"], dict)
    assert payload["address"]["street"] and payload["address"]["zip"]


def test_payload_passes_schema_validation():
    schema = {
        "fields": [
            {"name": "member_name", "type": "string"},
            {"name": "amount", "type": "integer"},
            {"name": "is_denied", "type": "boolean", "required": True},
        ]
    }
    payload = synth_payload(schema)
    ok, errors = validate_payload(schema, payload)
    assert ok, errors


def test_uses_description_when_present():
    schema = {
        "fields": [
            {"name": "field_xyz", "type": "string", "description": "First Name"},
        ]
    }
    payload = synth_payload(schema)
    # Description should be visible in the sample so the reply PDF looks
    # populated rather than filled with opaque field-key placeholders.
    assert "First Name" in payload["field_xyz"]
