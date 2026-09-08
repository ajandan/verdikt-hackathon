"""Build a plausible test payload from a frozen template's schema.

The inbox agent uses this to prove the newly-mapped template actually
renders: it feeds the synthesized payload into execute_merge and attaches
the resulting docx/pdf to the reply.

Values lean on `FieldSpec.description` (the human label harvested from
PDF placements or the AI mapper) to pick realistic sample data — a
"First Name" field gets "John", "State" gets "NY", "Date of Birth" gets
a real date. This makes the reply attachment credible enough for a
sender to spot-check field placement without being distracted by
placeholder noise.
"""
from __future__ import annotations

import re
from typing import Any

from docgen.core.schema_infer import FieldSpec, TemplateSchema

# Label-pattern → sample value. Ordered from most specific to least.
# The label is lowercased before match. First hit wins so long patterns
# ("date of birth") beat shorter ones ("date") when both would apply.
_STRING_SAMPLES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(social security|ssn)\b"), "123-45-6789"),
    (re.compile(r"\b(date of birth|dob|birth\s*date|birthday)\b"), "1985-06-15"),
    (re.compile(r"\b(mm[/_-]?dd[/_-]?yyyy|mm[/_-]?dd[/_-]?yy)\b"), "06/15/1985"),
    (re.compile(r"\b(date|effective|expiration|start|end)\b.*\b(date)?\b"), "2026-01-15"),
    (re.compile(r"\b(middle initial|\bmi\b)\b"), "A"),
    (re.compile(r"\b(first\s*name|given\s*name|patient\s*first)\b"), "John"),
    (re.compile(r"\b(last\s*name|surname|family\s*name|patient\s*last)\b"), "Doe"),
    (re.compile(r"\b(full\s*name|patient\s*name|name of|beneficiary|member\s*name)\b"), "John A. Doe"),
    (re.compile(r"\b(signature|sign(ed)? by|sig)\b"), "John A. Doe"),
    (re.compile(r"\b(email|e-mail)\b"), "john.doe@example.com"),
    (re.compile(r"\b(phone|telephone|mobile|cell)\b"), "(555) 555-1234"),
    (re.compile(r"\b(fax)\b"), "(555) 555-1235"),
    (re.compile(r"\b(zip|postal)\b"), "10001"),
    (re.compile(r"\b(state|province)\b"), "NY"),
    (re.compile(r"\b(city|town)\b"), "New York"),
    (re.compile(r"\b(country)\b"), "USA"),
    (re.compile(r"\b(street|address|street address|line 1)\b"), "123 Main Street"),
    (re.compile(r"\b(address\s*line\s*2|apt|apartment|suite|unit)\b"), "Apt 4B"),
    (re.compile(r"\b(county)\b"), "New York County"),
    (re.compile(r"\b(gender|sex)\b"), "M"),
    (re.compile(r"\b(marital)\b"), "Single"),
    (re.compile(r"\b(race|ethnicity)\b"), "Prefer not to say"),
    (re.compile(r"\b(language)\b"), "English"),
    (re.compile(r"\b(occupation|employer|company|organization)\b"), "Acme Corp"),
    (re.compile(r"\b(npi|provider\s*(id|number)|prov)\b"), "1234567893"),
    (re.compile(r"\b(dea)\b"), "AB1234567"),
    (re.compile(r"\b(license|lic)\b"), "LIC-12345678"),
    (re.compile(r"\b(policy|policy\s*number|member\s*id|subscriber\s*id|ins(urance)?\s*id)\b"), "POL-000123456"),
    (re.compile(r"\b(group\s*(number|no|id))\b"), "GRP-00789"),
    (re.compile(r"\b(claim|reference|ref|confirmation)\s*(number|no|id)?\b"), "CLM-2026-000123"),
    (re.compile(r"\b(medicare|medicaid|part [abcd]|advantage|hmo|ppo|health\s*insurance|insurance)\b"), "Original Medicare"),
    (re.compile(r"\b(diagnosis|dx|icd)\b"), "E11.9 - Type 2 diabetes"),
    (re.compile(r"\b(cpt|procedure)\b"), "99213"),
    (re.compile(r"\b(medication|med|rx|prescription|drug)\b"), "Metformin 500mg"),
    (re.compile(r"\b(dosage|dose|strength)\b"), "500 mg"),
    (re.compile(r"\b(quantity|qty|refills?)\b"), "30"),
    (re.compile(r"\b(pharmacy)\b"), "Sample Pharmacy"),
    (re.compile(r"\b(hospital|clinic|facility|practice)\b"), "Sample Medical Center"),
    (re.compile(r"\b(department|dept|specialty)\b"), "Internal Medicine"),
    (re.compile(r"\b(amount|balance|total|paid|due|copay|deductible|premium|income|salary|wage)\b"), "$1,250.00"),
    (re.compile(r"\b(household|family)\b.*\b(size|members|people|number)\b"), "3"),
    (re.compile(r"\b(number of|no\.? of|count|qty)\b"), "3"),
    (re.compile(r"\b(comments?|notes?|remarks?|description|reason|explanation)\b"), "Sample comment for testing."),
    (re.compile(r"\b(relation(ship)?)\b"), "Self"),
    (re.compile(r"\b(home|mailing|billing|shipping)\b"), "Home"),
    (re.compile(r"\bto my home\b"), "Yes"),
]


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().title() or "Sample"


def _sample_string_from_label(label: str) -> str:
    lower = label.lower()
    for pat, val in _STRING_SAMPLES:
        if pat.search(lower):
            return val
    # No pattern match — fall back to a human-readable placeholder built
    # from the label so the sender can at least see what field it is.
    return f"Sample {label}"


def _sample_scalar(f: FieldSpec) -> Any:
    label = (f.description or "").strip() or _humanize(f.name)
    lower = label.lower()

    if f.type == "integer":
        # Household size / member count style fields want small counts, not 42.
        if re.search(r"\b(household|family|children|dependents)\b", lower):
            return 3
        if re.search(r"\b(age)\b", lower):
            return 40
        if re.search(r"\b(year)\b", lower):
            return 2026
        return 42
    if f.type == "number":
        if re.search(r"\b(amount|balance|total|paid|due|copay|deductible|premium|income|salary|wage|cost|price)\b", lower):
            return 1250.00
        return 3.14
    if f.type == "boolean":
        # "No" checkboxes should be False; "Yes" / affirmative → True.
        # Anything else: deterministic hash so a page of checkboxes doesn't
        # come out all-True and look like a rendering bug.
        if re.search(r"\bno\b", lower) and not re.search(r"\byes\b", lower):
            return False
        if re.search(r"\byes\b", lower) and not re.search(r"\bno\b", lower):
            return True
        return (sum(ord(c) for c in f.name) % 2) == 0
    if f.type == "date":
        if re.search(r"\b(date of birth|dob|birth|birthday)\b", lower):
            return "1985-06-15"
        return "2026-01-15"
    # string / anything else
    return _sample_string_from_label(label)


def synth_for_field(f: FieldSpec) -> Any:
    if f.type == "list":
        if f.fields:
            inner = {sub.name: synth_for_field(sub) for sub in f.fields}
            return [inner, inner]
        item_type = f.item_type or "string"
        proxy = FieldSpec(
            name=f.name,
            type=item_type,
            required=True,
            description=f.description,
        )
        return [_sample_scalar(proxy), _sample_scalar(proxy)]
    if f.type == "object":
        return {sub.name: synth_for_field(sub) for sub in (f.fields or [])}
    return _sample_scalar(f)


def synth_payload(schema_dict: dict) -> dict[str, Any]:
    """Return one payload dict that satisfies `schema_dict`.

    Optional (`required=False`) fields still get a value — this is a smoke
    test, not a null-handling test. The render pipeline validates against
    the same schema so missing-optional would be fine, but including them
    makes the reply PDF look "populated" for the sender.
    """
    schema = TemplateSchema.model_validate(schema_dict)
    return {f.name: synth_for_field(f) for f in schema.fields}
