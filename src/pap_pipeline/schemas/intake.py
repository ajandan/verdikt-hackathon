"""Structured intake object emitted by the extract stage.

Every field is Optional: a real PAP packet almost never carries all evidence in
one message. Missing fields are the signal that drives the `missing_info`
outcome — the reason stage looks at which rules can't be evaluated against the
present fields and asks for exactly those gaps in the follow-up letter.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

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
    patient_name: str | None = None
    date_of_birth: date | None = None

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
