"""Structured intake object emitted by the extract stage.

Every field is Optional: a real PAP packet almost never carries all evidence in
one message. Missing fields are the signal that drives the `missing_info`
outcome — the reason stage looks at which rules can't be evaluated against the
present fields and asks for exactly those gaps in the follow-up letter.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

InsuranceStatus = Literal[
    "uninsured",
    "medicare",
    "medicaid",
    "commercial",
    "commercial_denied",
    "unknown",
]


class Intake(BaseModel):
    """Structured PAP application intake.

    The four demo rules each look at a subset of these fields:
      - CV-INC-01  → household_size, annual_household_income
      - CV-INS-01  → insurance_status, has_denial_letter
      - CV-RES-01  → country_of_residence, mailing_state
      - CV-CLIN-01 → prescribed_drug, prescriber_npi, prescription_date

    Any field left None is a candidate for the `missing_fields` list on a
    missing_info Decision. Do not add defaults here — silence is meaningful.
    """

    # Patient identity — used to address the reply letter, not to gate eligibility.
    # `family_name` + `honorific` let the salutation compose "Dear Ms. Chen,"
    # instead of dropping the full legal name into the greeting line.
    patient_name: str | None = None
    family_name: str | None = None
    honorific: str | None = None
    date_of_birth: date | None = None

    # Mailing address — populates the recipient block on the reply letter.
    # `country` is a duplicate of country_of_residence for cases where the
    # form separates residency from mailing address (rare, but the ASP forms
    # do have both). CV-RES-01 keeps using country_of_residence.
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None

    # Financial — CV-INC-01 checks these against the FPL table.
    household_size: int | None = Field(default=None, ge=1)
    annual_household_income: float | None = Field(default=None, ge=0)

    # Insurance — CV-INS-01 gates on status; commercial_denied requires a denial letter.
    insurance_status: InsuranceStatus | None = None
    has_denial_letter: bool | None = None

    # Residency — CV-RES-01 gates on US residency + mailing state.
    country_of_residence: str | None = None
    mailing_state: str | None = None

    # Clinical — CV-CLIN-01 gates on drug identity, prescriber NPI, and prescription age.
    prescribed_drug: str | None = None
    prescriber_npi: str | None = None
    prescription_date: date | None = None

    # Form-supplied case identifier (e.g. ALD-26-207802). Preferred over the
    # pipeline-generated fallback so audit trails reconcile back to the paper
    # form. Left None when the form doesn't carry one.
    referral_id: str | None = None


def merge_intake_and_extras(
    intake: "Intake", extras: dict[str, Any] | None
) -> dict[str, Any]:
    """Return ``extras ∪ intake`` with intake's NON-NULL values winning
    on key collision.

    Single source of truth for the "what did the model actually see"
    merge that three separate passes rely on:

    * :func:`pap_pipeline.stages.reason._build_user_message` — the
      prompt payload sent to Nemotron.
    * :func:`pap_pipeline.orchestrator._enforce_undetermined_outcome`
      — the data-presence gate.
    * :func:`pap_pipeline.orchestrator._attach_rule_evaluator_traces`
      — the values dict passed to the deterministic evaluator.

    Ordering rule matters: applicant-submitted canonical fields beat
    MDM-derived / program-specific slot values when BOTH are populated.
    But a null on the Intake side must NOT wipe a real value in extras
    — program-native pipelines fill extras with form data and leave the
    canonical Intake empty; naively overwriting extras with intake
    Nones was silently blanking every real value and breaking rule
    evaluation. The non-null filter fixes that while preserving the
    "intake authoritative on collision" contract for cases where both
    sides carry data.
    """
    merged: dict[str, Any] = dict(extras or {})
    for k, v in intake.model_dump(mode="json").items():
        if v is None:
            continue
        merged[k] = v
    return merged


__all__ = ["Intake", "InsuranceStatus", "merge_intake_and_extras"]
