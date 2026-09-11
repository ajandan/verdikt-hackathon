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
from docgen.inbox.agent import ProcessResult, set_pap_handler, set_pap_reply_sender
from docgen.inbox.imap_poller import InboundMessage
from docgen.inbox.mailer import Attachment, MailerError, send_reply
from docgen.store.blob import get_object, put_object
from docgen.store.models import InboxEvent, Template, TemplateVersion, Tenant

from pap_pipeline.orchestrator import PipelineConfig, run_with_intake
from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig

log = structlog.get_logger(__name__)

# Every pipeline outcome parks in pending_review — nothing goes back to the
# applicant until a human hits Approve on the pipeline detail page. A wrong
# auto-send (denial to an approvable case, approval to a fraudulent one)
# cannot be un-sent, so we prefer a queue over an outbox.
_OUTCOME_TO_REVIEW_STATE = {
    "approve": "pending_review",
    "deny": "pending_review",
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


# InboundAttachment.kind is docx|pdf; the poller filters everything else,
# so this map covers the only two we ever land on. Falls back to the
# generic bytestream MIME so a future third kind still gets served.
_KIND_TO_CONTENT_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _persist_inbound_attachments(
    event: InboxEvent, msg: InboundMessage, settings: Settings
) -> list[dict[str, Any]]:
    """Upload each inbound attachment to the documents bucket and return a
    manifest the UI can render as preview links.

    Blob-store failures don't abort the pipeline — a missing preview is less
    bad than dropping the claim. Each failed upload just gets its blob_key
    left null in the manifest so the UI renders it as "(upload failed)".
    """
    manifest: list[dict[str, Any]] = []
    for idx, a in enumerate(msg.attachments):
        if not a.content:
            continue
        content_type = _KIND_TO_CONTENT_TYPE.get(a.kind, "application/octet-stream")
        safe_name = (a.filename or f"attachment-{idx}").replace("/", "_").replace("\\", "_")
        blob_key = f"inbox-inbound/{event.id}/{idx:02d}-{safe_name}"
        entry: dict[str, Any] = {
            "filename": a.filename or safe_name,
            "content_type": content_type,
            "size": len(a.content),
            "blob_key": None,
        }
        try:
            put_object(
                settings.s3_bucket_documents,
                blob_key,
                a.content,
                content_type=content_type,
            )
            entry["blob_key"] = blob_key
        except Exception as e:
            log.warning(
                "pap_inbound_attachment_persist_failed",
                event_id=str(event.id),
                filename=a.filename,
                error=f"{type(e).__name__}: {e}",
            )
        manifest.append(entry)
    return manifest


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
    tv = _find_reply_template(session, tenant, decision.outcome)
    if tv is None:
        log.warning("pap_reply_template_missing", outcome=decision.outcome, tenant=str(tenant.id))
        return None, None
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
        return None, None
    filename = f"alderwood_{decision.outcome}_{_short_id(event.id)}.pdf"
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
        )
        return attachment, None
    return attachment, blob_key


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
    # Persist raw inbound files up-front so the reviewer can preview them
    # even if the pipeline itself blows up mid-run.
    event.inbound_attachments = _persist_inbound_attachments(event, msg, settings)
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

    # Render + persist the reply PDF now so the reviewer can preview the
    # exact letter that will be sent. The send itself is deferred to the
    # human-approval endpoint — see _send_pap_reply_for_event.
    _, blob_key = _render_reply_attachment(
        session, tenant, intake, decision, program, event, settings
    )
    if blob_key:
        event.reply_attachment_blob_key = blob_key
        session.commit()

    log.info(
        "pap_decision",
        event_id=str(event.id),
        outcome=decision.outcome,
        citations=len(decision.citations),
        missing=len(decision.missing_fields),
    )
    return ProcessResult(event.id, "succeeded", decision.outcome)


def _send_pap_reply_for_event(
    session: Any, event: InboxEvent, settings: Settings
) -> str | None:
    """Send the queued reply after a human approves the pipeline verdict.

    Reconstructs the reply from fields persisted on the event (decision,
    from/cc/subject, previously rendered PDF blob). Called by the UI's
    /pipeline/approve endpoint via the docgen reply-sender slot.

    Raises MailerError on SMTP failure; the caller renders that back to
    the reviewer so they know the send didn't happen.
    """
    if event.pap_decision is None:
        raise MailerError("event has no pipeline decision to reply about")
    decision = Decision.model_validate(event.pap_decision)

    attachment: Attachment | None = None
    if event.reply_attachment_blob_key:
        try:
            body = get_object(
                settings.s3_bucket_documents, event.reply_attachment_blob_key
            )
        except Exception as e:
            # Log and continue with a text-only reply. Losing the PDF is
            # bad but not as bad as blocking the approved send entirely.
            log.warning(
                "pap_reply_attachment_fetch_failed",
                event_id=str(event.id),
                blob_key=event.reply_attachment_blob_key,
                error=f"{type(e).__name__}: {e}",
            )
        else:
            filename = event.reply_attachment_blob_key.rsplit("/", 1)[-1]
            attachment = Attachment(
                filename=filename, content=body, content_type="application/pdf"
            )

    if not settings.smtp_host:
        raise MailerError("SMTP not configured (DOCGEN_SMTP_HOST is empty)")
    outcome, text, html = _build_reply(decision)
    return send_reply(
        settings=settings,
        to_addrs=[event.from_addr],
        cc_addrs=list(event.cc_addrs or []),
        subject=_reply_subject(event.subject, outcome),
        body_text=text,
        body_html=html,
        in_reply_to=event.message_id,
        references=[event.message_id],
        attachments=[attachment] if attachment else None,
    )


# Register at import time — docgen.plugins.load_plugins() imports this module
# when DOCGEN_PLUGINS names it, and the side effect wires both slots.
set_pap_handler(_handler)
set_pap_reply_sender(_send_pap_reply_for_event)


__all__ = ["_handler", "_send_pap_reply_for_event"]
