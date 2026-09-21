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

from typing import Any

import structlog

from docgen.config import Settings
from docgen.inbox.agent import ProcessResult, set_pap_handler, set_pap_reply_sender
from docgen.inbox.imap_poller import InboundMessage
from docgen.inbox.mailer import Attachment, MailerError, send_reply
from docgen.store.blob import get_object
from docgen.store.models import InboxEvent, Program, Tenant
from pap_pipeline.assembler import (
    AssemblerError,
    AssemblerMissingBinding,
    AssemblyResult,
    assemble_reply_package,
)
from pap_pipeline.inbox_attachments import (
    attachments_from_message,
    persist_inbound_attachments,
)
from pap_pipeline.inbox_audit import (
    AI_ACTOR,
    SYSTEM_ACTOR,
    append_audit,
    append_stage_audit,
)
from pap_pipeline.orchestrator import run_with_intake
from pap_pipeline.pipeline_config import build_pipeline_config
from pap_pipeline.reply_render import (
    build_render_data,
    build_reply,
    extras_only,
    render_reply_attachment,
    reply_subject,
)
from pap_pipeline.rules import load_rules
from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule

log = structlog.get_logger(__name__)

# Every verdict outcome parks in pending_review — nothing goes back to the
# applicant until a human hits Approve on the pipeline detail page. A wrong
# auto-send (denial to an approvable case, approval to a fraudulent one)
# cannot be un-sent, so we prefer a queue over an outbox.
#
# ``routed`` is the exception: Triage classified the case as off-Decide
# (refill, appeal, missing-info-only) and dispatch is deterministic, so
# there is nothing for a HumanLoop reviewer to release. Map it to None
# to keep those cases out of the pending_review queue.
_OUTCOME_TO_REVIEW_STATE: dict[str, str | None] = {
    "approve": "pending_review",
    "deny": "pending_review",
    "missing_info": "pending_review",
    "routed": None,
}


def _score_from_decision(decision: Decision) -> float | None:
    """Highest citation score, scaled to 0–100 to match the template flow's
    review-page score column. None when no citation carries a score.

    Only rule-typed citations carry retrieval scores; history-typed
    citations (ADR-0004) don't and are skipped here.
    """
    scores = [
        c.score
        for c in decision.citations
        if c.type == "rule" and c.score is not None
    ]
    if not scores:
        return None
    return round(max(scores) * 100.0, 1)


def _draft_reply_artifact(
    session: Any,
    tenant: Tenant,
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    settings: Settings,
    *,
    extras: dict[str, Any] | None,
) -> str | None:
    """Return the persisted blob key for the reply PDF, or None on failure.

    Dispatches between two rendering modes based on program configuration:

      1. Program has a package bound to this outcome → run the assembler.
         The merged package (approval letter + guides + forms) becomes the
         reply attachment; a `doc_package_assemblies` row records the exact
         versions merged for audit + downstream retrieval.
      2. No binding, or binding lookup fails → fall back to the legacy
         single-template flow (`render_reply_attachment`). Kept so tenants
         that haven't declared outcomes/bindings yet still get a reply
         during the migration window.

    Any assembler failure other than "no binding" is logged and falls through
    to the legacy flow — losing the enrichment (static guides + forms) is
    preferable to blocking the reply entirely. Bindings are configuration;
    a config error shouldn't stall an approved case.
    """
    prog = _load_program_row(session, tenant, program)
    fallback_reason: str | None = None
    if prog is not None and (prog.package_bindings or {}).get(decision.outcome):
        try:
            data = build_render_data(intake, decision, program, event, extras=extras)
            result: AssemblyResult = assemble_reply_package(
                session,
                tenant=tenant,
                program=prog,
                decision=decision,
                event=event,
                render_data=data,
                settings=settings,
            )
        except AssemblerMissingBinding as e:
            # Shouldn't happen — we just checked the binding — but keep the
            # branch tidy for concurrent binding edits.
            log.warning(
                "pap_assembler_missing_binding",
                event_id=str(event.id),
                outcome=decision.outcome,
                error=str(e),
            )
            fallback_reason = f"assembler binding disappeared: {e}"
        except AssemblerError as e:
            # Item missing / render failed. Log with enough detail for the
            # operator to fix the package, then fall through to legacy.
            log.warning(
                "pap_assembler_failed",
                event_id=str(event.id),
                outcome=decision.outcome,
                error=f"{type(e).__name__}: {e}",
            )
            fallback_reason = f"{type(e).__name__}: {e}"
        else:
            return result.blob_key

    # When the assembler was expected but bailed, tell the reviewer.
    # Otherwise the timeline shows "reply drafted" and hides that the
    # enriched package (approval letter + guides + forms) degraded to
    # a bare single-template reply — the operator loses the signal that
    # the tenant's package config needs fixing.
    if fallback_reason is not None:
        append_audit(
            event,
            action="reply_assembler_degraded",
            actor=AI_ACTOR,
            note=(
                f"Package assembler failed for outcome={decision.outcome}; "
                f"falling back to single-template reply. Reason: {fallback_reason}"
            ),
        )

    _, blob_key = render_reply_attachment(
        session, tenant, intake, decision, program, event, settings,
        extras=extras,
    )
    return blob_key


def _load_program_row(
    session: Any, tenant: Tenant, program: str | None
) -> Program | None:
    """Look up the active Program row for this tenant + slug.

    Returns None when the slug is empty, the row doesn't exist, or the
    program is inactive. Callers that only need the extraction schema
    should still use :func:`_resolve_program_slots` (a thin wrapper).
    """
    slug = (program or "").strip().lower()
    if not slug:
        return None
    return (
        session.query(Program)
        .filter(
            Program.tenant_id == tenant.id,
            Program.slug == slug,
            Program.is_active.is_(True),
        )
        .first()
    )


def _resolve_program_slots(
    session: Any, tenant: Tenant, program: str | None
) -> list[dict[str, Any]] | None:
    """Look up the Program row's extraction_schema for this tenant + slug.

    Returns None when the program isn't registered (in which case the
    extract stage falls back to its `_DEFAULT_SLOTS`, keeping legacy
    tenants working during the Program cutover) or when the row exists
    but hasn't had its schema populated yet.
    """
    row = _load_program_row(session, tenant, program)
    if row is None or not row.extraction_schema:
        return None
    return list(row.extraction_schema)


def _run_pipeline(
    attachments: list[bytes],
    program: str | None,
    slots: list[dict[str, Any]] | None,
    rules: list[Rule] | None,
    settings: Settings,
    tenant: Tenant | None = None,
    program_row: Program | None = None,
    subject: str | None = None,
    stage_trace: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], Intake, Decision]:
    """Sync wrapper — the inbox actor is sync (each call gets its own
    event loop via :func:`run_pipeline_sync`, and NullPool means no
    cross-loop DB reuse). Returns the raw extraction dict, the Intake
    coerced from it, and the Decision — the raw dict is what the UI
    and reply mapper read to see program-specific slots that Intake
    doesn't carry.

    `rules` is the tenant/program-scoped corpus resolved from the DB (with
    a YAML fallback baked in). Passing it here rather than letting the
    retrieve stage load its own copy keeps the docgen.store boundary on
    the calling side — pap_pipeline stages never touch the DB directly.

    `tenant` flows into the pipeline config so the MDM adapter is picked
    from the tenant's `mdm_config` JSONB column (ADR-0004). The inbox
    handler always has a tenant; the JSON-in API path also resolves one
    before calling here.

    `program_row` (when passed) sources the per-program Extract / Reason
    model overrides. Callers that have DB access should resolve the row
    via ``_load_program_row`` and pass it in; callers without DB access
    (or when the program isn't registered) can leave it None to fall
    back to the settings-wide defaults.
    """
    from pap_pipeline._async_bridge import run_pipeline_sync

    config = build_pipeline_config(settings, tenant, program_row)
    return run_pipeline_sync(
        run_with_intake(
            attachments,
            config=config,
            program=program,
            tenant_id=str(tenant.id) if tenant else "",
            slots=slots,
            rules=rules,
            # Subject is the highest-precedence signal in the Triage classifier
            # (see triage._LLM_SYSTEM_PROMPT_TEMPLATE). Without forwarding it,
            # every production case would classify against extras + intake only.
            subject=subject,
            stage_trace=stage_trace,
        )
    )


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
    attachments = attachments_from_message(msg)
    if not attachments:
        event.status = "skipped"
        event.error = "pap: no attachments to process"
        append_audit(
            event,
            action="pipeline_skipped",
            actor=AI_ACTOR,
            note="No attachments on the inbound message — nothing to process.",
        )
        session.commit()
        log.info("pap_no_attachments", event_id=str(event.id))
        return ProcessResult(event.id, "skipped", event.error)

    event.status = "processing"
    event.attachment_name = msg.attachments[0].filename if msg.attachments else None
    # Persist raw inbound files up-front so the reviewer can preview them
    # even if the pipeline itself blows up mid-run.
    event.inbound_attachments = persist_inbound_attachments(event, msg, settings)
    program_row = _load_program_row(session, tenant, program)
    slots = list(program_row.extraction_schema) if program_row and program_row.extraction_schema else None
    # Resolve the rule corpus on this side of the boundary so pap_pipeline
    # stages stay ignorant of docgen.store. load_rules resolves the tenant's
    # library rules linked to this program via `program_rule_links`, and
    # falls through to the YAML pack when no Program row matches.
    rules = (
        load_rules(program, session=session, tenant_id=tenant.id)
        if program
        else None
    )
    schema_source = (
        f"program schema ({len(slots)} slots)" if slots else "built-in default schema"
    )
    rules_source = (
        f"program rules ({len(rules)} active)" if rules else "YAML fallback"
    )
    append_audit(
        event,
        action="pipeline_started",
        actor=AI_ACTOR,
        note=(
            f"Received {len(attachments)} attachment(s) from {event.from_addr}. "
            f"Program: {program or 'default'}. Extraction: {schema_source}. "
            f"Rules: {rules_source}."
        ),
    )
    session.commit()

    stage_trace: list[dict[str, Any]] = []
    try:
        raw_extraction, intake, decision = _run_pipeline(
            attachments,
            program=program,
            slots=slots,
            rules=rules,
            settings=settings,
            tenant=tenant,
            program_row=program_row,
            subject=msg.subject,
            stage_trace=stage_trace,
        )
    except Exception as e:
        event.status = "failed"
        event.error = f"pap_pipeline_failed: {type(e).__name__}: {e}"
        # Emit whatever stage traces completed before the failure so the
        # audit shows how far the pipeline got.
        append_stage_audit(event, stage_trace)
        append_audit(
            event,
            action="pipeline_failed",
            actor=AI_ACTOR,
            note=f"{type(e).__name__}: {e}",
        )
        session.commit()
        log.exception("pap_pipeline_failed", event_id=str(event.id))
        return ProcessResult(event.id, "failed", event.error)

    event.score = _score_from_decision(decision)
    event.review_state = _OUTCOME_TO_REVIEW_STATE.get(decision.outcome, "pending_review")
    event.status = "succeeded"
    event.pap_intake = intake.model_dump(mode="json")
    # Persist the full extraction dict so the pipeline detail page and reply
    # mapper can see program-specific slots that don't live on Intake.
    event.pap_extraction = raw_extraction
    event.pap_decision = decision.model_dump(mode="json")
    # Emit one audit entry per pipeline stage — model id + wall-clock —
    # BEFORE the aggregate verdict entry so the timeline reads in
    # execution order: extract → triage → retrieve → mdm → reason → verdict.
    append_stage_audit(event, stage_trace)
    critical_findings = [
        f for f in decision.verifier_findings if f.severity == "critical"
    ]
    verdict_note = (
        f"Outcome: {decision.outcome}. "
        f"Citations: {len(decision.citations)}, "
        f"missing fields: {len(decision.missing_fields)}. "
    )
    if critical_findings:
        types = sorted({f.type for f in critical_findings})
        verdict_note += (
            f"Verifier flagged {len(critical_findings)} issue(s) "
            f"({', '.join(types)}). "
        )
    verdict_note += "Queued for human review."
    append_audit(
        event,
        action="verdict_rendered",
        actor=AI_ACTOR,
        note=verdict_note,
    )
    session.commit()

    # Draft the reply artifact now so the reviewer previews the exact bytes
    # that will be sent. Two paths — package assembler wins when the program
    # has an outcome→package binding, else we fall back to the single-template
    # legacy flow so tenants that haven't migrated yet still get a reply.
    blob_key = _draft_reply_artifact(
        session, tenant, intake, decision, program, event, settings,
        extras=extras_only(raw_extraction),
    )
    if blob_key:
        event.reply_attachment_blob_key = blob_key
        append_audit(
            event,
            action="reply_drafted",
            actor=AI_ACTOR,
            note=f"Drafted {decision.outcome} reply and stored for reviewer preview.",
        )
        session.commit()
    else:
        append_audit(
            event,
            action="reply_draft_skipped",
            actor=AI_ACTOR,
            note="No outcome-matched template or package found — reply will be text-only when sent.",
        )
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
                exc_info=True,
            )
        else:
            filename = event.reply_attachment_blob_key.rsplit("/", 1)[-1]
            attachment = Attachment(
                filename=filename, content=body, content_type="application/pdf"
            )

    if not settings.smtp_host:
        raise MailerError("SMTP not configured (DOCGEN_SMTP_HOST is empty)")
    outcome, text, html = build_reply(decision)
    reply_msg_id = send_reply(
        settings=settings,
        to_addrs=[event.from_addr],
        cc_addrs=list(event.cc_addrs or []),
        subject=reply_subject(event.subject, outcome),
        body_text=text,
        body_html=html,
        in_reply_to=event.message_id,
        references=[event.message_id],
        attachments=[attachment] if attachment else None,
    )
    append_audit(
        event,
        action="reply_sent",
        actor=SYSTEM_ACTOR,
        note=(
            f"Sent {outcome} reply to {event.from_addr}"
            + (f" with attachment {attachment.filename}" if attachment else " (text-only)")
            + "."
        ),
    )
    # Caller commits — this helper is invoked inside the approve endpoint's
    # sync worker which owns the transaction boundary.
    return reply_msg_id


# Register at import time — docgen.plugins.load_plugins() imports this module
# when DOCGEN_PLUGINS names it, and the side effect wires both slots.
set_pap_handler(_handler)
set_pap_reply_sender(_send_pap_reply_for_event)


__all__ = ["_handler", "_send_pap_reply_for_event"]
