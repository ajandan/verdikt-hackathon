"""Stage 2 (triage) — case-type classifier + downstream router.

Locks in the classifier's precedence order (subject > extras > intake
shape > default) and the case-type-to-route mapping. Also covers the
``enabled=False`` short-circuit that preserves pre-Triage behavior.

No I/O, no network — the classifier is pure Python today. When it's
upgraded to an AI-backed variant, the shape stays; only the internals
change.
"""
from __future__ import annotations

import re

import pytest

from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages.triage import (
    TriageConfig,
    TriageDecision,
    triage,
)


def _rich_intake() -> Intake:
    """Intake with enough populated fields to clear the ``not_decidable``
    threshold on its own."""
    return Intake(
        patient_name="Devon Okafor",
        family_name="Okafor",
        household_size=2,
        annual_household_income=32000.0,
        prescribed_drug="Cardiovita",
        prescriber_npi="1234567890",
    )


# ---------------------------------------------------------------------------
# Enabled/disabled contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_config_always_routes_to_decide() -> None:
    """``enabled=False`` short-circuits to ``new_intake`` → ``decide``
    regardless of the other signals. Preserves pre-Triage behavior so we
    can roll the code out without changing any tenant's runtime path."""
    result = await triage(
        _rich_intake(),
        config=TriageConfig(enabled=False),
        subject="Please refill my Cardiovita",  # would normally match refill
        extras={"case_number": "ASP-4821"},     # would normally match missing_info_reply
    )
    assert result.case_type == "new_intake"
    assert result.route == "decide"
    assert result.signals == {"triage_enabled": False}


# ---------------------------------------------------------------------------
# Subject-line signal (highest precedence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subject, expected_case_type, expected_route",
    [
        ("Refill request for Cardiovita", "refill", "pharmacy_direct"),
        ("Please re-supply my prescription", "refill", "pharmacy_direct"),
        ("Appeal of denial ASP-2026-4821", "appeal", "crm_appeals"),
        ("Reconsideration of my application", "appeal", "crm_appeals"),
        ("Missing documents attached", "missing_info_reply", "decide"),
        ("Follow-up on my application", "missing_info_reply", "decide"),
        ("Inquiry about the program", "not_decidable", "reject"),
    ],
)
async def test_subject_line_drives_case_type(
    subject: str, expected_case_type: str, expected_route: str
) -> None:
    result = await triage(_rich_intake(), subject=subject)
    assert result.case_type == expected_case_type
    assert result.route == expected_route
    assert result.signals["subject"] == subject
    assert "subject_match" in result.signals


@pytest.mark.asyncio
async def test_subject_precedence_beats_prior_reference() -> None:
    """Subject line wins over extras evidence. An email whose subject
    says ``refill`` is a refill even if the applicant quoted an old case
    number in the body."""
    result = await triage(
        _rich_intake(),
        subject="Refill for Cardiovita",
        extras={"case_number": "ASP-4821"},
    )
    assert result.case_type == "refill"
    assert result.route == "pharmacy_direct"


@pytest.mark.asyncio
async def test_word_boundaries_prevent_false_matches() -> None:
    """``\\brefill\\b`` should not match ``refillable`` — a subject like
    'refillable products discussion' is not a refill request."""
    result = await triage(
        _rich_intake(),
        subject="Refillable products general discussion",
    )
    # None of the patterns should match; falls through to default.
    assert result.case_type == "new_intake"
    assert result.route == "decide"


# ---------------------------------------------------------------------------
# Extras signal (second precedence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["previous_referral_id", "case_number", "application_id", "prior_referral_id"],
)
async def test_prior_reference_in_extras_routes_to_missing_info(key: str) -> None:
    """Any of the recognized prior-reference keys with a truthy value
    classifies as a missing-info reply, which routes back to Decide (the
    applicant is sending the delta the previous run asked for)."""
    result = await triage(_rich_intake(), extras={key: "ASP-4821"})
    assert result.case_type == "missing_info_reply"
    assert result.route == "decide"
    assert result.signals["prior_reference_key"] == key
    assert result.signals["prior_reference_value"] == "ASP-4821"


@pytest.mark.asyncio
async def test_empty_prior_reference_value_is_ignored() -> None:
    """A key present with an empty string is not evidence."""
    result = await triage(_rich_intake(), extras={"case_number": ""})
    # No signal → default new_intake
    assert result.case_type == "new_intake"


# ---------------------------------------------------------------------------
# Intake-shape signal (third precedence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nearly_empty_intake_is_not_decidable() -> None:
    """An intake with fewer than 3 populated fields is treated as noise
    (probably a plain-text inquiry, not a real application)."""
    result = await triage(Intake(patient_name="Devon"))  # 1 populated field
    assert result.case_type == "not_decidable"
    assert result.route == "reject"
    assert result.signals["populated_fields"] == 1


@pytest.mark.asyncio
async def test_intake_at_threshold_continues_to_decide() -> None:
    """Exactly at the threshold (3 fields) should NOT be rejected — the
    check is 'below threshold', so 3 populated fields is acceptable."""
    result = await triage(
        Intake(
            patient_name="Devon",
            family_name="Okafor",
            prescribed_drug="Cardiovita",
        )
    )
    assert result.case_type == "new_intake"
    assert result.route == "decide"
    assert result.signals["populated_fields"] == 3


@pytest.mark.asyncio
async def test_zero_valued_field_counts_as_populated() -> None:
    """``annual_household_income=0`` is meaningful data (indigent
    applicant), not missing data. The count should include it."""
    result = await triage(
        Intake(
            patient_name="Devon",
            annual_household_income=0.0,
            prescribed_drug="Cardiovita",
        )
    )
    assert result.signals["populated_fields"] >= 3


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_new_intake_when_no_signals_match() -> None:
    """Rich intake, no subject, no prior reference → default path."""
    result = await triage(_rich_intake())
    assert result.case_type == "new_intake"
    assert result.route == "decide"
    assert "no short-circuit" in result.reason.lower()


# ---------------------------------------------------------------------------
# Config overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_subject_hints_override_defaults() -> None:
    """Tenant can pass their own regex → case_type table."""
    custom = (
        (re.compile(r"URGENT", re.I), "appeal"),
    )
    result = await triage(
        _rich_intake(),
        config=TriageConfig(subject_hints=custom),
        subject="URGENT case needs review",
    )
    assert result.case_type == "appeal"


@pytest.mark.asyncio
async def test_custom_route_mapping_overrides_defaults() -> None:
    """Tenant can reroute refills through CRM instead of direct to Pharmacy."""
    custom_routes = {
        "new_intake": "decide",
        "refill": "crm_outreach",  # was "pharmacy_direct"
        "appeal": "crm_appeals",
        "missing_info_reply": "decide",
        "not_decidable": "reject",
    }
    result = await triage(
        _rich_intake(),
        config=TriageConfig(route_by_case_type=custom_routes),
        subject="Refill please",
    )
    assert result.case_type == "refill"
    assert result.route == "crm_outreach"


# ---------------------------------------------------------------------------
# TriageDecision shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_routing_includes_decision_needed_flag() -> None:
    """Refill default is decision_needed=False (skip Decide); new_intake is True."""
    r_refill = await triage(_rich_intake(), subject="Refill request")
    assert r_refill.case_type == "refill"
    assert r_refill.decision_needed is False

    r_new = await triage(_rich_intake())  # no subject → default new_intake
    assert r_new.case_type == "new_intake"
    assert r_new.decision_needed is True


@pytest.mark.asyncio
async def test_custom_routing_carries_decision_needed_override() -> None:
    """A tenant/program can override decision_needed per row (e.g., force
    refills back through Decide for stricter tenants)."""
    custom = {
        "refill": {"route": "decide", "decision_needed": True},
    }
    result = await triage(
        _rich_intake(),
        config=TriageConfig(routing=custom),
        subject="Refill please",
    )
    assert result.case_type == "refill"
    assert result.route == "decide"
    assert result.decision_needed is True  # overridden


@pytest.mark.asyncio
async def test_program_can_add_dynamic_case_types() -> None:
    """A program-configured case_type not in the platform default gets picked
    up via the routing dict — LLM prompt is built from configured types."""
    from pap_pipeline.stages.triage import normalize_routing
    custom = {
        "prescription_transfer": {"route": "pharmacy_direct", "decision_needed": False},
    }
    routing = normalize_routing(custom)
    assert "prescription_transfer" in routing
    assert routing["prescription_transfer"]["route"] == "pharmacy_direct"


@pytest.mark.asyncio
async def test_result_is_immutable_frozen_dataclass() -> None:
    """TriageDecision is a frozen dataclass so a caller can't accidentally
    mutate what the audit log will persist."""
    result = await triage(_rich_intake())
    assert isinstance(result, TriageDecision)
    with pytest.raises((AttributeError, Exception)):
        result.case_type = "appeal"  # type: ignore[misc]
