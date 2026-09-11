"""Registers the PAP pipeline as a plugin against the inbox agent's handler slot.

Loaded by docgen's plugin loader when `DOCGEN_PLUGINS=pap_pipeline.inbox_hook`
is set at boot. Docgen never imports this module directly (see the boundary
test) — the loader resolves it dynamically, and this module reaches back into
docgen only from the pap_pipeline side of the fence.

Contract:
  - Called after resolve_tenant + tenant_id stamp, before _pick_attachment.
  - Owns its own status/review_state/reply lifecycle; must return a
    ProcessResult so the actor can log the outcome.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import structlog

from docgen.config import Settings
from docgen.core.render_pipeline import execute_merge
from docgen.inbox.agent import ProcessResult, set_pap_handler
from docgen.inbox.imap_poller import InboundMessage
from docgen.inbox.mailer import Attachment, MailerError, send_reply
from docgen.store.blob import put_object
from docgen.store.models import InboxEvent, Template, TemplateVersion, Tenant

from pap_pipeline.orchestrator import PipelineConfig, run_with_intake
from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig

log = structlog.get_logger(__name__)

# Reply-outcome mapping. Approvals auto-approve; denials mark rejected;
# missing_info parks in pending_review so a human can decide whether to ask
# the applicant or override.
_OUTCOME_TO_REVIEW_STATE = {
    "approve": "auto_approved",
    "deny": "rejected",
    "missing_info": "pending_review",
}


def _pipeline_config(settings: Settings) -> PipelineConfig:
    """Build a PipelineConfig from docgen Settings — same Nebius endpoint the
    template pipeline uses. The three stages share `ai_base_url` + `ai_api_key`
    because they all target Nebius Token Factory."""
    base_url = (settings.ai_base_url or "https://api.studio.nebius.com/v1").rstrip("/")
    api_key = settings.ai_api_key or ""
    return PipelineConfig(
        extract=ExtractClientConfig(
            base_url=base_url,
            api_key=api_key,
            vision_model=settings.pdf_vision_ocr_model or "openbmb/MiniCPM-V-4_5",
            timeout=settings.ai_timeout_seconds,
        ),
        retrieve=RetrieveClientConfig(
            base_url=base_url,
            api_key=api_key,
            # Nebius Token Factory doesn't host the NVIDIA embed/rerank NIMs
            # yet, so use their sole embed model and skip rerank. The NVIDIA
            # requirement is met by the reason stage's Nemotron.
            embed_model="Qwen/Qwen3-Embedding-8B",
            rerank_model=None,
            timeout=settings.ai_timeout_seconds,
        ),
        reason=ReasonClientConfig(
            base_url=base_url,
            api_key=api_key,
            model=settings.ai_verdict_model or "nvidia/nemotron-3-super-120b-a12b",
            timeout=settings.ai_timeout_seconds,
        ),
    )


def _pap_attachments(msg: InboundMessage) -> list[bytes]:
    """Every attachment goes to the pipeline — a claim often spans a pay stub,
    an insurance card, and a prescription across separate files."""
    return [a.content for a in msg.attachments if a.content]


def _score_from_decision(decision: Decision) -> float | None:
    """Highest citation score, scaled to 0–100 to match the template flow's
    review-page score column. None when no citation carries a score."""
    scores = [c.score for c in decision.citations if c.score is not None]
    if not scores:
        return None
    return round(max(scores) * 100.0, 1)


def _build_reply(decision: Decision) -> tuple[str, str, str]:
    """(subject_suffix, text, html) for the applicant reply."""
    outcome = decision.outcome
    cite_lines = [
        f"- {c.rule_id}"
        + (f" (score {c.score:.2f})" if c.score is not None else "")
        + (" — matched" if c.matched else " — not matched")
        for c in decision.citations
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

    html_parts = [
        f"<p><b>Decision:</b> {outcome}</p>",
        f"<p>{decision.rationale or '(no rationale provided)'}</p>",
    ]
    if cite_lines:
        html_parts.append(
            "<p><b>Rules considered:</b></p><ul>"
            + "".join(f"<li>{line[2:]}</li>" for line in cite_lines)
            + "</ul>"
        )
    if missing_lines:
        html_parts.append(
            "<p><b>Missing information:</b></p><ul>"
            + "".join(f"<li>{line[2:]}</li>" for line in missing_lines)
            + "</ul>"
        )
    html = "".join(html_parts)
    return outcome, text, html


def _reply_subject(original: str | None, outcome: str) -> str:
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


def _find_reply_template(
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


def _short_id(event_id: Any) -> str:
    return str(event_id).replace("-", "")[:8].upper()


def _build_render_data(
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
) -> dict[str, Any]:
    """Map Intake + Decision onto the ASP-20x template field schema.

    Common fields (`alderwood_*`) appear on all three templates. Outcome
    branches (`application_has_*`, `we_not_*`, `we_need_*`) add the fields
    the specific letter uses. Missing template fields are simply left as
    empty strings — the renderer treats them as blanks rather than failing.
    """
    today = date.today()
    patient = intake.patient_name or "Applicant"
    salutation = f"Dear {patient},"
    dob = intake.date_of_birth.strftime("%m/%d/%Y") if intake.date_of_birth else ""
    city_state_zip = intake.mailing_state or ""
    program_code = (program or "cardiovita-pap-2026").upper()
    referral = f"ALD-{_short_id(event.id)}"
    coordinator = "Alderwood PAP Care Coordinator"
    direct_line = "1-877-555-0100"
    coord_title = "Patient Assistance Care Coordinator"
    med = intake.prescribed_drug or ""

    common: dict[str, Any] = {
        "alderwood_addressee": patient,
        "alderwood_patient": patient,
        "alderwood_dob": dob,
        "alderwood_letter_date": today.strftime("%m/%d/%Y"),
        "alderwood_street_address": "",
        "alderwood_apt_unit": "",
        "alderwood_city_state_zip": city_state_zip,
        "alderwood_program_code": program_code,
        "alderwood_referral_id": referral,
    }

    if decision.outcome == "approve":
        approval_end = today.replace(year=today.year + 1)
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


def _render_reply_attachment(
    session: Any,
    tenant: Tenant,
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    settings: Settings,
) -> Attachment | None:
    """Render the outcome-matched Alderwood letter to PDF bytes.

    Returns None if the template is missing, unplaced, or fails to render —
    the caller keeps the flow alive by sending a text-only reply. All three
    failure modes log so an operator can see why the attachment dropped.
    """
    tv = _find_reply_template(session, tenant, decision.outcome)
    if tv is None:
        log.warning("pap_reply_template_missing", outcome=decision.outcome, tenant=str(tenant.id))
        return None
    data = _build_render_data(intake, decision, program, event)
    try:
        pdf_bytes = execute_merge(tv, data, output_format="pdf", settings=settings)
    except Exception as e:
        log.warning(
            "pap_reply_render_failed",
            template_version=str(tv.id),
            outcome=decision.outcome,
            error=f"{type(e).__name__}: {e}",
        )
        return None
    filename = f"alderwood_{decision.outcome}_{_short_id(event.id)}.pdf"
    # Persist the exact bytes we're about to send so the reviewer can pull
    # the same PDF from the pipeline detail page. Blob-store failures
    # shouldn't drop the email attachment — log and move on with a null key.
    blob_key = f"inbox-replies/{event.id}/{filename}"
    try:
        put_object(
            settings.s3_bucket_documents,
            blob_key,
            pdf_bytes,
            content_type="application/pdf",
        )
        event.reply_attachment_blob_key = blob_key
    except Exception as e:
        log.warning(
            "pap_reply_blob_persist_failed",
            event_id=str(event.id),
            error=f"{type(e).__name__}: {e}",
        )
    return Attachment(filename=filename, content=pdf_bytes, content_type="application/pdf")


def _send_pap_reply(
    settings: Settings,
    msg: InboundMessage,
    decision: Decision,
    *,
    attachment: Attachment | None = None,
) -> str | None:
    if not settings.smtp_host:
        return None
    outcome, text, html = _build_reply(decision)
    try:
        return send_reply(
            settings=settings,
            to_addrs=[msg.from_addr],
            cc_addrs=list(msg.cc_addrs),
            subject=_reply_subject(msg.subject, outcome),
            body_text=text,
            body_html=html,
            in_reply_to=msg.message_id,
            references=msg.references + [msg.message_id],
            attachments=[attachment] if attachment else None,
        )
    except MailerError as e:
        log.warning("pap_reply_failed", error=str(e))
        return None


def _run_pipeline(
    attachments: list[bytes], program: str | None, settings: Settings
) -> tuple[Intake, Decision]:
    """Sync wrapper — the inbox actor is sync (asyncio.run per call is fine
    here; each call gets its own loop and NullPool means no cross-loop DB
    reuse). Returns both Intake and Decision so the reply renderer can fill
    patient-scoped fields the Decision doesn't carry."""
    import asyncio

    config = _pipeline_config(settings)
    return asyncio.run(run_with_intake(attachments, config=config, program=program))


def _handler(
    session: Any,
    *,
    event: InboxEvent,
    msg: InboundMessage,
    tenant: Tenant,
    program: str | None,
    settings: Settings,
) -> ProcessResult:
    """PAP handler entry point. Signature is fixed by docgen.inbox.agent."""
    attachments = _pap_attachments(msg)
    if not attachments:
        event.status = "skipped"
        event.error = "pap: no attachments to process"
        session.commit()
        log.info("pap_no_attachments", event_id=str(event.id))
        return ProcessResult(event.id, "skipped", event.error)

    event.status = "processing"
    event.attachment_name = msg.attachments[0].filename if msg.attachments else None
    session.commit()

    try:
        intake, decision = _run_pipeline(attachments, program=program, settings=settings)
    except Exception as e:
        event.status = "failed"
        event.error = f"pap_pipeline_failed: {type(e).__name__}: {e}"
        session.commit()
        log.exception("pap_pipeline_failed", event_id=str(event.id))
        return ProcessResult(event.id, "failed", event.error)

    event.score = _score_from_decision(decision)
    event.review_state = _OUTCOME_TO_REVIEW_STATE.get(decision.outcome, "pending_review")
    event.status = "succeeded"
    event.pap_intake = intake.model_dump(mode="json")
    event.pap_decision = decision.model_dump(mode="json")
    session.commit()

    attachment = _render_reply_attachment(
        session, tenant, intake, decision, program, event, settings
    )
    # Commit the blob key eagerly so it's visible on the pipeline detail
    # page even if SMTP is offline and we skip the send.
    if event.reply_attachment_blob_key:
        session.commit()
    reply_id = _send_pap_reply(settings, msg, decision, attachment=attachment)
    if reply_id:
        event.reply_message_id = reply_id
        session.commit()

    log.info(
        "pap_decision",
        event_id=str(event.id),
        outcome=decision.outcome,
        citations=len(decision.citations),
        missing=len(decision.missing_fields),
    )
    return ProcessResult(event.id, "succeeded", decision.outcome)


# Register at import time — docgen.plugins.load_plugins() imports this module
# when DOCGEN_PLUGINS names it, and the side effect wires the slot.
set_pap_handler(_handler)


__all__ = ["_handler"]
