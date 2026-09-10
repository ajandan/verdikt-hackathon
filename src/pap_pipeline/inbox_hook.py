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
from docgen.inbox.agent import ProcessResult, set_pap_handler
from docgen.inbox.imap_poller import InboundMessage
from docgen.inbox.mailer import MailerError, send_reply
from docgen.store.models import InboxEvent, Tenant

from pap_pipeline.orchestrator import PipelineConfig, run
from pap_pipeline.schemas.decision import Decision
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


def _send_pap_reply(
    settings: Settings,
    msg: InboundMessage,
    decision: Decision,
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
        )
    except MailerError as e:
        log.warning("pap_reply_failed", error=str(e))
        return None


def _run_pipeline(attachments: list[bytes], program: str | None, settings: Settings) -> Decision:
    """Sync wrapper — the inbox actor is sync (asyncio.run per call is fine
    here; each call gets its own loop and NullPool means no cross-loop DB
    reuse)."""
    import asyncio

    config = _pipeline_config(settings)
    return asyncio.run(run(attachments, config=config, program=program))


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
        decision = _run_pipeline(attachments, program=program, settings=settings)
    except Exception as e:
        event.status = "failed"
        event.error = f"pap_pipeline_failed: {type(e).__name__}: {e}"
        session.commit()
        log.exception("pap_pipeline_failed", event_id=str(event.id))
        return ProcessResult(event.id, "failed", event.error)

    event.score = _score_from_decision(decision)
    event.review_state = _OUTCOME_TO_REVIEW_STATE.get(decision.outcome, "pending_review")
    event.status = "succeeded"
    session.commit()

    reply_id = _send_pap_reply(settings, msg, decision)
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
