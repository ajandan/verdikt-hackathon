"""Inbox agent orchestrator: one InboundMessage → one InboxEvent lifecycle.

Runs synchronously inside a Dramatiq actor so it can reuse the render
pipeline's sync_session pattern. Every branch (miss on tenant, missing
attachment, mapping failure, render failure, high score, low score)
lands the same way: an InboxEvent row is written with a terminal status,
and a reply email is sent (when we can identify a recipient) so the
sender always gets a signal — success or otherwise.

Score model (intentionally simple; documented so operators can tune):
  base = 100 * (ai_covered_fields / total_fields) when both freeze and
         render succeed. Freeze failure → 0. Render failure → cap 50.
  Rationale: "orphan_backfill" suggestions are placeholders the AI
  missed and a human should confirm, so they drag the score down.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from docgen.config import Settings, get_settings
from docgen.core.render_pipeline import execute_merge
from docgen.designtime.audit import record_audit_event
from docgen.designtime.freeze import freeze_pdf_version, freeze_version
from docgen.designtime.mapper import propose_mapping_for_version
from docgen.designtime.publish import publish_version
from docgen.designtime.upload import upload_pdf_template, upload_template
from docgen.inbox.imap_poller import InboundAttachment, InboundMessage
from docgen.inbox.mailer import Attachment, MailerError, send_reply
from docgen.inbox.replies import (
    find_parent_event,
    parse_decision,
    sender_matches,
)
from docgen.inbox.resolver import (
    auto_approve_threshold,
    notify_addresses,
    resolve_tenant,
)
from docgen.inbox.synth import synth_payload
from docgen.inbox.test_executor import ExecutedTrial, execute_test_matrix
from docgen.providers.base import MappingProposal
from docgen.store.models import (
    InboxEvent,
    Template,
    TemplateVersion,
    Tenant,
)


def _run_async(coro):
    """Run an async coroutine from the sync actor body.

    asyncio.run creates + tears down a loop per call. Fine here — the
    async DB engine uses NullPool, so no cross-loop connection reuse.
    """
    return asyncio.run(coro)

log = structlog.get_logger(__name__)


@dataclass
class ProcessResult:
    event_id: uuid.UUID
    status: str  # succeeded | failed | skipped
    detail: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_BODY_TEXT_MAX = 8 * 1024  # 8 KB — enough for a legible preview, not enough to bloat the row


def _truncate_body(text: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= _BODY_TEXT_MAX:
        return text
    return text[:_BODY_TEXT_MAX] + "\n\n[truncated]"


def _pick_attachment(msg: InboundMessage) -> InboundAttachment | None:
    # First accepted attachment wins. Multi-attachment support is Phase 3+.
    return msg.attachments[0] if msg.attachments else None


def _template_name_from(msg: InboundMessage, filename: str) -> str:
    # Prefer the attachment filename (sender's intent) over the subject.
    base = filename.rsplit(".", 1)[0].strip()
    return base or (msg.subject or "Inbox template").strip()[:255]


# Raw AcroForm widget names Acrobat/writers auto-assign when a form field
# has no explicit /T entry. When our vision labeler misses a field, the
# placement's label stays as the AcroForm name — a signal for the scorer
# that this field wasn't successfully human-labeled.
_ACROFORM_DEFAULT_LABEL_RE = re.compile(
    r"^(text field|check\s*box|choice box|combo box|list box|"
    r"radio button|signature|button|field)\s*\d*$",
    re.IGNORECASE,
)


def _is_default_label(name: str | None, description: str | None) -> bool:
    """True when a PDF acroform field's description is still a raw default.

    Only fires on positive detection — a description that matches the
    "Text Field 158" acroform auto-name pattern, or a description that
    equals a similarly-shaped name. Docx placeholders (`{{ member_name }}`)
    have no separate description because the token itself is the label, so
    empty descriptions are neutral, not a miss.
    """
    desc = (description or "").strip()
    if not desc:
        return False
    if _ACROFORM_DEFAULT_LABEL_RE.match(desc):
        return True
    return desc == (name or "").strip() and bool(
        _ACROFORM_DEFAULT_LABEL_RE.match(desc)
    )


def _score(proposal: MappingProposal, *, render_ok: bool, freeze_ok: bool) -> float:
    """Compute a 0-100 confidence score for the ingested template.

    Only a fully-labeled + successfully-rendered template earns 100. Any
    field the AI missed (raw AcroForm name still in the description)
    drags the score down proportionally, which — combined with the 100
    auto-approve threshold — forces human review whenever the labeler
    didn't get every field.
    """
    if not freeze_ok:
        return 0.0
    total = len(proposal.suggestions)
    if total == 0:
        base = 0.0
    else:
        labeled = sum(
            1
            for s in proposal.suggestions
            if s.source == "ai"
            and not _is_default_label(s.field.name, s.field.description)
        )
        base = 100.0 * (labeled / total)
    if not render_ok:
        base = min(base, 50.0)
    return round(base, 2)


def _record_event_row(
    session: Session,
    *,
    msg: InboundMessage,
    to_addr: str | None,
) -> InboxEvent:
    """Insert the initial pending row. Unique(message_id) makes this idempotent."""
    row = InboxEvent(
        message_id=msg.message_id,
        from_addr=msg.from_addr,
        to_addr=to_addr,
        cc_addrs=list(msg.cc_addrs),
        subject=(msg.subject or "")[:998],
        received_at=msg.received_at,
        status="pending",
        body_text=_truncate_body(msg.body_text),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


async def _draw_pdf_placements(
    session,
    *,
    tv_id: uuid.UUID,
    pdf_bytes: bytes,
) -> None:
    """Synthesize placements for a flat PDF via the vision detector.

    Mirrors the manual `/versions/{id}/placements/ai-draw` UI endpoint —
    for PDFs with no AcroForm widgets, upload_pdf_template leaves the
    placement list empty, which would otherwise trip freeze's
    "no field placements" reject. Vision detects field boxes on each
    page and writes them so the downstream labeler + freeze see the same
    shape as an AcroForm PDF.

    Best-effort: no backend or a failed detection leaves placements empty
    and the caller falls through to the existing freeze-fails path.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from docgen.designtime.pdf_field_detector import detect_fields_in_pdf
    from docgen.designtime.pdf_vision_backend import pick_pdf_vision_backend

    settings = get_settings()
    backend = pick_pdf_vision_backend(settings)
    if backend is None:
        log.info("inbox_pdf_ai_draw_skipped", reason="no_vision_backend")
        return

    tv = await session.get(TemplateVersion, tv_id)
    if tv is None or tv.pdf_flavor != "flat":
        return
    existing = list((tv.pdf_field_placements or {}).get("placements") or [])
    if existing:
        return

    try:
        boxes = await detect_fields_in_pdf(
            pdf_bytes,
            api_key=backend["api_key"],
            chat_model=backend["chat_model"],
            timeout=backend["timeout"],
            base_url=backend["base_url"],
            extra_body=backend["extra_body"],
        )
    except Exception as e:
        log.warning(
            "inbox_pdf_ai_draw_failed",
            version_id=str(tv_id),
            provider=backend["provider"],
            error=f"{type(e).__name__}: {e}",
        )
        return

    placements: list[dict] = []
    for i, b in enumerate(boxes, start=1):
        ftype = str(b.get("field_type") or "text")
        placements.append({
            "name": f"field_{i}",
            "page": int(b["page"]),
            "x_pct": float(b["x_pct"]),
            "y_pct": float(b["y_pct"]),
            "w_pct": float(b["w_pct"]),
            "h_pct": float(b["h_pct"]),
            "field_type": ftype,
            "payload_type": "boolean" if ftype == "checkbox" else "string",
        })

    if not placements:
        log.info(
            "inbox_pdf_ai_draw_empty",
            version_id=str(tv_id),
            provider=backend["provider"],
        )
        return

    stored = dict(tv.pdf_field_placements or {})
    stored["placements"] = placements
    tv.pdf_field_placements = stored
    flag_modified(tv, "pdf_field_placements")
    await session.commit()
    log.info(
        "inbox_pdf_ai_drawn",
        version_id=str(tv_id),
        provider=backend["provider"],
        detected=len(placements),
    )


async def _label_pdf_placements(
    session,
    *,
    tv_id: uuid.UUID,
    pdf_bytes: bytes,
) -> None:
    """Run the vision labeler over the freshly-uploaded PDF's placements.

    Best-effort: any error (vision provider unconfigured, HTTP timeout,
    JSON parse failure) is logged and swallowed — freeze proceeds with
    raw AcroForm field names as labels. Mutates tv.pdf_field_placements
    in-place with `label` (and optional `section`) so `_pdf_field_spec`
    at freeze time carries them into `FieldSpec.description`.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from docgen.designtime.pdf_field_detector import label_fields_in_pdf
    from docgen.designtime.pdf_payload_key import shorten_payload_key, slugify_key
    from docgen.designtime.pdf_vision_backend import pick_pdf_vision_backend

    settings = get_settings()
    backend = pick_pdf_vision_backend(settings)
    if backend is None:
        log.info("inbox_pdf_ai_label_skipped", reason="no_vision_backend")
        return

    tv = await session.get(TemplateVersion, tv_id)
    if tv is None or not tv.pdf_field_placements:
        return
    placements: list[dict] = list(tv.pdf_field_placements.get("placements") or [])
    if not placements:
        return

    # label_fields_in_pdf keys results by the caller-supplied `index`, so
    # feed enumeration indices that line up with the placements list.
    fields = [
        {
            "index": i,
            "name": str(p.get("name") or ""),
            "page": int(p.get("page") or 1),
            "x_pct": float(p.get("x_pct") or 0),
            "y_pct": float(p.get("y_pct") or 0),
            "w_pct": float(p.get("w_pct") or 0),
            "h_pct": float(p.get("h_pct") or 0),
            "field_type": p.get("field_type"),
        }
        for i, p in enumerate(placements)
        if p.get("name")
    ]
    if not fields:
        return

    try:
        labels_by_index = await label_fields_in_pdf(
            pdf_bytes,
            fields,
            api_key=backend["api_key"],
            ocr_model=backend["ocr_model"],
            chat_model=backend["chat_model"],
            timeout=backend["timeout"],
            pdf_flavor=tv.pdf_flavor,
            base_url=backend["base_url"],
            ocr_via_vision_chat=backend["ocr_via_vision_chat"],
            ocr_concurrency=backend["ocr_concurrency"],
            extra_body=backend["extra_body"],
        )
    except Exception as e:
        log.warning(
            "inbox_pdf_ai_label_failed",
            version_id=str(tv_id),
            provider=backend["provider"],
            error=f"{type(e).__name__}: {e}",
        )
        return

    changed = 0
    for idx, info in labels_by_index.items():
        if 0 <= idx < len(placements):
            label = str(info.get("label") or "").strip()
            if label:
                placements[idx]["label"] = label
                changed += 1
            section = str(info.get("section") or "").strip()
            if section:
                placements[idx]["section"] = section

    if changed == 0:
        log.info(
            "inbox_pdf_ai_label_empty",
            version_id=str(tv_id),
            provider=backend["provider"],
            total=len(fields),
        )
        return

    # Rename each labeled placement's `name` (the payload key that
    # becomes the frozen schema field name) to a compact snake_case slug
    # built from section + label — mirrors the manual UI's "AI Label
    # fields" button. Placements without an AI label keep their raw
    # AcroForm name and are pre-seeded into `taken` so the slugger
    # doesn't collide with them.
    labeled_idx = {i for i in labels_by_index if 0 <= i < len(placements)}
    taken: set[str] = {
        str(p.get("name") or "")
        for i, p in enumerate(placements)
        if i not in labeled_idx and p.get("name")
    }
    renamed = 0
    for idx, info in labels_by_index.items():
        if not (0 <= idx < len(placements)):
            continue
        label = str(info.get("label") or "").strip()
        if not label:
            continue
        section = str(info.get("section") or "").strip()
        new_name = shorten_payload_key(
            slugify_key(section), slugify_key(label), taken
        )
        if placements[idx].get("name") != new_name:
            placements[idx]["name"] = new_name
            renamed += 1
        taken.add(new_name)

    tv.pdf_field_placements["placements"] = placements
    flag_modified(tv, "pdf_field_placements")
    await session.commit()
    log.info(
        "inbox_pdf_ai_labeled",
        version_id=str(tv_id),
        provider=backend["provider"],
        labeled=changed,
        renamed=renamed,
        total=len(fields),
    )


def _run_async_pipeline(
    *,
    tenant_id: uuid.UUID,
    attachment: InboundAttachment,
    msg: InboundMessage,
) -> tuple[Template, TemplateVersion, MappingProposal, bool]:
    """Upload → propose_mapping → freeze. Returns (template, tv, proposal, freeze_ok).

    The design-time pipeline is async (SQLAlchemy AsyncSession); the actor
    is sync. We hop into an event loop with run_async so the actor stays
    a straight-line sync body.
    """
    async def _work():
        # Local imports would be circular at module top; the async_session
        # helper is imported inside to keep this file's static imports narrow.
        from docgen.store.db import async_session

        async with async_session() as session:
            owner = msg.from_addr
            name = _template_name_from(msg, attachment.filename)
            if attachment.kind == "pdf":
                template, tv, _report = await upload_pdf_template(
                    session,
                    name=name,
                    pdf_bytes=attachment.content,
                    tenant_id=tenant_id,
                    owner=owner,
                )
            else:
                template, tv, _report = await upload_template(
                    session,
                    name=name,
                    docx_bytes=attachment.content,
                    tenant_id=tenant_id,
                    owner=owner,
                )

        # Mapping proposal + freeze run in a fresh session (each call
        # commits internally). Same session would double-flush the tv row.
        proposal: MappingProposal
        freeze_ok = True
        async with async_session() as session:
            if attachment.kind == "pdf":
                # PDF pipeline derives schema from placements — no mapping
                # proposal needed. Two vision steps mirror what the manual
                # UI offers on the placement editor:
                #   (a) `_draw_pdf_placements` — flat PDF only, synthesizes
                #       boxes so the freeze step has something to work with
                #       (equivalent of the "AI Draw" button).
                #   (b) `_label_pdf_placements` — labels every placement so
                #       FieldSpec.description gets a human caption (drives
                #       synth_payload realism + score-vs-threshold gating).
                # Both are best-effort: a vision failure falls back to the
                # empty-placements freeze reject, same as today.
                await _draw_pdf_placements(
                    session, tv_id=tv.id, pdf_bytes=attachment.content
                )
                await _label_pdf_placements(
                    session, tv_id=tv.id, pdf_bytes=attachment.content
                )

                from docgen.core.schema_infer import FieldSpec
                from docgen.providers.base import Suggestion

                try:
                    tv = await freeze_pdf_version(session, version_id=tv.id, actor=owner)
                except Exception as e:
                    freeze_ok = False
                    log.warning(
                        "inbox_freeze_failed", version_id=str(tv.id), error=str(e)
                    )
                # Build the proposal from the *frozen* schema — that's the
                # only place FieldSpec.description is populated from the
                # placement labels, and _score reads description to detect
                # unlabeled fields. Building this before freeze scores 0
                # for every ingest because pre-freeze tv.schema is just
                # name+type with no description.
                schema_fields = tv.schema.get("fields", []) if tv.schema else []
                proposal = MappingProposal(
                    suggestions=[
                        Suggestion(
                            placeholder=f["name"],
                            jinja="{{ " + f["name"] + " }}",
                            field=FieldSpec.model_validate(f),
                            source="ai",
                        )
                        for f in schema_fields
                    ]
                )
            else:
                try:
                    proposal = await propose_mapping_for_version(session, tv.id)
                except Exception:
                    proposal = MappingProposal(suggestions=[])
                    freeze_ok = False
                    log.exception("inbox_mapping_failed", version_id=str(tv.id))
                if freeze_ok:
                    try:
                        tv = await freeze_version(
                            session, version_id=tv.id, proposal=proposal, actor=owner
                        )
                    except Exception as e:
                        freeze_ok = False
                        log.warning(
                            "inbox_freeze_failed",
                            version_id=str(tv.id),
                            error=str(e),
                        )
        return template, tv, proposal, freeze_ok

    return _run_async(_work())


def _test_render(tv: TemplateVersion, *, settings: Settings) -> tuple[bytes | None, str, str | None]:
    """One-shot test render — used as a fallback when the multi-test matrix
    produces no trials (e.g. schema-invalid). Returns (bytes, ext, error)."""
    try:
        payload = synth_payload(tv.schema)
    except Exception as e:
        return None, "docx", f"synth_failed: {e}"
    output_format = "pdf" if tv.pdf_flavor is not None else "docx"
    try:
        rendered = execute_merge(tv, payload, output_format, settings=settings)
    except Exception as e:
        return None, output_format, f"render_failed: {type(e).__name__}: {e}"
    ext = "pdf" if output_format == "pdf" else "docx"
    return rendered, ext, None


def _build_reply_subject(original: str | None) -> str:
    base = (original or "").strip() or "Template ingest"
    if base.lower().startswith("re:"):
        return base
    return f"Re: {base}"


def _html_escape(value: str) -> str:
    """Minimal escape for values that land inline in the reply HTML."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_reply_body(
    *,
    tenant_name: str,
    template_name: str,
    template_id: uuid.UUID,
    template_version_id: uuid.UUID,
    score: float,
    threshold: float,
    auto_approved: bool,
    render_error: str | None,
    freeze_ok: bool,
    admin_link: str | None,
    template_link: str | None,
    trials: list[ExecutedTrial],
    trial_download_urls: dict[int, str | None],
) -> tuple[str, str]:
    """Return (plain_text, html) reply bodies.

    `trials` and `trial_download_urls` drive the AI test-results table
    that lets the reviewer see per-trial verdicts + downloads inline.
    Downloads are optional (None when the deploy has no public_base_url
    to build absolute URLs against, or when the trial's artifact wasn't
    persisted).
    """
    if not freeze_ok:
        headline = "Template ingest failed — could not freeze the mapping."
    elif render_error:
        headline = f"Template ingested but the test render failed: {render_error}"
    elif auto_approved:
        headline = (
            f"Template ingested and auto-published "
            f"(score {score:.1f} >= threshold {threshold:.1f})."
        )
    else:
        headline = (
            f"Template ingested and awaiting review "
            f"(score {score:.1f} < threshold {threshold:.1f})."
        )

    passed_count = sum(1 for t in trials if t.passed)
    trial_summary = (
        f"{passed_count}/{len(trials)} trials passed" if trials else None
    )

    lines = [
        headline,
        "",
        f"Tenant:            {tenant_name}",
        f"Template:          {template_name}",
        f"Template ID:       {template_id}",
        f"Version ID:        {template_version_id}",
        f"Confidence score:  {score:.1f}",
        f"Auto-publish at:   {threshold:.1f}",
    ]
    if trial_summary:
        lines.append(f"AI test summary:   {trial_summary}")
    if template_link:
        lines += ["", f"Open the template: {template_link}"]
    if admin_link:
        lines += [f"Review the ingest: {admin_link}"]
    if trials:
        lines += ["", "AI test trials:"]
        for t in trials:
            status = "PASS" if t.passed else "FAIL"
            lines.append(f"  {t.ordinal}. [{status}] {t.name} — {t.verdict}")
            download_url = trial_download_urls.get(t.ordinal)
            if download_url:
                lines.append(f"     Download: {download_url}")
    lines += [
        "",
        "A test-rendered document is attached so you can spot-check the field mapping.",
        "Reply APPROVE to publish, or REJECT to discard.",
    ]
    text = "\n".join(lines)

    html_lines = [f"<p>{_html_escape(headline)}</p>", "<ul>"]
    html_lines.append(
        f"<li><strong>Tenant:</strong> {_html_escape(tenant_name)}</li>"
    )
    html_lines.append(
        f"<li><strong>Template:</strong> {_html_escape(template_name)}</li>"
    )
    html_lines.append(
        f"<li><strong>Template ID:</strong> <code>{template_id}</code></li>"
    )
    html_lines.append(
        f"<li><strong>Version ID:</strong> <code>{template_version_id}</code></li>"
    )
    html_lines.append(
        f"<li><strong>Score:</strong> {score:.1f} "
        f"(auto-publish threshold {threshold:.1f})</li>"
    )
    if trial_summary:
        html_lines.append(
            f"<li><strong>AI tests:</strong> {_html_escape(trial_summary)}</li>"
        )
    if template_link:
        html_lines.append(
            f'<li><a href="{_html_escape(template_link)}">Open the template</a></li>'
        )
    if admin_link:
        html_lines.append(
            f'<li><a href="{_html_escape(admin_link)}">Review the ingest</a></li>'
        )
    html_lines.append("</ul>")

    if trials:
        html_lines.append(_build_trials_table_html(trials, trial_download_urls))

    html_lines.append(
        "<p>The first test render is attached. Reply <b>APPROVE</b> to publish "
        "or <b>REJECT</b> to discard.</p>"
    )
    return text, "".join(html_lines)


def _build_trials_table_html(
    trials: list[ExecutedTrial],
    trial_download_urls: dict[int, str | None],
) -> str:
    """Reply-friendly summary table of AI test trials.

    Uses inline styles because email clients strip <style> blocks; the
    color choices mirror the admin portal's success/danger palette so
    reviewers get the same visual language in both surfaces.
    """
    parts: list[str] = []
    parts.append("<h3 style=\"margin:20px 0 8px;font-size:15px\">AI-planned test trials</h3>")
    parts.append(
        "<table cellspacing=\"0\" cellpadding=\"6\" "
        "style=\"border-collapse:collapse;border:1px solid #d0d7de;"
        "font-family:-apple-system,Segoe UI,sans-serif;font-size:13px;width:100%\">"
    )
    parts.append(
        "<thead><tr style=\"background:#f6f8fa;text-align:left\">"
        "<th style=\"border-bottom:1px solid #d0d7de;padding:8px\">#</th>"
        "<th style=\"border-bottom:1px solid #d0d7de;padding:8px\">Trial</th>"
        "<th style=\"border-bottom:1px solid #d0d7de;padding:8px\">Result</th>"
        "<th style=\"border-bottom:1px solid #d0d7de;padding:8px\">AI verdict</th>"
        "<th style=\"border-bottom:1px solid #d0d7de;padding:8px\">Download</th>"
        "</tr></thead><tbody>"
    )
    for t in trials:
        badge_color = "#1a7f37" if t.passed else "#cf222e"
        badge_bg = "#dafbe1" if t.passed else "#ffebe9"
        badge_text = "PASS" if t.passed else "FAIL"
        url = trial_download_urls.get(t.ordinal)
        if url:
            dl = (
                f'<a href="{_html_escape(url)}" '
                f'style="color:#0969da;text-decoration:none">Download</a>'
            )
        else:
            dl = "<span style=\"color:#57606a\">—</span>"
        parts.append(
            "<tr>"
            f"<td style=\"border-bottom:1px solid #eaeef2;padding:8px;vertical-align:top\">{t.ordinal}</td>"
            f"<td style=\"border-bottom:1px solid #eaeef2;padding:8px;vertical-align:top\">"
            f"<strong>{_html_escape(t.name)}</strong>"
            + (f"<br><span style=\"color:#57606a;font-size:12px\">{_html_escape(t.description)}</span>" if t.description else "")
            + "</td>"
            f"<td style=\"border-bottom:1px solid #eaeef2;padding:8px;vertical-align:top\">"
            f"<span style=\"background:{badge_bg};color:{badge_color};padding:2px 8px;"
            f"border-radius:10px;font-weight:600;font-size:11.5px\">{badge_text}</span></td>"
            f"<td style=\"border-bottom:1px solid #eaeef2;padding:8px;vertical-align:top\">"
            f"{_html_escape(t.verdict) if t.verdict else '<span style=\"color:#57606a\">—</span>'}</td>"
            f"<td style=\"border-bottom:1px solid #eaeef2;padding:8px;vertical-align:top\">{dl}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def _trial_download_url(
    settings: Settings, test_result_id: uuid.UUID
) -> str | None:
    """Absolute signed URL for a persisted trial artifact, or None if the
    deployment has no public base URL to hang it off.

    Signing is safe here — the token binds to the test_result_id and the
    route re-loads the row for authorization. Missing base URL yields
    None so the email skips the link rather than emitting a broken /path.
    """
    from docgen.auth import sign_inbox_test_token

    base = (settings.public_base_url or settings.admin_base_url or "").rstrip("/")
    if not base:
        return None
    token = sign_inbox_test_token(str(test_result_id))
    return f"{base}/downloads/inbox-test/{test_result_id}?token={token}"


def _build_reject_body(reason: str) -> tuple[str, str]:
    text = (
        "Your email was received but could not be processed.\n\n"
        f"Reason: {reason}\n\n"
        "Ensure the message includes a .docx or .pdf attachment and is sent from an "
        "address on the tenant's approved sender list."
    )
    html = f"<p>Your email was received but could not be processed.</p><p><b>Reason:</b> {reason}</p>"
    return text, html


def _admin_link_for(
    settings: Settings, event_id: uuid.UUID
) -> str | None:
    base = (settings.admin_base_url or settings.public_base_url or "").rstrip("/")
    if not base:
        return None
    return f"{base}/ui/inbox/{event_id}"


def _template_link_for(
    settings: Settings, template_id: uuid.UUID
) -> str | None:
    """Deep-link to the template detail page in the admin portal.

    The sender lands here after login (portal auth gates /ui/*), so they
    can jump straight into the template they just ingested without
    hunting for it in the list.
    """
    base = (settings.admin_base_url or settings.public_base_url or "").rstrip("/")
    if not base:
        return None
    return f"{base}/ui/templates/{template_id}"


def process_message(
    session: Session,
    *,
    msg: InboundMessage,
    settings: Settings | None = None,
) -> ProcessResult:
    """Run the full ingest-to-reply pipeline for one inbound message."""
    settings = settings or get_settings()

    to_addr = msg.to_addrs[0] if msg.to_addrs else None
    event = _record_event_row(session, msg=msg, to_addr=to_addr)

    tenants = list(session.query(Tenant).all())
    match = resolve_tenant(tenants, from_addr=msg.from_addr, subject=msg.subject)
    if match is None:
        event.status = "skipped"
        event.error = "no matching tenant (sender_domain or subject_tag)"
        session.commit()
        log.info("inbox_no_tenant", from_addr=msg.from_addr, subject=msg.subject)
        _try_send_reject_reply(msg, settings=settings, reason=event.error)
        return ProcessResult(event.id, "skipped", event.error)

    tenant = match.tenant
    event.tenant_id = tenant.id
    session.commit()

    attachment = _pick_attachment(msg)
    if attachment is None:
        event.status = "skipped"
        event.error = "no .docx or .pdf attachment"
        session.commit()
        _try_send_reject_reply(msg, settings=settings, reason=event.error)
        return ProcessResult(event.id, "skipped", event.error)

    event.attachment_name = attachment.filename
    event.attachment_hash = _sha256(attachment.content)
    event.status = "processing"
    session.commit()

    try:
        template, tv, proposal, freeze_ok = _run_async_pipeline(
            tenant_id=tenant.id, attachment=attachment, msg=msg
        )
    except Exception as e:
        event.status = "failed"
        event.error = f"upload_or_mapping_failed: {type(e).__name__}: {e}"
        session.commit()
        log.exception("inbox_pipeline_failed", message_id=msg.message_id)
        _try_send_reject_reply(msg, settings=settings, reason=event.error)
        return ProcessResult(event.id, "failed", event.error)

    event.template_id = template.id
    event.template_version_id = tv.id
    session.commit()  # make event.id visible to the FK on inbox_test_results

    executed_trials: list[ExecutedTrial] = []
    rendered, ext, render_error = (None, "docx", "freeze_failed")
    if freeze_ok:
        # Multi-test matrix: N AI-planned trials, each rendered, persisted,
        # and scored by the verdict LLM. The first trial doubles as the
        # legacy single-render sample so the reply still attaches one
        # concrete artifact for offline spot-checking.
        try:
            executed_trials = execute_test_matrix(
                session,
                event_id=event.id,
                tv=tv,
                tenant_ai_provider_config=tenant.ai_provider_config or None,
                settings=settings,
            )
        except Exception as e:
            log.warning(
                "inbox_test_matrix_failed",
                event_id=str(event.id),
                error=f"{type(e).__name__}: {e}",
            )
        if executed_trials:
            first = executed_trials[0]
            rendered = first.rendered
            ext = first.ext
            render_error = first.render_error
        else:
            # Matrix produced nothing (schema-invalid, planner exception).
            # Fall back to the single-render path so the reply still tells
            # the sender whether the template renders at all.
            rendered, ext, render_error = _test_render(tv, settings=settings)

    score = _score(proposal, render_ok=render_error is None, freeze_ok=freeze_ok)
    event.score = score
    threshold = auto_approve_threshold(tenant)

    auto_approved = False
    if freeze_ok and render_error is None and score >= threshold:
        try:
            async def _publish():
                from docgen.store.db import async_session

                async with async_session() as s2:
                    await publish_version(
                        s2,
                        template_id=template.id,
                        tenant_id=tenant.id,
                        version=tv.version,
                        subversion=tv.subversion,
                        actor=msg.from_addr,
                    )

            _run_async(_publish())
            auto_approved = True
            event.review_state = "auto_approved"
            _write_template_audit(
                template_id=template.id,
                template_version_id=tv.id,
                event_kind="inbox_auto_approved",
                actor=f"inbox-auto:{msg.from_addr}",
                summary={
                    "inbox_event_id": str(event.id),
                    "score": score,
                    "threshold": threshold,
                },
            )
        except Exception as e:
            # Don't fail the whole event — freeze already succeeded and the
            # sender still gets their test PDF. Just record and leave for
            # human review.
            log.warning(
                "inbox_auto_publish_failed",
                template_id=str(template.id),
                error=str(e),
            )
            event.review_state = "pending_review"
    else:
        event.review_state = "pending_review" if freeze_ok else None

    session.commit()

    admin_link = _admin_link_for(settings, event.id)
    template_link = _template_link_for(settings, template.id)
    trial_download_urls: dict[int, str | None] = {
        t.ordinal: (
            _trial_download_url(settings, t.test_result_id)
            if t.test_result_id is not None
            else None
        )
        for t in executed_trials
    }
    body_text, body_html = _build_reply_body(
        tenant_name=tenant.name,
        template_name=template.name,
        template_id=template.id,
        template_version_id=tv.id,
        score=score,
        threshold=threshold,
        auto_approved=auto_approved,
        render_error=render_error,
        freeze_ok=freeze_ok,
        admin_link=admin_link,
        template_link=template_link,
        trials=executed_trials,
        trial_download_urls=trial_download_urls,
    )

    attachments: list[Attachment] = []
    if rendered is not None:
        content_type = "application/pdf" if ext == "pdf" else (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        stem = _template_name_from(msg, attachment.filename)
        attachments.append(
            Attachment(
                filename=f"{stem}-test.{ext}",
                content=rendered,
                content_type=content_type,
            )
        )

    cc = list(msg.cc_addrs) + notify_addresses(tenant)
    try:
        reply_msgid = send_reply(
            settings=settings,
            to_addrs=[msg.from_addr],
            cc_addrs=cc,
            subject=_build_reply_subject(msg.subject),
            body_text=body_text,
            body_html=body_html,
            in_reply_to=msg.message_id,
            references=msg.references + [msg.message_id],
            attachments=attachments,
        )
        event.reply_message_id = reply_msgid
    except MailerError as e:
        # Don't overwrite freeze/render status — the ingest succeeded, only
        # the notification failed. Operators can still see the row in the
        # admin portal.
        event.error = (event.error or "") + f" reply_failed: {e}"
        log.warning(
            "inbox_reply_send_failed", event_id=str(event.id), error=str(e)
        )

    status = "succeeded" if freeze_ok and render_error is None else "failed"
    event.status = status
    session.commit()

    return ProcessResult(event.id, status, "ok" if status == "succeeded" else (render_error or "freeze_failed"))


def _try_send_reject_reply(
    msg: InboundMessage,
    *,
    settings: Settings,
    reason: str,
) -> None:
    if not settings.smtp_host:
        return
    text, html = _build_reject_body(reason)
    try:
        send_reply(
            settings=settings,
            to_addrs=[msg.from_addr],
            cc_addrs=list(msg.cc_addrs),
            subject=_build_reply_subject(msg.subject),
            body_text=text,
            body_html=html,
            in_reply_to=msg.message_id,
            references=msg.references + [msg.message_id],
        )
    except MailerError as e:
        log.warning("inbox_reject_reply_failed", error=str(e), reason=reason)


# ---------------------------------------------------------------------------
# Reply-to-approve (Phase 3)
# ---------------------------------------------------------------------------


def _write_template_audit(
    *,
    template_id: uuid.UUID | None,
    template_version_id: uuid.UUID | None,
    event_kind: str,
    actor: str,
    summary: dict[str, Any] | None = None,
) -> None:
    """Write a template_audit_events row from the sync inbox agent.

    The audit helper is async-only (shared with the designtime pipeline),
    so we open a short-lived async session and commit it independently of
    the inbox's sync session. No-op when there's no template to audit
    against — some inbox events (freeze-fail before a template row exists,
    sender-mismatch on an orphan parent) legitimately have no target.
    Failures are logged, never raised: an audit gap is better than losing
    the underlying user-visible action.
    """
    if template_id is None:
        return

    async def _write():
        from docgen.store.db import async_session

        async with async_session() as s:
            await record_audit_event(
                s,
                template_id=template_id,
                version_id=template_version_id,
                event_kind=event_kind,
                actor=actor,
                summary=summary,
            )
            await s.commit()

    try:
        _run_async(_write())
    except Exception as e:  # pragma: no cover — logged and swallowed
        log.warning(
            "inbox_audit_write_failed",
            event_kind=event_kind,
            template_id=str(template_id),
            error=str(e),
        )


def _record_reply_event(
    session: Session,
    *,
    msg: InboundMessage,
    parent: InboxEvent,
    detail: str,
) -> InboxEvent:
    """Persist a terminal row for the reply so the poller doesn't re-process it.

    Reply rows aren't tied to a review_state (that lives on the parent); we
    reuse `status='skipped'` and cross-reference the parent via error text
    so operators can trace the decision chain from the inbox list.
    """
    row = InboxEvent(
        message_id=msg.message_id,
        from_addr=msg.from_addr,
        to_addr=(msg.to_addrs[0] if msg.to_addrs else None),
        cc_addrs=list(msg.cc_addrs),
        subject=(msg.subject or "")[:998],
        received_at=msg.received_at,
        status="skipped",
        tenant_id=parent.tenant_id,
        template_id=parent.template_id,
        template_version_id=parent.template_version_id,
        error=f"reply_to:{parent.id}:{detail}",
        body_text=_truncate_body(msg.body_text),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _send_reply_ack(
    msg: InboundMessage,
    *,
    settings: Settings,
    headline: str,
    detail: str,
) -> None:
    if not settings.smtp_host:
        return
    text = f"{headline}\n\n{detail}"
    html = f"<p>{headline}</p><p>{detail}</p>"
    try:
        send_reply(
            settings=settings,
            to_addrs=[msg.from_addr],
            cc_addrs=list(msg.cc_addrs),
            subject=_build_reply_subject(msg.subject),
            body_text=text,
            body_html=html,
            in_reply_to=msg.message_id,
            references=msg.references + [msg.message_id],
        )
    except MailerError as e:
        log.warning("inbox_reply_ack_failed", error=str(e))


def _process_reply(
    session: Session,
    *,
    msg: InboundMessage,
    parent: InboxEvent,
    settings: Settings,
) -> ProcessResult:
    """Handle a reply to a prior notification: APPROVE → publish, REJECT → mark."""
    if not sender_matches(msg.from_addr, parent):
        detail = f"sender_mismatch:{msg.from_addr}"
        row = _record_reply_event(session, msg=msg, parent=parent, detail=detail)
        _write_template_audit(
            template_id=parent.template_id,
            template_version_id=parent.template_version_id,
            event_kind="inbox_sender_mismatch",
            actor=f"spoofed:{msg.from_addr}",
            summary={
                "inbox_event_id": str(parent.id),
                "expected_from": parent.from_addr,
            },
        )
        _send_reply_ack(
            msg,
            settings=settings,
            headline="Reply ignored — sender does not match the original requester.",
            detail=(
                "Only the address that sent the original template email "
                "(or someone CC'd on it) can approve or reject."
            ),
        )
        return ProcessResult(row.id, "skipped", detail)

    if parent.review_state in ("approved", "auto_approved"):
        row = _record_reply_event(
            session, msg=msg, parent=parent, detail="already_approved"
        )
        _send_reply_ack(
            msg,
            settings=settings,
            headline="Nothing to do — this template was already approved.",
            detail=f"Template version {parent.template_version_id} is already published.",
        )
        return ProcessResult(row.id, "skipped", "already_approved")

    if parent.review_state == "rejected":
        row = _record_reply_event(
            session, msg=msg, parent=parent, detail="already_rejected"
        )
        _send_reply_ack(
            msg,
            settings=settings,
            headline="Nothing to do — this template was already rejected.",
            detail="If you want to try again, resend the original attachment.",
        )
        return ProcessResult(row.id, "skipped", "already_rejected")

    decision = parse_decision(msg.body_text)
    if decision is None:
        row = _record_reply_event(session, msg=msg, parent=parent, detail="no_keyword")
        _send_reply_ack(
            msg,
            settings=settings,
            headline="Reply received but no decision was detected.",
            detail=(
                "Reply with a single line 'APPROVE' to publish or 'REJECT' "
                "to discard. Case doesn't matter; trailing punctuation is fine."
            ),
        )
        return ProcessResult(row.id, "skipped", "no_keyword")

    if decision == "approve":
        if parent.template_id is None or parent.template_version_id is None:
            row = _record_reply_event(
                session, msg=msg, parent=parent, detail="approve_no_template"
            )
            _send_reply_ack(
                msg,
                settings=settings,
                headline="Approval received, but there's nothing to publish.",
                detail="The original ingest failed before producing a template version.",
            )
            return ProcessResult(row.id, "skipped", "approve_no_template")

        tv_version = None
        tv_subversion = None

        async def _load_and_publish():
            from docgen.store.db import async_session

            async with async_session() as s2:
                tv = await s2.get(TemplateVersion, parent.template_version_id)
                if tv is None:
                    raise ValueError("template version disappeared")
                await publish_version(
                    s2,
                    template_id=parent.template_id,
                    tenant_id=parent.tenant_id,
                    version=tv.version,
                    subversion=tv.subversion,
                    actor=f"reply-approve:{msg.from_addr}",
                )
                return tv.version, tv.subversion

        try:
            tv_version, tv_subversion = _run_async(_load_and_publish())
        except Exception as e:
            row = _record_reply_event(
                session,
                msg=msg,
                parent=parent,
                detail=f"publish_failed:{type(e).__name__}",
            )
            log.exception(
                "inbox_reply_publish_failed", parent=str(parent.id), error=str(e)
            )
            _send_reply_ack(
                msg,
                settings=settings,
                headline="Approval received, but publishing failed.",
                detail=f"Reason: {e}. Try approving from the admin portal instead.",
            )
            return ProcessResult(row.id, "failed", f"publish_failed:{e}")

        parent.review_state = "approved"
        session.add(parent)
        session.commit()

        _write_template_audit(
            template_id=parent.template_id,
            template_version_id=parent.template_version_id,
            event_kind="inbox_approved",
            actor=f"reply-approve:{msg.from_addr}",
            summary={"inbox_event_id": str(parent.id)},
        )
        row = _record_reply_event(session, msg=msg, parent=parent, detail="approved")
        _send_reply_ack(
            msg,
            settings=settings,
            headline=f"Template v{tv_version}.{tv_subversion} approved and published.",
            detail="It's now the active version for your tenant.",
        )
        return ProcessResult(row.id, "succeeded", "approved")

    # decision == "reject"
    parent.review_state = "rejected"
    session.add(parent)
    session.commit()
    _write_template_audit(
        template_id=parent.template_id,
        template_version_id=parent.template_version_id,
        event_kind="inbox_rejected",
        actor=f"reply-reject:{msg.from_addr}",
        summary={"inbox_event_id": str(parent.id)},
    )
    row = _record_reply_event(session, msg=msg, parent=parent, detail="rejected")
    _send_reply_ack(
        msg,
        settings=settings,
        headline="Rejection recorded — the draft was not published.",
        detail="The template version is retained as a draft for auditing.",
    )
    return ProcessResult(row.id, "succeeded", "rejected")


def dispatch_message(
    session: Session,
    *,
    msg: InboundMessage,
    settings: Settings | None = None,
) -> ProcessResult:
    """Route an inbound message to reply-handling or fresh-ingest.

    A message counts as a reply when its In-Reply-To header points at a
    Message-ID we previously sent (stored in InboxEvent.reply_message_id).
    Anything else — including replies to unrelated threads — falls
    through to `process_message` for ingest.
    """
    settings = settings or get_settings()
    parent = find_parent_event(session, msg.in_reply_to)
    if parent is not None:
        return _process_reply(session, msg=msg, parent=parent, settings=settings)
    return process_message(session, msg=msg, settings=settings)


