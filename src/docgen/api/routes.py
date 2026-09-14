"""REST routes — §11.

Thin handlers over the core pipeline. Sync render inline when the source template is small;
otherwise 202 + Dramatiq. Async contract is Day-1 fixed (§10).

Every non-health route is tenant-scoped via `get_tenant_id`, which reads the
tenant resolved by `ApiKeyAuthMiddleware`. Queries filter by tenant; cross-tenant
lookups return 404 (never 403) to avoid leaking existence.
"""
from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.api import idempotency as idem
from docgen.api.deps import get_session, get_tenant_id
from docgen.api.schemas import (
    FreezeRequest,
    FreezeResponse,
    JobStatus,
    MappingResponse,
    ProvenanceEntry,
    PublishEventResponse,
    PublishRequest,
    PublishResponse,
    RenderMappedRequest,
    RenderRequest,
    RenderResponse,
    TemplateSchemaResponse,
    UploadResponse,
)
from docgen.config import get_settings
from docgen.core.docx_sanitize import InvalidDocxError
from docgen.core.mapping import MappingError, apply_mapping
from docgen.core.render_pipeline import (
    execute_merge,
    fetch_template_bytes,
    format_meta,
    persist_result,
)
from docgen.core.render_pipeline_pdf import PdfRenderError
from docgen.core.render_stack import render_stack_hash
from docgen.core.schema_infer import TemplateSchema, validate_payload
from docgen.designtime.freeze import freeze_pdf_version, freeze_version
from docgen.designtime.mapper import propose_mapping_for_version
from docgen.designtime.publish import list_publish_events, publish_version
from docgen.designtime.upload import PdfPreflightRejected, upload_pdf_template, upload_template
from docgen.metrics import observe_preflight_rejection, observe_render
from docgen.net_safety import UrlNotAllowed, validate_public_url
from docgen.providers.base import MappingProposal
from docgen.quotas import QuotaExceeded, check_render_quota
from docgen.store.blob import get_object
from docgen.store.models import (
    CallerProfile,
    Provenance,
    RenderJob,
    Template,
    TemplateVersion,
)


async def _enforce_render_quota(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    try:
        await check_render_quota(session, tenant_id)
    except QuotaExceeded as e:
        raise HTTPException(
            429,
            {
                "detail": "tenant quota exceeded",
                "quota": e.kind,
                "limit": e.limit,
                "current": e.current,
            },
        ) from e

router = APIRouter()
log = structlog.get_logger(__name__)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/templates", response_model=UploadResponse, status_code=201)
async def create_template(
    name: str = Form(...),
    owner: str | None = Form(None),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> UploadResponse:
    fname = (file.filename or "").lower()
    is_docx = fname.endswith(".docx")
    is_pdf = fname.endswith(".pdf")
    if not (is_docx or is_pdf):
        raise HTTPException(400, "Upload must be a .docx or .pdf file")
    file_bytes = await file.read()
    try:
        if is_pdf:
            template, version, report = await upload_pdf_template(
                session, name=name, pdf_bytes=file_bytes, owner=owner, tenant_id=tenant_id
            )
        else:
            template, version, report = await upload_template(
                session, name=name, docx_bytes=file_bytes, owner=owner, tenant_id=tenant_id
            )
    except InvalidDocxError as e:
        raise HTTPException(400, str(e)) from e
    except PdfPreflightRejected as e:
        # Reject at the boundary — no rows or blobs were written. Return the
        # full preflight report so the client sees the finding message + hint
        # and can present them to the operator (Print-to-PDF, encrypted, etc.).
        # Reason label is the first finding's code so ops can slice
        # "encrypted" vs "scanned-image" vs "no-text-layer" without free-form
        # message strings blowing the cardinality budget.
        first_reason = (
            e.report.findings[0].code if e.report.findings else "unknown"
        )
        observe_preflight_rejection(tenant_id=str(tenant_id), reason=first_reason)
        raise HTTPException(400, {"preflight": e.report.to_dict()}) from e
    return UploadResponse(
        template_id=template.id,
        version_id=version.id,
        version=version.version,
        preflight=report.to_dict(),
        accepted=report.outcome != "reject",
    )


@router.post("/templates/{version_id}/map", response_model=MappingResponse)
async def map_version(
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> MappingResponse:
    # Ensure this version belongs to the tenant.
    tv = await _get_version_scoped(session, version_id, tenant_id)
    if tv is None:
        raise HTTPException(404, "Template version not found")
    try:
        proposal = await propose_mapping_for_version(session, version_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return MappingResponse(version_id=version_id, proposal=proposal)


@router.post("/templates/{version_id}/freeze", response_model=FreezeResponse)
async def freeze(
    version_id: uuid.UUID,
    body: FreezeRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> FreezeResponse:
    tv = await _get_version_scoped(session, version_id, tenant_id)
    if tv is None:
        raise HTTPException(404, "Template version not found")
    try:
        if tv.pdf_flavor is not None:
            # PDF freeze ignores the mapping proposal — placements are the
            # schema source. Body suggestions (if any) are silently discarded.
            tv = await freeze_pdf_version(session, version_id=version_id)
        else:
            proposal = MappingProposal(suggestions=body.suggestions, notes=body.notes)
            tv = await freeze_version(session, version_id=version_id, proposal=proposal)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return FreezeResponse(
        template_id=tv.template_id,
        version_id=tv.id,
        version=tv.version,
        version_hash=tv.version_hash,
        is_frozen=tv.is_frozen,
        schema=tv.schema,
    )


@router.post("/templates/{template_id}/publish", response_model=PublishResponse)
async def publish(
    template_id: uuid.UUID,
    body: PublishRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> PublishResponse:
    try:
        _template, event = await publish_version(
            session,
            template_id=template_id,
            tenant_id=tenant_id,
            version=body.version,
            subversion=body.subversion,
            actor=body.actor,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return PublishResponse(
        template_id=template_id,
        version=event.version,
        subversion=event.subversion,
        previous_version=event.previous_version,
        previous_subversion=event.previous_subversion,
        event_kind=event.event_kind,
        actor=event.actor,
    )


@router.get(
    "/templates/{template_id}/publish-events",
    response_model=list[PublishEventResponse],
)
async def publish_events(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> list[PublishEventResponse]:
    try:
        events = await list_publish_events(
            session, template_id=template_id, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return [
        PublishEventResponse(
            version=ev.version,
            subversion=ev.subversion,
            previous_version=ev.previous_version,
            previous_subversion=ev.previous_subversion,
            event_kind=ev.event_kind,
            actor=ev.actor,
            created_at=ev.created_at,
        )
        for ev in events
    ]


@router.get("/templates/{template_id}/schema", response_model=TemplateSchemaResponse)
async def get_schema(
    template_id: uuid.UUID,
    response: Response,
    version: int | None = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> TemplateSchemaResponse | Response:
    """Return the frozen template's schema + derived JSON Schema.

    Cached via `ETag: "{version_hash}"`. Version hashes are content-
    addressed and immutable per frozen version, so a matching
    `If-None-Match` short-circuits with 304 before we compute the JSON
    Schema. The DB hit for tenant + version resolution still happens —
    it's a single indexed lookup, cheaper than the schema derivation
    itself, and skipping it would open a cross-tenant leak surface.
    """
    tv = await _resolve_frozen_version(session, template_id, version, tenant_id)
    etag = f'"{tv.version_hash}"'
    # `?version=N` requests are immutable — v3's schema never changes.
    # No-version-specified resolves to "latest", which flips on freeze,
    # so we keep the TTL short so clients notice new frozen versions.
    if version is not None:
        cache_control = "private, max-age=86400, immutable"
    else:
        cache_control = "private, max-age=60, must-revalidate"
    if if_none_match and etag in {
        v.strip() for v in if_none_match.split(",")
    }:
        response.status_code = 304
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = cache_control
        return response
    ts = TemplateSchema.model_validate(tv.schema)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    return TemplateSchemaResponse(
        template_id=template_id,
        version=tv.version,
        version_hash=tv.version_hash,
        schema=tv.schema,
        json_schema=ts.to_json_schema(),
    )


@router.post("/render", response_model=RenderResponse)
async def render(
    body: RenderRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RenderResponse:
    tv = await _resolve_frozen_version(
        session, body.template_id, body.version, tenant_id, locale=body.locale
    )

    key = _normalize_idempotency_key(idempotency_key)
    request_hash: str | None = None
    if key is not None:
        request_hash = idem.hash_request(_fingerprint_render(body, tv))
        replay = await _handle_idempotent_replay(
            session, tenant_id=tenant_id, key=key, request_hash=request_hash
        )
        if replay is not None:
            return replay

    await _enforce_render_quota(session, tenant_id)

    result = await _execute_render(
        session=session,
        tv=tv,
        tenant_id=tenant_id,
        data=body.data,
        caller=body.caller,
        async_=body.async_,
        output_format=body.output_format,
        callback_url=str(body.callback_url) if body.callback_url is not None else None,
        response=response,
    )
    if key is not None and request_hash is not None:
        try:
            winner = await idem.record(
                session,
                tenant_id=tenant_id,
                key=key,
                request_hash=request_hash,
                job_id=result.job_id,
            )
        except idem.IdempotencyRaceMismatch as e:
            raise HTTPException(409, str(e)) from e
        # On a race with an identical concurrent request, the winner already
        # produced (or is producing) a job — return that job_id so the client's
        # idempotency guarantee holds across retries. Our own job stays in the
        # DB but is orphaned from any key lookup.
        if winner.job_id != result.job_id:
            replay = await _handle_idempotent_replay(
                session, tenant_id=tenant_id, key=key, request_hash=request_hash,
            )
            if replay is not None:
                return replay
    return result


def _normalize_idempotency_key(raw: str | None) -> str | None:
    try:
        return idem.normalize_key(raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _fingerprint_render(body: RenderRequest, tv: TemplateVersion) -> dict:
    """Everything that would change the rendered artifact — no more, no less."""
    return {
        "template_version_id": str(tv.id),
        "data": body.data,
        "output_format": body.output_format,
        "caller": body.caller,
        "callback_url": str(body.callback_url) if body.callback_url is not None else None,
    }


async def _handle_idempotent_replay(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    request_hash: str,
) -> RenderResponse | None:
    existing = await idem.lookup(session, tenant_id, key)
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise HTTPException(
            409,
            "Idempotency-Key already used with a different payload",
        )
    job = await session.get(RenderJob, existing.job_id)
    if job is None:
        # Underlying job cascade-deleted or purged; treat as if there were no key.
        return None
    return RenderResponse(
        job_id=job.id,
        state=job.state,
        inline=(job.state == "succeeded" and job.result_blob_key is not None),
        output_format=job.output_format,  # type: ignore[arg-type]
    )


@router.post("/render/mapped", response_model=RenderResponse)
async def render_mapped(
    body: RenderMappedRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RenderResponse:
    """Render using a saved caller profile.

    The profile pins (template, version) and holds the field mapping. The caller
    ships their native JSON as `data`; we translate it to the template's schema
    shape, then delegate to the standard render pipeline.

    Idempotent under the same rules as `/render`: if `Idempotency-Key` is
    present, a replay with the same key + same fingerprint returns the
    original job. A same-key + different-payload replay is a 409. The
    fingerprint hashes the *mapped* payload rather than the raw body so
    a downstream profile edit doesn't silently share an idempotency
    window with the pre-edit call.
    """
    profile = await session.get(CallerProfile, body.profile_id)
    if profile is None or profile.tenant_id != tenant_id:
        raise HTTPException(404, "Caller profile not found")

    tv = await _resolve_frozen_version(
        session, profile.template_id, profile.version, tenant_id, locale=body.locale
    )
    ts = TemplateSchema.model_validate(tv.schema)
    required = {f.name for f in ts.fields if f.required}
    try:
        mapped = apply_mapping(profile.mapping, body.data, required=required)
    except MappingError as e:
        raise HTTPException(422, {"mapping_errors": e.errors}) from e

    key = _normalize_idempotency_key(idempotency_key)
    request_hash: str | None = None
    if key is not None:
        request_hash = idem.hash_request(_fingerprint_render_mapped(body, tv, mapped, profile))
        replay = await _handle_idempotent_replay(
            session, tenant_id=tenant_id, key=key, request_hash=request_hash,
        )
        if replay is not None:
            return replay

    await _enforce_render_quota(session, tenant_id)

    result = await _execute_render(
        session=session,
        tv=tv,
        tenant_id=tenant_id,
        data=mapped,
        caller=body.caller or profile.caller,
        async_=body.async_,
        output_format=body.output_format,
        callback_url=str(body.callback_url) if body.callback_url is not None else None,
        response=response,
    )
    if key is not None and request_hash is not None:
        try:
            winner = await idem.record(
                session,
                tenant_id=tenant_id,
                key=key,
                request_hash=request_hash,
                job_id=result.job_id,
            )
        except idem.IdempotencyRaceMismatch as e:
            raise HTTPException(409, str(e)) from e
        if winner.job_id != result.job_id:
            replay = await _handle_idempotent_replay(
                session, tenant_id=tenant_id, key=key, request_hash=request_hash,
            )
            if replay is not None:
                return replay
    return result


def _fingerprint_render_mapped(
    body: RenderMappedRequest,
    tv: TemplateVersion,
    mapped: dict,
    profile: CallerProfile,
) -> dict:
    """Fingerprint the mapped-render call. Uses `mapped` (post-translation)
    rather than raw `body.data` so a same-key retry after a profile edit
    doesn't return the pre-edit job — behavior would silently diverge
    from the caller's current expectations otherwise."""
    return {
        "template_version_id": str(tv.id),
        "profile_id": str(profile.id),
        "data": mapped,
        "output_format": body.output_format,
        "caller": body.caller or profile.caller,
        "callback_url": str(body.callback_url) if body.callback_url is not None else None,
    }


async def _execute_render(
    *,
    session: AsyncSession,
    tv: TemplateVersion,
    tenant_id: uuid.UUID,
    data: dict,
    caller: str | None,
    async_: bool,
    output_format: str,
    callback_url: str | None,
    response: Response,
) -> RenderResponse:
    settings = get_settings()

    if callback_url is not None:
        try:
            validate_public_url(callback_url)
        except UrlNotAllowed as e:
            raise HTTPException(422, f"callback_url rejected: {e}") from e

    ok, errors = validate_payload(tv.schema, data)
    if not ok:
        raise HTTPException(422, {"validation_errors": errors})

    # Fast-fail PDF templates with no placements — otherwise we'd enqueue a
    # doomed job and force the caller to poll to discover an obvious 4xx.
    if tv.pdf_flavor is not None:
        placements = (tv.pdf_field_placements or {}).get("placements") or []
        if not placements:
            raise HTTPException(
                422,
                "PDF template has no field placements — open it in the editor "
                "and place fields before rendering.",
            )
        # PDF-in / PDF-out. Silently coerce so a shared client payload template
        # that defaults output_format="docx" still works against PDF templates.
        output_format = "pdf"

    job = RenderJob(
        tenant_id=tenant_id,
        template_version_id=tv.id,
        state="queued",
        caller=caller,
        payload=data,
        output_format=output_format,
        callback_url=callback_url,
        webhook_state="pending" if callback_url else "not_configured",
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    template_bytes = fetch_template_bytes(tv, settings=settings)
    inline_eligible = (not async_) and (len(template_bytes) <= settings.sync_render_max_bytes)

    if inline_eligible:
        started_at = datetime.now(UTC)
        try:
            output = execute_merge(tv, data, output_format, settings=settings)
        except PdfRenderError as e:
            job.state = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.completed_at = datetime.now(UTC)
            await session.commit()
            log.info(
                "render_rejected", job_id=str(job.id), template_id=str(tv.template_id),
                tenant_id=str(tenant_id),
                version=tv.version, mode="inline", format=output_format, error=job.error,
            )
            observe_render(
                tenant_id=str(tenant_id),
                format=output_format,
                state="failed",
                duration_seconds=(datetime.now(UTC) - started_at).total_seconds(),
            )
            if callback_url:
                _enqueue_webhook(str(job.id))
            raise HTTPException(422, str(e)) from e
        except Exception as e:
            job.state = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.completed_at = datetime.now(UTC)
            await session.commit()
            log.error(
                "render_failed", job_id=str(job.id), template_id=str(tv.template_id),
                tenant_id=str(tenant_id),
                version=tv.version, mode="inline", format=output_format, error=job.error,
            )
            observe_render(
                tenant_id=str(tenant_id),
                format=output_format,
                state="failed",
                duration_seconds=(datetime.now(UTC) - started_at).total_seconds(),
            )
            if callback_url:
                _enqueue_webhook(str(job.id))
            raise HTTPException(500, f"Render failed: {e}") from e
        result_key = persist_result(tv, job.id, output, output_format, settings=settings)
        job.state = "succeeded"
        job.result_blob_key = result_key
        job.result_bytes = len(output)
        job.completed_at = datetime.now(UTC)
        session.add(
            Provenance(
                render_job_id=job.id,
                template_version_id=tv.id,
                version_hash=tv.version_hash,
                render_stack_hash=render_stack_hash(),
                caller=caller,
            )
        )
        await session.commit()
        log.info(
            "render_succeeded", job_id=str(job.id), template_id=str(tv.template_id),
            tenant_id=str(tenant_id),
            version=tv.version, version_hash=tv.version_hash,
            mode="inline", format=output_format,
            result_bytes=job.result_bytes, caller=caller,
        )
        observe_render(
            tenant_id=str(tenant_id),
            format=output_format,
            state="succeeded",
            duration_seconds=(datetime.now(UTC) - started_at).total_seconds(),
        )
        if callback_url:
            _enqueue_webhook(str(job.id))
        return RenderResponse(
            job_id=job.id, state=job.state, inline=True, output_format=output_format
        )

    from docgen.jobs.render import render_job as render_actor

    try:
        render_actor.send(str(job.id))
    except Exception as e:
        # Broker unreachable — mark the row failed so it isn't left orphaned in
        # "queued" forever, then surface the failure to the caller.
        job.state = "failed"
        job.error = f"enqueue_failed: {type(e).__name__}: {e}"
        job.completed_at = datetime.now(UTC)
        await session.commit()
        log.exception(
            "render_enqueue_failed", job_id=str(job.id),
            template_id=str(tv.template_id), tenant_id=str(tenant_id),
        )
        raise HTTPException(503, "Render broker unavailable; try again shortly") from e

    log.info(
        "render_enqueued", job_id=str(job.id), template_id=str(tv.template_id),
        tenant_id=str(tenant_id),
        version=tv.version, format=output_format, caller=caller,
    )
    response.status_code = 202
    return RenderResponse(
        job_id=job.id, state="queued", inline=False, output_format=output_format
    )


def _enqueue_webhook(job_id: str) -> None:
    """Send the webhook actor if it can be enqueued; log & swallow otherwise."""
    try:
        from docgen.jobs.webhooks import deliver_webhook

        deliver_webhook.send(job_id)
    except Exception:
        log.exception("webhook_enqueue_failed", job_id=job_id)




@router.get("/jobs/{job_id}", response_model=JobStatus)
async def job_status(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> JobStatus:
    job = await session.get(RenderJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(404, "Job not found")
    return JobStatus(
        job_id=job.id,
        state=job.state,
        submitted_at=job.submitted_at,
        completed_at=job.completed_at,
        error=job.error,
        result_bytes=job.result_bytes,
        output_format=job.output_format,  # type: ignore[arg-type]
    )


@router.get("/jobs/{job_id}/result")
async def job_result(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> StreamingResponse:
    job = await session.get(RenderJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(404, "Job not found")
    if job.state != "succeeded" or not job.result_blob_key:
        raise HTTPException(409, f"Job not ready (state={job.state})")
    data = get_object(get_settings().s3_bucket_documents, job.result_blob_key)
    ext, ctype = format_meta(job.output_format)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=ctype,
        headers={"Content-Disposition": f'attachment; filename="{job_id}.{ext}"'},
    )


@router.get("/templates/{template_id}/provenance", response_model=list[ProvenanceEntry])
async def provenance(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> list[ProvenanceEntry]:
    # 404 if template belongs to another tenant.
    template = await session.get(Template, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(404, "Template not found")

    result = await session.execute(
        select(Provenance)
        .join(TemplateVersion, TemplateVersion.id == Provenance.template_version_id)
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(TemplateVersion.template_id == template_id)
        .where(Template.tenant_id == tenant_id)
        .order_by(Provenance.rendered_at.desc())
    )
    return [
        ProvenanceEntry(
            render_job_id=p.render_job_id,
            version_hash=p.version_hash,
            caller=p.caller,
            rendered_at=p.rendered_at,
        )
        for p in result.scalars().all()
    ]


async def _get_version_scoped(
    session: AsyncSession, version_id: uuid.UUID, tenant_id: uuid.UUID
) -> TemplateVersion | None:
    result = await session.execute(
        select(TemplateVersion)
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(TemplateVersion.id == version_id)
        .where(Template.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def _resolve_frozen_version(
    session: AsyncSession,
    template_id: uuid.UUID,
    version: int | None,
    tenant_id: uuid.UUID,
    locale: str | None = None,
) -> TemplateVersion:
    template = await session.get(Template, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(404, "Template not found")
    if not template.is_active:
        raise HTTPException(409, "Template is inactive")
    # When the caller passes `version` explicitly, we resolve the highest
    # frozen sub for that version — v1 alone should mean "current v1 tip".
    # When they don't, we pin to the published (version, subversion) pair
    # so runtime keeps hitting exactly what publish blessed.
    if version is None:
        version = template.current_version
        pinned_sub = template.current_subversion
    else:
        pinned_sub = None
    if version is None:
        raise HTTPException(409, "Template has no frozen version yet")
    target_locale = locale or "default"
    q = select(TemplateVersion).where(
        TemplateVersion.template_id == template_id,
        TemplateVersion.version == version,
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
        raise HTTPException(
            404,
            f"Frozen version {version} (locale '{target_locale}') "
            f"not found for template {template_id}",
        )
    return tv
