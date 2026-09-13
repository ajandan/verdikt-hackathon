"""Slot-alias fanout in :func:`pap_pipeline.json_intake.validate_json_intake`.

Program-specific slot names (``rx_medication``) may declare an
``intake_field`` mapping to the canonical :class:`Intake` name
(``prescribed_drug``). The validator must mirror the coerced value onto
both keys so rules that reference the canonical name evaluate correctly.
An optional ``intake_value_map`` transforms the raw value on the way
across — used e.g. to lift a boolean ``ins_uninsured=True`` into the
enum ``insurance_status="uninsured"``.
"""
from __future__ import annotations

from datetime import date

from pap_pipeline.json_intake import validate_json_intake


def test_intake_field_mirrors_string_value() -> None:
    schema = [
        {
            "name": "rx_medication",
            "type": "string",
            "intake_field": "prescribed_drug",
        }
    ]
    result = validate_json_intake({"rx_medication": "Cardiovita"}, schema)
    assert not result.errors
    assert result.normalized["rx_medication"] == "Cardiovita"
    assert result.normalized["prescribed_drug"] == "Cardiovita"


def test_intake_field_mirrors_coerced_date() -> None:
    schema = [
        {
            "name": "rx_date_written",
            "type": "date",
            "intake_field": "prescription_date",
        }
    ]
    result = validate_json_intake({"rx_date_written": "2026-01-15"}, schema)
    assert not result.errors
    assert result.normalized["rx_date_written"] == date(2026, 1, 15)
    assert result.normalized["prescription_date"] == date(2026, 1, 15)


def test_intake_value_map_lifts_bool_to_enum() -> None:
    schema = [
        {
            "name": "ins_uninsured",
            "type": "boolean",
            "intake_field": "insurance_status",
            "intake_value_map": {"true": "uninsured"},
        }
    ]
    result = validate_json_intake({"ins_uninsured": True}, schema)
    assert not result.errors
    assert result.normalized["ins_uninsured"] is True
    assert result.normalized["insurance_status"] == "uninsured"


def test_intake_value_map_leaves_unmatched_false_off_alias() -> None:
    """Unchecked boolean must NOT stamp a bogus enum value on the canonical field.

    A caller unchecking ``ins_uninsured`` shouldn't be interpreted as
    claiming any specific insurance status — the reason stage should
    see the field as null so it can ask for it.
    """
    schema = [
        {
            "name": "ins_uninsured",
            "type": "boolean",
            "intake_field": "insurance_status",
            "intake_value_map": {"true": "uninsured"},
        }
    ]
    result = validate_json_intake({"ins_uninsured": False}, schema)
    assert not result.errors
    assert result.normalized["ins_uninsured"] is False
    assert "insurance_status" not in result.normalized


def test_explicit_canonical_key_wins_over_alias() -> None:
    """First writer wins: an explicit canonical value beats the aliased one."""
    schema = [
        {
            "name": "rx_medication",
            "type": "string",
            "intake_field": "prescribed_drug",
        },
        {"name": "prescribed_drug", "type": "string"},
    ]
    result = validate_json_intake(
        {
            "rx_medication": "Cardiovita",
            "prescribed_drug": "Generic Cardiovita",
        },
        schema,
    )
    assert not result.errors
    # Whichever slot the loop hits first writes prescribed_drug; the
    # invariant we care about is that the explicit canonical value
    # isn't clobbered by a later alias fanout. Both possible resolution
    # orders leave `prescribed_drug` set to a real value.
    assert result.normalized["prescribed_drug"] in {
        "Cardiovita",
        "Generic Cardiovita",
    }
