"""Sync entry point for the JSON intake channel (REST + MCP).

Parallel to :mod:`pap_pipeline.inbox_hook` but for callers that already
have structured fields — CRMs, call-center apps, MCP tool users. Skips
the extract stage entirely: the payload becomes the "raw extraction"
directly, so the pipeline resumes at retrieve → reason.

The design mirrors the email path deliberately:

- A row is written to ``inbox_events`` for every submission (even
  validation failures). Reviewers see API cases in the same queue as
  email cases; the channel is encoded on the ``from_addr`` prefix
  (``api://…`` for REST, ``mcp://…`` for MCP).
- The pipeline is invoked sync in-process via ``asyncio.run`` — same
  pattern as :func:`pap_pipeline.inbox_hook._run_pipeline`. FastAPI
  handlers call this from a thread-pool executor so we don't block the
  event loop.
- Reply-letter rendering runs on success so the reviewer can preview
  what the applicant would receive; sending it is still gated on human
  approval via the existing inbox-agent path.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog

from docgen.config import Settings
from docgen.net_safety import UrlNotAllowed, validate_public_url
from docgen.store.db import SyncSessionLocal
from docgen.store.models import InboxEvent, Program, Tenant
from pap_pipeline._async_bridge import run_pipeline_sync
from pap_pipeline.inbox_audit import AI_ACTOR, append_audit
from pap_pipeline.inbox_hook import (
    _draft_reply_artifact,
    _score_from_decision,
)
from pap_pipeline.json_intake import (
    IntakeValidationError,
    validate_json_intake,
)
from pap_pipeline.orchestrator import (
    _extras_from_extraction,
    _intake_from_extraction,
    run_triaged_from_intake,
)
from pap_pipeline.pipeline_config import build_pipeline_config
from pap_pipeline.rules import load_rules
from pap_pipeline.schemas.decision import Decision

log = structlog.get_logger(__name__)


# Prefix on InboxEvent.from_addr that tells the UI + reviewers this case
# arrived over a non-email channel. Kept as a URL-ish scheme so it reads
# naturally in the "From" column of the inbox list.
CHANNEL_REST = "rest"
CHANNEL_MCP = "mcp"


class ProgramNotFound(Exception):
    """Raised when the (tenant, slug) pair doesn't resolve to an active program."""


class IntakeValidationFailed(Exception):
    """Raised when the payload fails schema validation.

    Carries the structured error list so the REST handler can render a
    400 response with per-field messages, and the event_id so the UI can
    link the caller to the recorded failure for audit purposes.
    """

    def __init__(
        self,
        errors: list[IntakeValidationError],
        *,
        missing_required: list[str],
        unknown_keys: list[str],
        event_id: uuid.UUID,
    ) -> None:
        super().__init__(f"json intake validation failed ({len(errors)} error(s))")
        self.errors = errors
        self.missing_required = missing_required
        self.unknown_keys = unknown_keys
        self.event_id = event_id


class PipelineExecutionFailed(Exception):
    """Raised when retrieve/reason blew up. Includes event_id for follow-up."""

    def __init__(self, message: str, *, event_id: uuid.UUID) -> None:
        super().__init__(message)
        self.event_id = event_id


@dataclass
class ApiIntakeResult:
    """Outcome of a successful JSON intake submission.

    Two shapes share this dataclass:

    - **sync** (``status="succeeded"``): full decision is returned inline;
      the caller is done. This is the path for programs without a review
      hold.
    - **queued** (``status="queued"``): the pipeline finished but the case
      is holding for reviewer approval. ``outcome`` still carries the
      draft verdict so the caller can render "pending — likely approve",
      but the authoritative final answer is delivered later via webhook
      when the reviewer clicks approve/reject. ``callback_url`` is
      populated when the caller supplied one.
    """

    event_id: uuid.UUID
    case_id: str
    status: str
    review_state: str | None
    outcome: str
    decision: dict[str, Any]
    intake: dict[str, Any]
    unknown_keys: list[str]
    callback_url: str | None = None
    requires_review: bool = False


def _channel_from_addr(channel: str, actor: str) -> str:
    """Encode the intake channel + caller identity as a URL-ish string.

    Stored on ``InboxEvent.from_addr`` so the UI can show a channel icon
    on the inbox row and reviewers can trace which integration or MCP
    session submitted the case. ``actor`` is typically the API key's
    display name — never the raw key.
    """
    safe = (actor or "unknown").strip().replace(" ", "-")[:100]
    return f"{channel}://{safe}"


def run_json_intake_sync(
    tenant_id: uuid.UUID,
    program_slug: str,
    payload: dict[str, Any],
    settings: Settings,
    *,
    actor: str = "api",
    channel: str = CHANNEL_REST,
    callback_url: str | None = None,
) -> ApiIntakeResult:
    """Run the pipeline against a JSON payload; persist a case row.

    Sync-only: creates its own :class:`SyncSessionLocal` to mirror the
    inbox handler and to keep the reply-render helpers happy (they were
    written for the sync inbox actor). FastAPI handlers should call this
    from ``run_in_threadpool`` so the event loop doesn't stall on the
    Nemotron round-trip.

    Raises :class:`ProgramNotFound`, :class:`IntakeValidationFailed`, or
    :class:`PipelineExecutionFailed` — all three carry enough context for
    the caller to render a useful 4xx/5xx response.
    """
    if channel not in (CHANNEL_REST, CHANNEL_MCP):
        raise ValueError(f"unknown channel: {channel!r}")

    program_slug = (program_slug or "").strip().lower()
    if not program_slug:
        raise ProgramNotFound("program slug is required")

    # Validate callback_url at the intake boundary so bad URLs never reach
    # the DB. `validate_public_url` covers scheme, userinfo, loopback
    # hostnames, and DNS resolution to non-public IPs — closes the SSRF
    # surface where a caller could point the webhook at 169.254.169.254
    # (cloud metadata) or 127.0.0.1:5432 (in-cluster services). The
    # webhook dispatcher re-validates just before the outbound POST as
    # defense against DNS rebinding between intake and delivery.
    if callback_url is not None:
        callback_url = callback_url.strip() or None
    if callback_url is not None:
        try:
            validate_public_url(callback_url)
        except UrlNotAllowed as e:
            # Server-side log carries the specific reason ("unresolvable
            # host 'internal.corp.local'", "non-public IP 10.0.0.1", …).
            # The caller-visible error is deliberately generic so an
            # SSRF-recon caller can't distinguish "host doesn't exist"
            # from "host exists but resolves to a private IP" by
            # comparing 400 detail strings across many probes.
            log.warning(
                "pap_intake_callback_url_rejected",
                reason=str(e),
            )
            raise ValueError("callback_url is not an allowed URL") from e

    with SyncSessionLocal() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise ProgramNotFound("tenant not found")

        program = (
            session.query(Program)
            .filter(
                Program.tenant_id == tenant_id,
                Program.slug == program_slug,
                Program.is_active.is_(True),
            )
            .first()
        )
        if program is None:
            raise ProgramNotFound(
                f"program {program_slug!r} not found for this tenant"
            )

        schema = list(program.extraction_schema or [])
        validation = validate_json_intake(payload, schema)

        # Always create the case row — even failed validations should be
        # visible to reviewers for audit. The channel prefix on from_addr
        # is what the inbox list renders as a source icon.
        event = InboxEvent(
            message_id=f"{channel}://{uuid.uuid4()}",
            from_addr=_channel_from_addr(channel, actor),
            to_addr=None,
            received_at=datetime.now(UTC),
            subject=f"{channel.upper()} intake — {program.slug}",
            tenant_id=tenant_id,
            status="processing",
            inbound_attachments=[
                {
                    "filename": "intake.json",
                    "content_type": "application/json",
                    "size": _payload_size(payload),
                    "blob_key": None,
                }
            ],
        )
        session.add(event)
        session.flush()

        append_audit(
            event,
            action="pipeline_started",
            actor=AI_ACTOR,
            note=(
                f"JSON intake received over {channel.upper()} from {actor!r}. "
                f"Program: {program.slug}. "
                f"Schema: {len(schema)} slot(s). "
                f"Payload keys: {len(payload) if isinstance(payload, dict) else 0}."
            ),
        )
        session.commit()

        if validation.errors or validation.missing_required:
            event.status = "failed"
            note_parts = []
            if validation.errors:
                note_parts.append(
                    f"{len(validation.errors)} field(s) failed type coercion"
                )
            if validation.missing_required:
                note_parts.append(
                    "missing required: " + ", ".join(validation.missing_required)
                )
            if validation.unknown_keys:
                note_parts.append(
                    "unknown keys: " + ", ".join(validation.unknown_keys)
                )
            event.error = "json_intake_validation_failed: " + "; ".join(note_parts)
            append_audit(
                event,
                action="pipeline_failed",
                actor=AI_ACTOR,
                note=event.error,
            )
            session.commit()
            raise IntakeValidationFailed(
                validation.errors,
                missing_required=validation.missing_required,
                unknown_keys=validation.unknown_keys,
                event_id=event.id,
            )

        # Split the normalized dict into an Intake (canonical PAP fields)
        # + extras (program-specific slots). Same helper the extract path
        # uses, so downstream stages don't know they got here via JSON.
        raw = dict(validation.normalized)
        intake = _intake_from_extraction(raw)
        extras = _extras_from_extraction(raw)

        rules = load_rules(program_slug, session=session, tenant_id=tenant_id) or None
        config = build_pipeline_config(settings, tenant, program)

        try:
            # run_triaged_from_intake runs Triage first (honoring tenant /
            # program routing overrides + skip-Decide flag) and then
            # dispatches to run_from_intake for the Decide path. Prior to
            # this, the JSON channel bypassed Triage entirely — every case
            # ran straight through Decide with decision.triage=None.
            decision: Decision = run_pipeline_sync(
                run_triaged_from_intake(
                    intake,
                    config=config,
                    program=program_slug,
                    tenant_id=str(tenant.id),
                    extras=extras,
                    rules=rules,
                )
            )
        except Exception as e:
            event.status = "failed"
            event.error = f"pap_pipeline_failed: {type(e).__name__}: {e}"
            append_audit(
                event,
                action="pipeline_failed",
                actor=AI_ACTOR,
                note=f"{type(e).__name__}: {e}",
            )
            session.commit()
            log.exception("pap_api_pipeline_failed", event_id=str(event.id))
            raise PipelineExecutionFailed(str(e), event_id=event.id) from e

        event.score = _score_from_decision(decision)
        event.status = "succeeded"
        event.pap_intake = intake.model_dump(mode="json")
        # date/datetime instances slip in via the coerce step; JSONB
        # rejects them, so shallow-convert to ISO strings before persist.
        event.pap_extraction = _json_safe(raw)
        event.pap_decision = decision.model_dump(mode="json")

        # Contract split — sync vs queued for M2M:
        #
        # - Program requires review → hold in the HumanLoop queue; the
        #   caller sees status="queued". If they registered a callback_url
        #   we mark it pending; the approve/reject endpoint will POST the
        #   final verdict later. Missing_info falls into this bucket too
        #   even when requires_review is false — a human should look at
        #   any case where the pipeline itself couldn't answer.
        # - Program does not require review → auto-approve the M2M case
        #   so it doesn't clog HumanLoop. The verdict has already been
        #   handed to the caller inline; there is nothing for a reviewer
        #   to release.
        # Verifier findings (Phase C/D) force pending_review even for tenants
        # that would otherwise auto-approve. Any inconsistency flagged by
        # dual-VLM diff or by the deterministic citation checker is enough
        # to short-circuit the auto path.
        critical_findings = [
            f for f in decision.verifier_findings if f.severity == "critical"
        ]
        should_hold = (
            program.requires_review
            or decision.outcome == "missing_info"
            or bool(critical_findings)
        )
        result_status = "succeeded"
        if should_hold:
            event.review_state = "pending_review"
            if callback_url:
                event.callback_url = callback_url
                event.callback_status = "pending"
            if program.requires_review or critical_findings:
                result_status = "queued"
        else:
            event.review_state = "auto_approved"

        verdict_note = (
            f"Outcome: {decision.outcome}. "
            f"Citations: {len(decision.citations)}, "
            f"missing fields: {len(decision.missing_fields)}. "
        )
        if critical_findings:
            types = sorted({f.type for f in critical_findings})
            verdict_note += (
                f"Verifier flagged {len(critical_findings)} issue(s) "
                f"({', '.join(types)}). Escalated to HumanLoop. "
            )
        elif result_status == "queued":
            verdict_note += "Held for reviewer approval before webhook delivery."
        elif event.review_state == "auto_approved":
            verdict_note += "Auto-approved — verdict returned inline to the caller."
        else:
            verdict_note += "Queued for human review."
        append_audit(
            event,
            action="verdict_rendered",
            actor=AI_ACTOR,
            note=verdict_note,
        )
        session.commit()

        # Draft the reply artifact so a reviewer can preview it. Uses the
        # same dispatcher as the email path: package assembler when the
        # program has bindings, single-template fallback otherwise. Failures
        # don't fail the intake — same tolerance as the email path.
        try:
            blob_key = _draft_reply_artifact(
                session,
                tenant,
                intake,
                decision,
                program_slug,
                event,
                settings,
                extras=extras,
            )
            if blob_key:
                event.reply_attachment_blob_key = blob_key
                append_audit(
                    event,
                    action="reply_drafted",
                    actor=AI_ACTOR,
                    note=(
                        f"Drafted {decision.outcome} reply letter and stored for "
                        f"reviewer preview."
                    ),
                )
                session.commit()
            else:
                append_audit(
                    event,
                    action="reply_draft_skipped",
                    actor=AI_ACTOR,
                    note=(
                        "No outcome-matched template found — reply will be "
                        "text-only when sent."
                    ),
                )
                session.commit()
        except Exception as e:
            log.warning(
                "pap_api_reply_render_failed",
                event_id=str(event.id),
                error=f"{type(e).__name__}: {e}",
                exc_info=True,
            )

        return ApiIntakeResult(
            event_id=event.id,
            case_id=event.case_id,
            status=result_status,
            review_state=event.review_state,
            outcome=decision.outcome,
            decision=event.pap_decision,
            intake=event.pap_intake,
            unknown_keys=validation.unknown_keys,
            callback_url=event.callback_url,
            requires_review=bool(program.requires_review),
        )


def _json_safe(value: Any) -> Any:
    """Recursively coerce dates/datetimes to ISO strings for JSONB.

    The coerce step in :mod:`pap_pipeline.json_intake` builds typed
    values so downstream code can rely on real ``date`` objects, but
    psycopg's JSONB codec can't marshal them. This one-time conversion
    at the persistence boundary keeps the domain logic typed while
    letting the audit blob round-trip through Postgres.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _payload_size(payload: Any) -> int:
    """Best-effort byte count for the inbound-attachment manifest.

    Not authoritative — the JSON we serialize may differ from what the
    caller sent (whitespace, key order). But close enough to give
    reviewers a sense of "how big was this submission" on the case row.

    Narrow except: only the two `json.dumps` failure modes are silenced
    (non-serializable value, deep recursion). Anything else bubbles up
    so an unexpected TypeError from somewhere else isn't hidden by a
    reviewer-metrics helper.
    """
    import json as _json

    try:
        return len(_json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError) as e:
        log.debug(
            "pap_api_payload_size_failed",
            error=f"{type(e).__name__}: {e}",
        )
        return 0


__all__ = [
    "CHANNEL_MCP",
    "CHANNEL_REST",
    "ApiIntakeResult",
    "IntakeValidationFailed",
    "PipelineExecutionFailed",
    "ProgramNotFound",
    "run_json_intake_sync",
]
