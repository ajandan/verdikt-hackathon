"""Reply-letter rendering for the PAP inbox pipeline.

Extracted from :mod:`pap_pipeline.inbox_hook`. This module owns the
"turn a Decision + Intake into a reply artifact" path:

  * Compose the plain-text / HTML fallback body (:func:`build_reply`) and
    subject line (:func:`reply_subject`) — used when the tenant's outcome
    template is missing so the applicant still gets a note.
  * Resolve the tenant's frozen outcome template
    (:func:`find_reply_template`).
  * Map Intake + Decision onto the ASP-20x template field schema
    (:func:`build_render_data` and the small formatters it composes:
    :func:`compose_salutation`, :func:`compose_city_state_zip`,
    :func:`short_id`, :func:`short_program_code`, :func:`extras_only`).
  * Merge the template to PDF and persist the exact bytes to the
    documents bucket (:func:`render_reply_attachment`).

Split out so the top-level ``inbox_hook`` module can focus on the
inbound-event lifecycle (poll → pipeline → audit → dispatch) without
also owning template merge, address formatting, and blob layout for
the reply half.
"""
from __future__ import annotations

import html
from datetime import date, timedelta
from typing import Any

import structlog

from docgen.config import Settings
from docgen.core.render_pipeline import execute_merge
from docgen.inbox.mailer import Attachment
from docgen.store.blob import put_object
from docgen.store.models import InboxEvent, Template, TemplateVersion, Tenant
from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake

log = structlog.get_logger(__name__)


def build_reply(decision: Decision) -> tuple[str, str, str]:
    """(subject_suffix, text, html) for the applicant reply.

    Only rule-typed citations are rendered as the "rules considered"
    list. History-typed citations (ADR-0004) belong in the outcome-bound
    template body, not this evidence summary.

    HTML output escapes every LLM-derived string (rationale, rule_id,
    missing_fields entries). The LLM is instructed to emit data, not
    markup, but the escape is the load-bearing defense: framing shapes
    behavior, escaping enforces it. Applicant email clients render this
    body verbatim, so a raw <script> or <img onerror=…> in a hostile
    rationale would fire otherwise. `outcome` is a Pydantic Literal
    (approve / deny / missing_info) — enum-constrained so no escape
    needed, but the label doesn't hurt.
    """
    outcome = decision.outcome
    rule_cites = [c for c in decision.citations if c.type == "rule"]
    cite_lines = [
        f"- {c.rule_id}"
        + (f" (score {c.score:.2f})" if c.score is not None else "")
        + (" — matched" if c.matched else " — not matched")
        for c in rule_cites
    ]
    missing_lines = [f"- {m}" for m in decision.missing_fields]

    text_parts = [
        f"Decision: {outcome}",
        "",
        decision.rationale or "(no rationale provided)",
    ]
    if cite_lines:
        text_parts += ["", "Rules considered:", *cite_lines]
    if missing_lines:
        text_parts += ["", "Missing information:", *missing_lines]
    text = "\n".join(text_parts) + "\n"

    rationale_html = html.escape(decision.rationale or "(no rationale provided)")
    html_parts = [
        f"<p><b>Decision:</b> {html.escape(outcome)}</p>",
        f"<p>{rationale_html}</p>",
    ]
    if cite_lines:
        html_parts.append(
            "<p><b>Rules considered:</b></p><ul>"
            + "".join(f"<li>{html.escape(line[2:])}</li>" for line in cite_lines)
            + "</ul>"
        )
    if missing_lines:
        html_parts.append(
            "<p><b>Missing information:</b></p><ul>"
            + "".join(f"<li>{html.escape(line[2:])}</li>" for line in missing_lines)
            + "</ul>"
        )
    body_html = "".join(html_parts)
    return outcome, text, body_html


def reply_subject(original: str | None, outcome: str) -> str:
    base = (original or "PAP intake").strip()
    prefix = "" if base.lower().startswith("re:") else "Re: "
    return f"{prefix}{base} — {outcome}"


# Outcome → substring on template name. The Alderwood letter set is
# ASP-201 (approval), ASP-202 (denial), ASP-203 (missing-info) — we key on
# the human-readable middle segment so re-uploads under new versions still
# resolve as long as the naming stays consistent.
_OUTCOME_TO_TEMPLATE_HINT = {
    "approve": "approv",
    "deny": "deni",
    "missing_info": "missing",
}


def find_reply_template(
    session: Any, tenant: Tenant, outcome: str
) -> TemplateVersion | None:
    """Resolve the tenant's frozen reply template for this outcome.

    Picks the most recent frozen version whose template name contains the
    outcome hint (approv / deni / missing). Returns None if the tenant hasn't
    uploaded that outcome's template yet — the caller then falls back to a
    text-only reply so a single missing template doesn't break the flow.
    """
    hint = _OUTCOME_TO_TEMPLATE_HINT.get(outcome)
    if not hint:
        return None
    row = (
        session.query(TemplateVersion)
        .join(Template, Template.id == TemplateVersion.template_id)
        .filter(
            Template.tenant_id == tenant.id,
            Template.is_active.is_(True),
            Template.name.ilike(f"%{hint}%"),
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(
            TemplateVersion.version.desc(),
            TemplateVersion.subversion.desc(),
            TemplateVersion.created_at.desc(),
        )
        .first()
    )
    return row


def short_id(event_id: Any) -> str:
    return str(event_id).replace("-", "")[:8].upper()


# Long program codes ("CARDIOVITA-PAP-2026") overflow the header band cell
# on the ASP-20x letters — the cell fits ~83pt, the long form needs ~110pt at
# 9.8pt Helvetica-Bold, so it clips silently at the band edge. Compressing
# common program slugs to their short form keeps the header legible without
# widening the template.
_PROGRAM_CODE_SHORT = {
    "cardiovita-pap-2026": "CV-PAP-2026",
}


def short_program_code(program: str | None) -> str:
    """Return the header-band-safe program code for the reply letter.

    Known slugs (see :data:`_PROGRAM_CODE_SHORT`) render as their curated
    short form; unknown slugs fall back to an upper-cased version of the
    input and emit a warning so a mis-branded letter is at least traceable
    in the log stream. The default slug for a None/empty input matches
    the demo program's canonical slug — that fallback is a hackathon
    convenience, not a multi-tenant guarantee.
    """
    slug = (program or "cardiovita-pap-2026").lower()
    if slug in _PROGRAM_CODE_SHORT:
        return _PROGRAM_CODE_SHORT[slug]
    log.warning(
        "reply_render_unknown_program_code",
        slug=slug,
        note="fallback to slug.upper(); add an entry to _PROGRAM_CODE_SHORT for a curated short form",
    )
    return slug.upper()


def compose_salutation(intake: Intake) -> str:
    """Compose a natural greeting from honorific + family name.

    Prefers "Dear Ms. Chen," when the form supplied a title, falling back
    through last-name-only ("Dear Chen,") and finally the full legal name
    ("Dear Maria L Chen,") only when nothing else is available. The reply
    letter reserves the whole salutation line so a merged full-name string
    reads as an obvious template dump rather than a natural greeting.
    """
    honorific = (intake.honorific or "").strip().rstrip(".")
    family = (intake.family_name or "").strip()
    if honorific and family:
        return f"Dear {honorific}. {family},"
    if family:
        return f"Dear {family},"
    if intake.patient_name:
        return f"Dear {intake.patient_name},"
    return "Dear Applicant,"


def compose_city_state_zip(intake: Intake) -> str:
    """Assemble a `City, State ZIP` (or non-US equivalent) line from parts.

    Non-US applicants often have no `mailing_state`; the `city` + `country`
    is still worth showing so the recipient block on the denial letter
    matches what the applicant actually wrote, rather than the state-only
    fragment the old mapper produced.
    """
    city = (intake.city or "").strip()
    state = (intake.mailing_state or "").strip()
    postal = (intake.postal_code or "").strip()
    country = (intake.country or intake.country_of_residence or "").strip()

    left = ", ".join(p for p in (city, state) if p)
    parts = [p for p in (left, postal) if p]
    line = " ".join(parts).strip()
    # Only append country when it's non-US — US recipients don't expect
    # "United States" on their own address block.
    if country and country.lower() not in {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}:  # noqa: E501
        line = f"{line}, {country}" if line else country
    return line


def extras_only(raw_extraction: dict[str, Any]) -> dict[str, Any]:
    """Fields in the raw extraction that aren't part of Intake.

    Used by the reply mapper so that when a program defines a custom slot
    like `member_id` and the tenant's letter template has a matching field
    name, the value flows through end-to-end without needing an Intake
    schema change.
    """
    known = set(Intake.model_fields.keys())
    return {k: v for k, v in (raw_extraction or {}).items() if k not in known}


# Coordinator identity on the reply letter. Tenant-specific but the
# Program model doesn't yet carry a `reply_letter_config` column — see
# the note on :func:`build_render_data`. Callers can override via
# `extras["_coordinator_name"]` etc. until the config column ships.
_DEFAULT_COORDINATOR_NAME = "Alderwood PAP Care Coordinator"
_DEFAULT_COORDINATOR_TITLE = "Patient Assistance Care Coordinator"
_DEFAULT_COORDINATOR_PHONE = "1-877-555-0100"


def build_render_data(
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    extras: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Map Intake + Decision onto the ASP-20x template field schema.

    Common fields (`alderwood_*`) appear on all three templates. Outcome
    branches (`application_has_*`, `we_not_*`, `we_need_*`) add the fields
    the specific letter uses. Missing template fields are simply left as
    empty strings — the renderer treats them as blanks rather than failing.

    ``today`` pins the reference date used for the letter date and the
    approve/deny/missing-info deadline math. Tests inject a fixed date so
    fixture output doesn't drift as they age; production callers leave it
    None to get ``date.today()``. The clock is read at most once per
    call so every downstream date stays consistent even if midnight
    passes mid-render.

    Coordinator name / title / phone come from `extras` when present
    (`_coordinator_name`, `_coordinator_title`, `_coordinator_phone`) so
    a second program with its own signatory can override without touching
    this file. When absent, the Alderwood defaults win — a hardcoded
    fallback that will produce the wrong signatory on any non-Alderwood
    tenant. A future migration should surface these three fields as a
    `reply_letter_config` JSONB column on Program; until then the extras
    override is the multi-tenant escape hatch.
    """
    today = today if today is not None else date.today()
    patient = intake.patient_name or "Applicant"
    salutation = compose_salutation(intake)
    dob = intake.date_of_birth.strftime("%m/%d/%Y") if intake.date_of_birth else ""
    street = (intake.address_line1 or "").strip()
    apt = (intake.address_line2 or "").strip()
    city_state_zip = compose_city_state_zip(intake)
    program_code = short_program_code(program)
    # Prefer the referral ID printed on the paper form so the reply
    # reconciles back to the applicant's file. Fall back to a pipeline-
    # invented `ALD-{short-uuid}` only when the form didn't carry one.
    referral = (intake.referral_id or "").strip() or f"ALD-{short_id(event.id)}"
    extras_dict = extras or {}
    coordinator = str(extras_dict.get("_coordinator_name") or _DEFAULT_COORDINATOR_NAME)
    coord_title = str(extras_dict.get("_coordinator_title") or _DEFAULT_COORDINATOR_TITLE)
    direct_line = str(extras_dict.get("_coordinator_phone") or _DEFAULT_COORDINATOR_PHONE)
    med = intake.prescribed_drug or ""

    # Program-specific slots seed the render dict first — a template field
    # named `member_id` picks up the extracted value here even though it's
    # not part of Intake. Alderwood mappings below overwrite these where
    # slot names collide with the canonical `alderwood_*` schema.
    common: dict[str, Any] = {k: v for k, v in (extras or {}).items() if v is not None}
    common.update({
        "alderwood_addressee": patient,
        "alderwood_patient": patient,
        "alderwood_dob": dob,
        "alderwood_letter_date": today.strftime("%m/%d/%Y"),
        "alderwood_street_address": street,
        "alderwood_apt_unit": apt,
        "alderwood_city_state_zip": city_state_zip,
        "alderwood_program_code": program_code,
        "alderwood_referral_id": referral,
    })

    if decision.outcome == "approve":
        # +365 days rather than year=today.year+1: today.replace() raises
        # ValueError on Feb 29 when next year isn't a leap year, and the
        # letter's approval period is descriptive prose rather than a
        # legal-precise anniversary — one day of drift near Feb 29 is
        # acceptable, an intermittent Feb-29 crash is not.
        approval_end = today + timedelta(days=365)
        common.update({
            "application_has_salutation": salutation,
            "application_has_med": med,
            "application_has_rx": med,
            "application_has_strength_dosage_form": "",
            "application_has_approval_period": (
                f"{today.strftime('%m/%d/%Y')} – {approval_end.strftime('%m/%d/%Y')}"
            ),
            "application_has_refills_auth": "",
            "application_has_quantity_per_fill": "",
            "application_has_ship": "Patient home",
            "application_has_pap_member_id": referral,
            "application_has_coordinator": coordinator,
            "application_has_title": coord_title,
            "application_has_direct_line": direct_line,
        })
    elif decision.outcome == "deny":
        recon = today + timedelta(days=30)
        common.update({
            "we_not_salutation": salutation,
            "we_not_med_requested": med,
            "we_not_rx": med,
            "we_not_application_received": today.strftime("%m/%d/%Y"),
            "we_not_determination_date": today.strftime("%m/%d/%Y"),
            "we_not_outcome": "denied",
            "we_not_reason_decision": decision.rationale or "See enclosed rule citations.",
            "we_not_reconsideration_deadline": recon.strftime("%m/%d/%Y"),
            "we_not_coordinator": coordinator,
            "we_not_title": coord_title,
            "we_not_direct_line": direct_line,
        })
    elif decision.outcome == "missing_info":
        respond_by = today + timedelta(days=14)
        items = "\n".join(f"• {m}" for m in decision.missing_fields) or (
            decision.rationale or "Please review the enclosed request."
        )
        common.update({
            "we_need_salutation": salutation,
            "we_need_med_requested": med,
            "we_need_rx": med,
            "we_need_application_received": today.strftime("%m/%d/%Y"),
            "we_need_items_still_needed": items,
            "we_need_respond": respond_by.strftime("%m/%d/%Y"),
            "we_need_coordinator": coordinator,
            "we_need_title": coord_title,
            "we_need_direct_line": direct_line,
        })

    return common


def render_reply_attachment(
    session: Any,
    tenant: Tenant,
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    settings: Settings,
    *,
    extras: dict[str, Any] | None = None,
) -> tuple[Attachment | None, str | None]:
    """Render the outcome-matched Alderwood letter to PDF bytes.

    Returns `(attachment, blob_key)`. Either half can be None independently:

    - `(None, None)` — template missing / render failed. Caller falls back to
      a text-only reply so a single missing template doesn't drop the flow.
    - `(Attachment, None)` — render succeeded, blob upload failed. Caller
      still sends the PDF over SMTP; the reviewer just can't re-download it
      from the pipeline detail page. All failure modes log for the operator.

    The caller assigns `blob_key` onto `event.reply_attachment_blob_key`
    explicitly — the sequencing is visible at the call site rather than
    hidden behind a side effect on this function.
    """
    tv = find_reply_template(session, tenant, decision.outcome)
    if tv is None:
        log.warning("pap_reply_template_missing", outcome=decision.outcome, tenant=str(tenant.id))
        return None, None
    data = build_render_data(intake, decision, program, event, extras=extras)
    try:
        pdf_bytes = execute_merge(tv, data, output_format="pdf", settings=settings)
    except Exception as e:
        log.warning(
            "pap_reply_render_failed",
            template_version=str(tv.id),
            outcome=decision.outcome,
            error=f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        return None, None
    filename = f"alderwood_{decision.outcome}_{short_id(event.id)}.pdf"
    attachment = Attachment(
        filename=filename, content=pdf_bytes, content_type="application/pdf"
    )
    # Persist the exact bytes we're about to send so the reviewer can pull
    # the same PDF from the pipeline detail page. Blob-store failures
    # shouldn't drop the email attachment — log and return a None key so the
    # caller skips the DB write.
    blob_key = f"inbox-replies/{event.id}/{filename}"
    try:
        put_object(
            settings.s3_bucket_documents,
            blob_key,
            pdf_bytes,
            content_type="application/pdf",
        )
    except Exception as e:
        log.warning(
            "pap_reply_blob_persist_failed",
            event_id=str(event.id),
            error=f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        return attachment, None
    return attachment, blob_key


__all__ = [
    "build_render_data",
    "build_reply",
    "compose_city_state_zip",
    "compose_salutation",
    "extras_only",
    "find_reply_template",
    "render_reply_attachment",
    "reply_subject",
    "short_id",
    "short_program_code",
]
