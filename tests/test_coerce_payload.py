from __future__ import annotations

from docgen.core.schema_infer import coerce_payload


def _schema(*fields: tuple[str, str]) -> dict:
    return {"fields": [{"name": n, "type": t, "required": True} for n, t in fields]}


def test_string_false_becomes_bool_false() -> None:
    schema = _schema(("Proof_Pay_Stubs__c", "boolean"), ("Proof_Other__c", "boolean"))
    out = coerce_payload(schema, {"Proof_Pay_Stubs__c": "true", "Proof_Other__c": "false"})
    assert out["Proof_Pay_Stubs__c"] is True
    assert out["Proof_Other__c"] is False


def test_numeric_strings_coerced() -> None:
    schema = _schema(("count", "integer"), ("amount", "number"))
    out = coerce_payload(schema, {"count": "12", "amount": "3.14"})
    assert out["count"] == 12
    assert out["amount"] == 3.14


def test_extras_preserved() -> None:
    schema = _schema(("flag", "boolean"))
    out = coerce_payload(schema, {"flag": "true", "unknown_extra": "keep me"})
    assert out["flag"] is True
    assert out["unknown_extra"] == "keep me"


def test_already_typed_bool_pass_through() -> None:
    schema = _schema(("flag", "boolean"))
    assert coerce_payload(schema, {"flag": True})["flag"] is True
    assert coerce_payload(schema, {"flag": False})["flag"] is False


def test_invalid_payload_returns_input_dict() -> None:
    """coerce_payload never raises — validate_payload is the gate."""
    schema = _schema(("count", "integer"))
    # not coercible → helper falls back to raw payload; validate_payload catches it upstream
    out = coerce_payload(schema, {"count": "not-a-number"})
    assert out == {"count": "not-a-number"}
