"""FastMCP server. Mounted at /mcp on the FastAPI app (streamable-http transport).

Per §11: each frozen template is exposed as a tool. This first cut registers a generic
render tool + a discovery tool. Dynamic per-template tools with strict input schemas are
a Day-2 refinement (requires listChanged notifications for post-freeze registration).

All tools scope by the caller's tenant, resolved from the `_tenant_id` ContextVar set by
`ApiKeyAuthMiddleware`. Callers must send `Authorization: Bearer <key>` on every MCP
request; the middleware enforces it before the tool handler runs.
"""
from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from mcp.server import MCPServer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from docgen.api import idempotency as idem
from docgen.auth import _tenant_id, sign_download_token
from docgen.config import Settings, get_settings
from docgen.core.render_pipeline import FORMAT_META, execute_merge, persist_result
from docgen.core.render_stack import render_stack_hash
from docgen.core.schema_infer import TemplateSchema, validate_payload
from docgen.store.blob import delete_object, get_object, head_object
from docgen.store.db import async_session
from docgen.store.models import (
    IdempotencyKey,
    InboxEvent,
    Provenance,
    RenderJob,
    Template,
    TemplateVersion,
)

log = structlog.get_logger(__name__)

mcp = MCPServer(name="docgen")


def _fetch_url(job_id: uuid.UUID | str, tenant_id: uuid.UUID | str) -> str:
    """Return a clickable download URL for a render job.

    The URL carries a short-lived signed `?token=` that bypasses Bearer auth
    for this one job — so a human can click the link in Claude Desktop / a
    browser without pasting the API key. Absolute if DOCGEN_PUBLIC_BASE_URL
    is set, relative otherwise. TTL: see DOWNLOAD_TOKEN_MAX_AGE_SECONDS.
    """
    base = get_settings().public_base_url.rstrip("/")
    token = sign_download_token(str(job_id), str(tenant_id))
    path = f"/jobs/{job_id}/result?token={token}"
    return f"{base}{path}" if base else path


def _current_tenant_uuid() -> uuid.UUID:
    """Return the caller's tenant_id (from auth middleware) or raise.

    ContextVar is set by `ApiKeyAuthMiddleware`; if unset, the request bypassed
    auth — which means either middleware config drift or a direct in-process
    invocation. Both are programming errors from the caller's point of view.
    """
    raw = _tenant_id.get()
    if raw is None:
        raise PermissionError("MCP tools require an authenticated tenant context")
    return uuid.UUID(raw)


@mcp.tool()
async def list_templates() -> list[dict[str, Any]]:
    """List templates (with a frozen version) belonging to the caller's tenant."""
    tenant_id = _current_tenant_uuid()
    async with async_session() as session:
        # Single JOIN — was N+1 (one query per template to fetch its current
        # TemplateVersion). At tenant scale (hundreds of templates) the extra
        # round-trips dominate.
        from sqlalchemy import func as _func

        result = await session.execute(
            select(Template, TemplateVersion)
            .join(
                TemplateVersion,
                (TemplateVersion.template_id == Template.id)
                & (TemplateVersion.version == Template.current_version)
                & (
                    TemplateVersion.subversion
                    == _func.coalesce(Template.current_subversion, 0)
                )
                & (TemplateVersion.locale == "default"),
            )
            .where(
                Template.tenant_id == tenant_id,
                Template.current_version.is_not(None),
                Template.is_active.is_(True),
            )
        )
        return [
            {
                "template_id": str(t.id),
                "name": t.name,
                "current_version": t.current_version,
                "version_hash": tv.version_hash,
            }
            for t, tv in result.all()
        ]


@mcp.tool()
async def get_template_schema(template_id: str) -> dict[str, Any]:
    """Return the JSON schema for a template's current frozen version."""
    tenant_id = _current_tenant_uuid()
    tid = uuid.UUID(template_id)
    async with async_session() as session:
        template = await session.get(Template, tid)
        if (
            template is None
            or template.tenant_id != tenant_id
            or not template.is_active
            or template.current_version is None
        ):
            raise ValueError(f"Template {template_id} has no frozen version")
        result = await session.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == tid,
                TemplateVersion.version == template.current_version,
                TemplateVersion.subversion == (template.current_subversion or 0),
                TemplateVersion.locale == "default",
            )
        )
        tv = result.scalar_one_or_none()
        if tv is None:
            raise ValueError(
                f"Version {template.current_version}."
                f"{template.current_subversion or 0} not found"
            )
        ts = TemplateSchema.model_validate(tv.schema)
        return {
            "template_id": template_id,
            "version": tv.version,
            "version_hash": tv.version_hash,
            "json_schema": ts.to_json_schema(),
        }


# Base64-inlining a rendered artifact into an MCP tool result blows past most
# clients' response caps (Claude Desktop tops out around ~1MB). Return metadata
# + a REST fetch URL by default; callers opt into inline base64 for small ones.
_MCP_INLINE_MAX_BYTES = 256 * 1024


@mcp.tool()
async def render_template(
    template_id: str,
    data: dict[str, Any],
    version: int | None = None,
    locale: str | None = None,
    caller: str | None = None,
    output_format: str = "docx",
    inline: bool = False,
) -> dict[str, Any]:
    """Render a frozen template. Returns `job_id` + a REST fetch URL.

    - `output_format`: "docx" (default) or "pdf". For PDF templates this is
      silently coerced to "pdf" — PDF-in / PDF-out only.
    - `inline`: if true, and the rendered artifact is small enough
      (≤256 KB), the base64-encoded bytes are included in the response
      under `pdf_base64` / `docx_base64`. Otherwise the caller must fetch
      the result via `GET /jobs/{job_id}/result` with their API key.
    - `caller`: free-form tag recorded on the job (e.g. "claude-desktop",
      "n8n-workflow-42"). **Included in the idempotency fingerprint**: two
      logically different callers running the exact same payload get
      distinct jobs — treat it as the dedup scope boundary.

    Idempotent by request fingerprint: repeating the same call returns the
    original job (marked `replayed: true`) instead of writing a duplicate.
    If the prior job's blob has been garbage-collected, a fresh render is
    kicked off transparently.
    """
    tenant_id = _current_tenant_uuid()
    tid = uuid.UUID(template_id)
    settings = get_settings()

    if output_format not in FORMAT_META:
        raise ValueError(
            f"output_format must be one of {sorted(FORMAT_META)}; got {output_format!r}"
        )

    async with async_session() as session:
        template = await session.get(Template, tid)
        if template is None or template.tenant_id != tenant_id or not template.is_active:
            raise ValueError(f"Template {template_id} not found")
        # Explicit `version` → highest frozen sub for that version. No version →
        # pin to the exact (current_version, current_subversion) publish blessed
        # so runtime doesn't drift onto an unpublished sub-version.
        if version is not None:
            v = version
            pinned_sub: int | None = None
        else:
            v = template.current_version
            pinned_sub = template.current_subversion
        if v is None:
            raise ValueError("Template has no frozen version")
        target_locale = locale or "default"
        q = select(TemplateVersion).where(
            TemplateVersion.template_id == tid,
            TemplateVersion.version == v,
            TemplateVersion.locale == target_locale,
            TemplateVersion.is_frozen.is_(True),
        )
        if pinned_sub is not None:
            q = q.where(TemplateVersion.subversion == pinned_sub)
        else:
            q = q.order_by(TemplateVersion.subversion.desc())
        q = q.limit(1)
        result = await session.execute(q)
        tv = result.scalar_one_or_none()
        if tv is None:
            raise ValueError(
                f"Frozen v{v} (locale '{target_locale}') not found"
            )

        # PDF templates only produce PDF (mirrors REST /render behavior). Coerce
        # silently so a shared client payload defaulting to docx still works.
        coerced = False
        if tv.pdf_flavor is not None and output_format != "pdf":
            output_format = "pdf"
            coerced = True

        ok, errors = validate_payload(tv.schema, data)
        if not ok:
            raise ValueError("Payload validation failed: " + "; ".join(errors))

        # MCP has no client-supplied Idempotency-Key header, so derive one from
        # the request fingerprint. Same call twice → same job, no orphaned blob.
        # `inline` is a transport preference (not part of the artifact) so it's
        # excluded from the hash — inline=True after a metadata-only call still
        # replays the same job and inlines the bytes.
        fingerprint = {
            "template_version_id": str(tv.id),
            "data": data,
            "output_format": output_format,
            "caller": caller or "mcp",
            "callback_url": None,
        }
        request_hash = idem.hash_request(fingerprint)
        key = f"mcp:{request_hash}"
        existing = await idem.lookup(session, tenant_id, key)
        if existing is not None:
            prior = await session.get(RenderJob, existing.job_id)
            if prior is not None and prior.state == "succeeded" and prior.result_blob_key:
                # The idempotency row survives, but the blob may have been
                # pruned by a retention policy or ops action. Verify before
                # returning — a fetch_url pointing at a 404 is worse than a
                # fresh render.
                if head_object(settings.s3_bucket_documents, prior.result_blob_key):
                    log.info(
                        "mcp_render_replayed",
                        tenant_id=str(tenant_id),
                        template_id=template_id,
                        job_id=str(prior.id),
                    )
                    return _build_render_response(
                        job=prior,
                        version_hash=tv.version_hash,
                        tenant_id=tenant_id,
                        inline=inline,
                        coerced=coerced,
                        replayed=True,
                        settings=settings,
                    )
                log.warning(
                    "mcp_replay_blob_gone",
                    tenant_id=str(tenant_id),
                    template_id=template_id,
                    job_id=str(prior.id),
                    blob_key=prior.result_blob_key,
                )

        output = execute_merge(tv, data, output_format, settings=settings)

        job = RenderJob(
            template_version_id=tv.id,
            tenant_id=tenant_id,
            state="succeeded",
            caller=caller or "mcp",
            payload=data,
            output_format=output_format,
            completed_at=datetime.now(UTC),
            result_bytes=len(output),
        )
        session.add(job)
        await session.flush()

        blob_key = persist_result(
            tv, job.id, output, output_format, settings=settings
        )
        job.result_blob_key = blob_key

        session.add(
            Provenance(
                render_job_id=job.id,
                template_version_id=tv.id,
                version_hash=tv.version_hash,
                render_stack_hash=render_stack_hash(),
                caller=caller or "mcp",
            )
        )

        # If an idempotency row for this key doesn't already exist, stage it
        # in the same transaction as the render job. On process death anywhere
        # between here and the commit, both the job row and the idem mapping
        # roll back together — no orphaned "phantom retry" case.
        # If `existing` was set above but we fell through (blob was gone),
        # skip re-staging: the row already points at the (now-deleted) prior
        # job. That's fine — it will get replaced on the next successful
        # render by TTL cleanup, and this render is best-effort recovery.
        wrote_idem_row = False
        if existing is None:
            session.add(
                IdempotencyKey(
                    tenant_id=tenant_id,
                    key=key,
                    request_hash=request_hash,
                    job_id=job.id,
                )
            )
            wrote_idem_row = True

        try:
            await session.commit()
        except IntegrityError:
            # Another concurrent request beat us to this idem key. Roll back,
            # look up the winner, and honor their job_id. Our render blob is
            # now orphaned in object storage — best-effort delete so we don't
            # leak on every race.
            await session.rollback()
            log.warning(
                "mcp_render_race_orphaned_blob",
                tenant_id=str(tenant_id),
                template_id=template_id,
                blob_key=blob_key,
            )
            try:
                delete_object(settings.s3_bucket_documents, blob_key)
            except Exception:
                log.exception(
                    "mcp_render_race_orphan_delete_failed", blob_key=blob_key
                )
            winner = await idem.lookup(session, tenant_id, key)
            if winner is None or winner.request_hash != request_hash:
                # Hash collision on the fingerprint itself → same bug class as
                # SHA-256 collisions in the wild. Surface loudly.
                raise ValueError(
                    "concurrent render race with mismatched fingerprint"
                ) from None
            prior = await session.get(RenderJob, winner.job_id)
            if prior is None or prior.state != "succeeded" or not prior.result_blob_key:
                raise ValueError("race winner job unavailable") from None
            return _build_render_response(
                job=prior,
                version_hash=tv.version_hash,
                tenant_id=tenant_id,
                inline=inline,
                coerced=coerced,
                replayed=True,
                settings=settings,
            )

        if wrote_idem_row:
            log.info(
                "mcp_render_committed",
                tenant_id=str(tenant_id),
                template_id=template_id,
                job_id=str(job.id),
            )

        return _build_render_response(
            job=job,
            version_hash=tv.version_hash,
            tenant_id=tenant_id,
            inline=inline,
            coerced=coerced,
            replayed=False,
            inline_bytes=output,
            settings=settings,
        )


def _build_render_response(
    *,
    job: RenderJob,
    version_hash: str,
    tenant_id: uuid.UUID,
    inline: bool,
    coerced: bool,
    replayed: bool,
    settings: Settings,
    inline_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Assemble the `render_template` response for both fresh and replayed jobs.

    `inline_bytes` is provided on the fresh path to skip re-reading MinIO;
    replays load from the blob store on demand when `inline=True`.
    """
    fmt = job.output_format
    size = len(inline_bytes) if inline_bytes is not None else (job.result_bytes or 0)
    response: dict[str, Any] = {
        "job_id": str(job.id),
        "version_hash": version_hash,
        "bytes": size,
        "output_format": fmt,
        "fetch_url": _fetch_url(job.id, tenant_id),
    }
    if replayed:
        response["replayed"] = True
    if coerced:
        response["note"] = (
            "output_format coerced to 'pdf' because the template is a PDF form"
        )
    if inline:
        if size <= _MCP_INLINE_MAX_BYTES:
            data = inline_bytes if inline_bytes is not None else get_object(
                settings.s3_bucket_documents, job.result_blob_key
            )
            payload_key = "pdf_base64" if fmt == "pdf" else "docx_base64"
            response[payload_key] = base64.b64encode(data).decode("ascii")
        else:
            response["inline_omitted"] = (
                f"output is {size} bytes, above the "
                f"{_MCP_INLINE_MAX_BYTES}-byte MCP inline cap; use fetch_url"
            )
    return response


@mcp.tool()
async def fetch_render_result(job_id: str) -> dict[str, Any]:
    """Fetch a rendered artifact by job_id.

    Returns metadata plus the base64-encoded bytes under
    `pdf_base64` / `docx_base64` when the artifact is ≤256 KB. Larger
    outputs return `fetch_url` only — the MCP transport can't carry
    them without exceeding client response caps; download via REST
    (`GET /jobs/{id}/result` with your API key).
    """
    tenant_id = _current_tenant_uuid()
    jid = uuid.UUID(job_id)
    async with async_session() as session:
        job = await session.get(RenderJob, jid)
        if job is None or job.tenant_id != tenant_id:
            raise ValueError(f"Job {job_id} not found")
        if job.state != "succeeded" or not job.result_blob_key:
            raise ValueError(f"Job not ready (state={job.state})")

    settings = get_settings()
    fmt = job.output_format
    response: dict[str, Any] = {
        "job_id": str(job.id),
        "output_format": fmt,
        "bytes": job.result_bytes,
        "fetch_url": _fetch_url(job.id, tenant_id),
    }
    if job.result_bytes is not None and job.result_bytes > _MCP_INLINE_MAX_BYTES:
        response["inline_omitted"] = (
            f"artifact is {job.result_bytes} bytes, above the "
            f"{_MCP_INLINE_MAX_BYTES}-byte MCP inline cap; use fetch_url"
        )
        return response

    data = get_object(settings.s3_bucket_documents, job.result_blob_key)
    payload_key = "pdf_base64" if fmt == "pdf" else "docx_base64"
    response[payload_key] = base64.b64encode(data).decode("ascii")
    return response


@mcp.tool()
async def fetch_case_reply(event_id: str) -> dict[str, Any]:
    """Return a short-lived signed download URL for a case's drafted reply.

    The artifact is the assembler's merged PDF (or the legacy single-template
    render for programs without package bindings). The URL bypasses Bearer
    auth so a remote client can open it directly — it's bound to the
    event_id and expires per `DOWNLOAD_TOKEN_MAX_AGE_SECONDS`.

    This is a *preview*: fetching does not approve, send, or otherwise
    advance the case. If no artifact exists yet (case still processing,
    or a draft failure), the tool raises a ValueError.
    """
    tenant_id = _current_tenant_uuid()
    eid = uuid.UUID(event_id)
    async with async_session() as session:
        event = await session.get(InboxEvent, eid)
        if event is None or event.tenant_id != tenant_id:
            raise ValueError(f"Case event {event_id} not found")
        if not event.reply_attachment_blob_key:
            raise ValueError(
                f"Case event {event_id} has no drafted reply yet "
                f"(status={event.status!r})"
            )
        case_number = event.case_number
        outcome = (event.pap_decision or {}).get("outcome")

    base = get_settings().public_base_url.rstrip("/")
    token = sign_download_token(str(eid), str(tenant_id))
    path = f"/downloads/case-reply/{eid}?token={token}"
    download_url = f"{base}{path}" if base else path
    return {
        "event_id": event_id,
        "case_number": case_number,
        "outcome": outcome,
        "download_url": download_url,
        "output_format": "pdf",
    }


@mcp.tool()
async def get_case_details(event_id: str) -> dict[str, Any]:
    """Return the full record for one case: verdict, intake, audit, reply link.

    The dict mirrors what the reviewer detail page renders — decision
    citations and missing fields, the extracted slot values (including
    any MDM-sourced history slots the pipeline merged in), the audit
    trail, and a signed URL for the drafted reply artifact when one
    exists. Read-only: fetching does not advance the case.
    """
    tenant_id = _current_tenant_uuid()
    eid = uuid.UUID(event_id)
    async with async_session() as session:
        ev = await session.get(InboxEvent, eid)
        if ev is None or ev.tenant_id != tenant_id:
            raise ValueError(f"Case event {event_id} not found")

        template_name = None
        if ev.template_id is not None:
            tmpl = await session.get(Template, ev.template_id)
            if tmpl is not None:
                template_name = tmpl.name

    reply: dict[str, Any] = {"drafted": bool(ev.reply_attachment_blob_key)}
    if ev.reply_attachment_blob_key:
        base = get_settings().public_base_url.rstrip("/")
        token = sign_download_token(str(eid), str(tenant_id))
        path = f"/downloads/case-reply/{eid}?token={token}"
        reply["download_url"] = f"{base}{path}" if base else path
        reply["blob_key"] = ev.reply_attachment_blob_key

    return {
        "event_id": event_id,
        "case_number": ev.case_number,
        "tenant_id": str(ev.tenant_id) if ev.tenant_id else None,
        "message_id": ev.message_id,
        "from_addr": ev.from_addr,
        "subject": ev.subject,
        "received_at": ev.received_at.isoformat() if ev.received_at else None,
        "status": ev.status,
        "review_state": ev.review_state,
        "reply_sent": ev.reply_message_id is not None,
        "template_name": template_name,
        "score": ev.score,
        "error": ev.error,
        "decision": ev.pap_decision,
        "intake": ev.pap_extraction or ev.pap_intake,
        "audit_trail": ev.review_history or [],
        "reply": reply,
    }


@mcp.tool()
async def list_pending_reviews(limit: int = 25) -> list[dict[str, Any]]:
    """List cases currently waiting on a human reviewer.

    Returns the most recently received `pending_review` cases for the
    caller's tenant with enough surface for a chat client to summarize
    the queue: case number, event_id, verdict outcome, subject/from,
    and receive time. Pair with `fetch_case_reply` to preview any row.
    """
    tenant_id = _current_tenant_uuid()
    limit = max(1, min(int(limit), 200))
    async with async_session() as session:
        result = await session.execute(
            select(InboxEvent)
            .where(
                InboxEvent.tenant_id == tenant_id,
                InboxEvent.review_state == "pending_review",
                InboxEvent.reply_message_id.is_(None),
            )
            .order_by(InboxEvent.received_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
        return [
            {
                "event_id": str(ev.id),
                "case_number": ev.case_number,
                "outcome": (ev.pap_decision or {}).get("outcome"),
                "subject": ev.subject,
                "from_addr": ev.from_addr,
                "received_at": ev.received_at.isoformat() if ev.received_at else None,
                "has_reply_draft": bool(ev.reply_attachment_blob_key),
            }
            for ev in rows
        ]


@mcp.tool()
async def submit_pap_intake(
    program_slug: str,
    intake: dict[str, Any],
    actor: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """Submit a structured PAP intake JSON payload — parallel to the email channel.

    Skips OCR/VLM entirely: the pipeline treats ``intake`` as the raw
    extraction dict, runs retrieve + reason against the program's active
    rules, and returns the verdict inline. Every submission (including
    validation failures) is persisted to the reviewer queue under a new
    ``inbox_events`` row so ops staff can audit MCP-sourced cases the
    same way they audit email intakes.

    ``intake`` keys may be either the slot ``name`` values from the
    program's extraction schema or their ``source_field`` labels
    (whichever the caller already has on hand). Type coercion follows the
    slot definitions; type failures come back as a ``ValueError`` listing
    every offending field.

    ``actor`` is a caller-supplied label surfaced on the case row so
    reviewers can trace which MCP session or downstream integration
    submitted the case — defaults to the API key's short prefix.
    """
    import asyncio as _asyncio

    from pap_pipeline.api_hook import (
        CHANNEL_MCP,
        IntakeValidationFailed,
        PipelineExecutionFailed,
        ProgramNotFound,
        run_json_intake_sync,
    )

    tenant_id = _current_tenant_uuid()
    settings = get_settings()
    actor_label = (actor or "mcp").strip()[:100] or "mcp"

    try:
        result = await _asyncio.to_thread(
            run_json_intake_sync,
            tenant_id,
            program_slug,
            intake,
            settings,
            actor=actor_label,
            channel=CHANNEL_MCP,
            callback_url=callback_url,
        )
    except ProgramNotFound as e:
        raise ValueError(f"Program not found: {e}") from e
    except IntakeValidationFailed as e:
        # Serialize into a ValueError so the MCP client sees a structured
        # message. Include event_id + per-field errors so a Claude Desktop
        # user can see what to fix.
        parts = [f"{err.field}: {err.message}" for err in e.errors]
        if e.missing_required:
            parts.append(f"missing required: {', '.join(e.missing_required)}")
        raise ValueError(
            "intake validation failed — case_event=" + str(e.event_id)
            + "; " + "; ".join(parts)
        ) from e
    except PipelineExecutionFailed as e:
        raise ValueError(
            f"pipeline execution failed (case_event={e.event_id}): {e}"
        ) from e

    return {
        "case_id": result.case_id,
        "event_id": str(result.event_id),
        "status": result.status,
        "review_state": result.review_state,
        "outcome": result.outcome,
        "decision": result.decision,
        "intake": result.intake,
        "unknown_keys": result.unknown_keys,
    }


def build_mcp_app():
    """ASGI app for streamable-http transport, mountable under FastAPI.

    We set `streamable_http_path="/"` so that mounting this app at `/mcp` in the
    parent yields the transport endpoint at `/mcp/` (not `/mcp/mcp/`). The
    session-manager task group must be started by the parent app's lifespan —
    see `docgen.main.lifespan`.

    When served behind a reverse proxy, FastMCP's default DNS-rebinding guard
    (localhost-only Host allowlist) rejects public hostnames with 421. Extend
    it via `DOCGEN_MCP_ALLOWED_HOSTS` (comma-separated FQDNs).
    """
    from mcp.server.transport_security import TransportSecuritySettings

    from docgen.config import get_settings

    raw = get_settings().mcp_allowed_hosts.strip()
    security: TransportSecuritySettings | None = None
    if raw:
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        security = TransportSecuritySettings(allowed_hosts=hosts)
    return mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=security,
    )


def run() -> None:
    """Stdio entrypoint — for direct MCP client integrations (e.g. Claude Desktop)."""
    mcp.run(transport="stdio")
