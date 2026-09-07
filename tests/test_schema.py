from __future__ import annotations

from docgen.core.schema_infer import FieldSpec, TemplateSchema, validate_payload


def test_json_schema_shape() -> None:
    ts = TemplateSchema(
        fields=[
            FieldSpec(name="member_name", type="string"),
            FieldSpec(name="amount", type="number"),
            FieldSpec(name="notes", type="string", required=False),
        ]
    )
    js = ts.to_json_schema()
    assert js["type"] == "object"
    assert set(js["required"]) == {"member_name", "amount"}
    assert js["properties"]["amount"]["type"] == "number"


def test_validate_payload_ok_and_fail() -> None:
    schema = TemplateSchema(
        fields=[
            FieldSpec(name="member_name", type="string"),
            FieldSpec(name="amount", type="number"),
        ]
    ).model_dump(mode="json")

    ok, errs = validate_payload(schema, {"member_name": "A", "amount": 12.5})
    assert ok, errs

    bad, errs = validate_payload(schema, {"member_name": "A"})
    assert not bad
    assert any("amount" in e for e in errs)
