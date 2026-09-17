"""Admin portal — server-rendered UI over the same domain layer as REST.

Deliberately thin: every UI action delegates to the same designtime/render code the
REST layer calls. The portal introduces zero business logic of its own — that keeps
the boundary tests meaningful and the audit story identical for REST- and UI-triggered
operations.

Auth: `AdminSessionMiddleware` gates every /ui/* except /ui/login and /ui/logout.
The admin is a superuser: routes list ALL tenants.

# tenant-audit: file-allow (admin superuser — every UI route is intentionally
# cross-tenant; the workspace-picker IDOR window is closed by `_enforce_scope`
# on the hot paths, not by per-query tenant_id filters).
"""
from __future__ import annotations

import io
import json
import math
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.api.deps import get_session
from docgen.auth import (
    CSRF_COOKIE,
    CSRF_TOKEN_BYTES,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    authenticate_admin,
    enforce_csrf_if_mutating,
    generate_api_key,
    sign_session,
)
from docgen.config import get_settings
from docgen.core.docx_sanitize import InvalidDocxError
from docgen.core.mapping import MappingError, apply_mapping, suggest_mapping, walk_leaves
from docgen.core.render_pipeline import (
    FORMAT_META,
    execute_merge,
    format_meta,
    persist_result,
)
from docgen.core.render_stack import render_stack_hash
from docgen.core.schema_infer import TemplateSchema, validate_payload
from docgen.designtime.audit import record_audit_event
from docgen.designtime.diff import compute_template_diff
from docgen.designtime.freeze import (
    apply_required_edits,
    create_pdf_subversion,
    freeze_pdf_version,
    freeze_version,
    refreeze_docx_subversion,
    supersede_docx_version,
)
from docgen.designtime.mapper import propose_mapping_for_version
from docgen.designtime.publish import list_publish_events, publish_version
from docgen.designtime.upload import (
    PdfPreflightRejected,
    upload_locale_variant,
    upload_pdf_template,
    upload_template,
)
from docgen.metrics import observe_render
from docgen.pagination import (
    Cursor,
    Page,
    clamp_page_size,
    keyset_where,
)
from docgen.providers.base import FieldSpec, MappingProposal, Suggestion
from docgen.providers.nebius_catalog import get_cached_catalog
from docgen.quotas import QuotaExceeded, check_active_template_quota, check_render_quota
from docgen.ratelimit import (
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    check_login_attempt,
    client_ip,
    reset_login_attempts,
)
from docgen.security_headers import preview_csp, secure_cookie_flag
from docgen.store.blob import get_object, put_object
from docgen.store.models import (
    ApiKey,
    CallerProfile,
    DocPackage,
    InboxEvent,
    InboxTestResult,
    Program,
    ProgramRuleLink,
    Provenance,
    PublishEvent,
    RenderJob,
    Rule,
    RuleVersion,
    Template,
    TemplateAuditEvent,
    TemplateVersion,
    Tenant,
)
from pap_pipeline.stages.slot_compile import SlotCompileError, SlotDef

router = APIRouter(
    prefix="/ui", tags=["ui"], include_in_schema=False,
    dependencies=[Depends(enforce_csrf_if_mutating)],
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
log = structlog.get_logger(__name__)


def _csrf_input(request: Request) -> str:
    """Jinja global: render the hidden CSRF field inside a <form>."""
    from markupsafe import Markup

    tok = getattr(request.state, "csrf_token", "") or ""
    return Markup(f'<input type="hidden" name="csrf_token" value="{tok}">')


def _csp_nonce(request: Request) -> str:
    """Jinja global: nonce to stamp on <script>/<style> tags so CSP accepts them."""
    return getattr(request.state, "csp_nonce", "") or ""


templates.env.globals["csrf_input"] = _csrf_input
templates.env.globals["csp_nonce"] = _csp_nonce


def _case_id_filter(case_number: int | None) -> str:
    """Jinja filter: format a case number as `QC-0042`.
    Mirrors InboxEvent.case_id so list rows (which are tuples, not ORM
    instances) can render the same shorthand.
    """
    if case_number is None:
        return "QC-????"
    return f"QC-{int(case_number):04d}"


templates.env.filters["case_id"] = _case_id_filter


import re as _re_base  # noqa: E402 — kept adjacent to the regex it compiles

# Matches default widget names pdfjs / Acrobat auto-assign when a form field
# was drawn without a caption ("Text Field 179", "Check Box 3", "field_12").
# The AI labeler leaves these unchanged when it can't find a nearby caption,
# so highlighting them nudges users to rename the ones that matter.
_UNNAMED_PDF_FIELD = _re_base.compile(
    r"^(?:text[\s_]*field|check[\s_]*box|radio[\s_]*button|button|field)[\s_]*\d+$",
    _re_base.IGNORECASE,
)


def _is_unnamed_pdf_field(name: str | None) -> bool:
    return bool(name and _UNNAMED_PDF_FIELD.match(name.strip()))


templates.env.tests["unnamed_pdf_field"] = _is_unnamed_pdf_field

_URL_TENANT_SEG = _re_base.compile(
    r"^/ui/admin/tenants/([0-9a-fA-F-]{36})(?:/.*)?$"
)


# flash_kind flows in from URL query params ("?flash=...&flash_kind=..."), so
# an attacker can seed arbitrary strings that a victim would render into a CSS
# class + data-* attribute. Autoescape stops the obvious XSS but the attacker
# could still smuggle unknown class names or break JS toast logic that expects
# known kinds. Whitelist to the four we render.
_FLASH_KINDS = frozenset({"info", "success", "warning", "error"})


def _normalize_flash_kind(v: str | None) -> str | None:
    if v is None:
        return None
    return v if v in _FLASH_KINDS else "info"


async def _base_ctx(
    request: Request, session: AsyncSession | None = None, **extra: Any
) -> dict[str, Any]:
    """Base template context: admin_user, tenant list for the sidebar workspace
    picker, and the currently-selected tenant.

    The sidebar picker's selected state prefers the URL when the current page is
    tenant-scoped (e.g. .../tenants/<uuid>/keys), so navigating directly to a
    different tenant's page shows the correct workspace even if the cookie is
    stale. Elsewhere, the cookie set by the picker wins.

    Pass a `session` when rendering sidebar-enabled pages so the picker has data.
    """
    if "flash_kind" in extra:
        extra["flash_kind"] = _normalize_flash_kind(extra["flash_kind"])
    identity = _identity(request)
    ctx: dict[str, Any] = {
        "admin_user": getattr(request.state, "admin_user", None),
        "admin_role": identity.role if identity is not None else None,
        "is_super_admin": identity is not None and identity.role == "super_admin",
    }
    if session is not None:
        from docgen.store.models import Tenant  # local to avoid circular imports
        # Reuse the caller's tenant list when it was already fetched at the
        # top of the route (dashboard, index, renders, upload, etc.). Saves
        # a redundant `SELECT name, id FROM tenants ORDER BY name` per hit.
        preloaded = extra.get("all_tenants")
        if preloaded is not None:
            tenants = preloaded
        else:
            stmt = select(Tenant).order_by(Tenant.name.asc())
            # tenant_admin should only see their own tenant in the workspace
            # picker; the sidebar list becomes single-item and effectively
            # informational.
            if identity is not None and identity.role == "tenant_admin" and identity.tenant_id is not None:
                stmt = stmt.where(Tenant.id == identity.tenant_id)
            result = await session.execute(stmt)
            tenants = result.scalars().all()
        ctx["sidebar_tenants"] = tenants
        selected: str | None = None
        # tenant_admin's picker is pinned to their tenant regardless of URL/cookie.
        if identity is not None and identity.role == "tenant_admin" and identity.tenant_id is not None:
            selected = str(identity.tenant_id)
        else:
            m = _URL_TENANT_SEG.match(request.url.path or "")
            if m:
                selected = m.group(1)
            else:
                cookie_tid = request.cookies.get("docgen_ui_tenant")
                if cookie_tid:
                    selected = cookie_tid
        ctx["sidebar_selected_tenant_id"] = selected

        if identity is not None:
            from sqlalchemy import func as _sqlfunc
            count_q = select(_sqlfunc.count(InboxEvent.id)).where(
                InboxEvent.review_state == "pending_review",
                InboxEvent.reply_message_id.is_(None),
            )
            if identity.role == "tenant_admin" and identity.tenant_id is not None:
                count_q = count_q.where(InboxEvent.tenant_id == identity.tenant_id)
            elif selected:
                try:
                    count_q = count_q.where(InboxEvent.tenant_id == uuid.UUID(selected))
                except ValueError:
                    pass
            ctx["humanloop_count"] = int((await session.execute(count_q)).scalar() or 0)
    ctx.update(extra)
    return ctx


DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _identity(request: Request):
    """Resolved AdminIdentity for this request, set by AdminSessionMiddleware."""
    return getattr(request.state, "admin_identity", None)


def _is_super_admin(request: Request) -> bool:
    ident = _identity(request)
    return ident is not None and ident.role == "super_admin"


def _require_super_admin(request: Request) -> None:
    """Guard for routes that only a super_admin may access (create tenant,
    list all tenants, cross-tenant admin actions). 404 (not 403) so the URL's
    existence isn't disclosed to lower-privilege users."""
    if not _is_super_admin(request):
        raise HTTPException(404, "Not found")


def _sidebar_scope(request: Request) -> uuid.UUID | None:
    """Tenant selected in the sidebar workspace picker, if any."""
    cookie = request.cookies.get("docgen_ui_tenant")
    if not cookie:
        return None
    try:
        return uuid.UUID(cookie)
    except ValueError:
        return None


def _forced_tenant_scope(request: Request) -> uuid.UUID | None:
    """Effective tenant scope for THIS request.

    tenant_admin → always their tenant (sidebar cookie is ignored).
    super_admin  → whatever the sidebar picker chose (may be None = all).
    """
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin":
        return ident.tenant_id
    return _sidebar_scope(request)


def _enforce_scope(request: Request, resource_tenant_id: uuid.UUID) -> None:
    """Reject when the effective scope disagrees with the resource's tenant.

    Uses 404 (not 403) to avoid leaking whether the resource exists, mirroring
    the REST API's tenant-scoping convention. For a tenant_admin this is an
    immovable check; for a super_admin it's the workspace-picker's opt-in
    isolation.
    """
    scope = _forced_tenant_scope(request)
    if scope is not None and scope != resource_tenant_id:
        raise HTTPException(404, "Not found")


async def _enforce_version_scope(
    request: Request, session: AsyncSession, tv: TemplateVersion
) -> None:
    """Enforce tenant scope for a route keyed by TemplateVersion.

    Load-avoidant: only fetches the parent Template when a scope is actually
    in force (tenant_admin, or super_admin with an active workspace picker).
    In the common super_admin-no-cookie case this is a no-op.
    """
    scope = _forced_tenant_scope(request)
    if scope is None:
        return
    template = await session.get(Template, tv.template_id)
    if template is None or template.tenant_id != scope:
        raise HTTPException(404, "Not found")


async def _visible_tenant_ids(
    request: Request, session: AsyncSession
) -> list[uuid.UUID] | None:
    """Tenant IDs the current identity is allowed to see.

    Returns None to mean "no filter" (super_admin, cross-tenant). For
    tenant_admin returns a single-element list — used by list-view queries
    that would otherwise SELECT across all tenants.
    """
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        return [ident.tenant_id]
    return None


def _resolve_list_tenant_filter(
    request: Request, tenant: str | None
) -> uuid.UUID | None:
    """Same tenant-filter resolution used by index/dashboard/renders/etc.

    tenant_admin:  forced to their tenant (?tenant / cookie ignored).
    super_admin:   ?tenant=<uuid> wins, then sidebar cookie, else None (all).
                   `?tenant=` empty string = explicit "all", ignore cookie.
    """
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        return ident.tenant_id
    explicit = "tenant" in request.query_params
    if explicit:
        if tenant:
            try:
                return uuid.UUID(tenant)
            except ValueError:
                return None
        return None
    cookie_tid = request.cookies.get("docgen_ui_tenant")
    if cookie_tid:
        try:
            return uuid.UUID(cookie_tid)
        except ValueError:
            return None
    return None


async def _get_or_default_tenant_id(
    session: AsyncSession,
    req_tenant: uuid.UUID | None,
    request: Request | None = None,
) -> uuid.UUID:
    """Pick a tenant for UI-originated actions.

    For a tenant_admin, this is always their tenant — a spoofed `req_tenant`
    from a form field is ignored. For a super_admin, `req_tenant` wins;
    falls back to the sole tenant or the well-known default when unset.
    """
    if request is not None:
        ident = _identity(request)
        if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
            return ident.tenant_id
    if req_tenant is not None:
        return req_tenant
    from docgen.store.models import Tenant  # local to avoid circular imports
    result = await session.execute(select(Tenant.id).order_by(Tenant.created_at.asc()))
    tenant_ids = [row[0] for row in result.all()]
    if len(tenant_ids) == 1:
        return tenant_ids[0]
    if DEFAULT_TENANT_ID in tenant_ids:
        return DEFAULT_TENANT_ID
    if tenant_ids:
        return tenant_ids[0]
    raise HTTPException(500, "No tenants exist. Create one via /ui/admin/tenants.")




@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tenant: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
    show_inactive: int = 0,
) -> HTMLResponse:
    from docgen.store.models import Tenant  # local to avoid circular import issues

    tenants_stmt = select(Tenant).order_by(Tenant.name.asc())
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenants_stmt = tenants_stmt.where(Tenant.id == ident.tenant_id)
    tenants_res = await session.execute(tenants_stmt)
    all_tenants = tenants_res.scalars().all()

    tenant_uuid = _resolve_list_tenant_filter(request, tenant)

    page_size = clamp_page_size(limit)
    cur = Cursor.decode(cursor)

    # Subquery: last render timestamp per template, so the list can show
    # freshness at a glance instead of just when the template was created.
    last_render_sq = (
        select(
            TemplateVersion.template_id.label("tid"),
            func.max(RenderJob.submitted_at).label("last_rendered_at"),
        )
        .join(RenderJob, RenderJob.template_version_id == TemplateVersion.id)
        .group_by(TemplateVersion.template_id)
        .subquery()
    )

    query = (
        select(
            Template,
            Tenant.name.label("tenant_name"),
            func.count(TemplateVersion.id).label("version_count"),
            last_render_sq.c.last_rendered_at,
        )
        .join(Tenant, Tenant.id == Template.tenant_id)
        .join(TemplateVersion, TemplateVersion.template_id == Template.id, isouter=True)
        .join(last_render_sq, last_render_sq.c.tid == Template.id, isouter=True)
        .group_by(Template.id, Tenant.name, last_render_sq.c.last_rendered_at)
        .order_by(Template.created_at.desc(), Template.id.desc())
        .limit(page_size + 1)
    )
    if not show_inactive:
        query = query.where(Template.is_active.is_(True))
    if tenant_uuid is not None:
        query = query.where(Template.tenant_id == tenant_uuid)
    keyset = keyset_where(Template.created_at, Template.id, cur)
    if keyset is not None:
        query = query.where(keyset)
    rows = (await session.execute(query)).all()

    items: list[Any] = []
    for template, tenant_name, version_count, last_rendered_at in rows:
        items.append(
            type(
                "TemplateRow",
                (),
                {
                    "id": template.id,
                    "name": template.name,
                    "owner": template.owner,
                    "current_version": template.current_version,
                    "created_at": template.created_at,
                    "last_rendered_at": last_rendered_at,
                    "version_count": version_count,
                    "tenant_name": tenant_name,
                    "tenant_id": template.tenant_id,
                    "is_active": template.is_active,
                },
            )
        )
    page = Page.from_rows(
        items,
        page_size,
        cursor_from_row=lambda row: Cursor(ts=row.created_at, id=row.id),
    )

    stats = await _core_stats(session, tenant_uuid)

    return templates.TemplateResponse(
        request,
        "index.html",
        await _base_ctx(
            request,
            session,
            templates=page.items,
            active="index",
            all_tenants=all_tenants,
            selected_tenant=tenant_uuid,
            stats=stats,
            next_cursor=page.next_cursor,
            is_paged=cur is not None,
            page_size=page_size,
            show_inactive=bool(show_inactive),
        ),
    )


async def _core_stats(
    session: AsyncSession,
    tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """Compute the shared operational stat-cards (templates landing + dashboard).

    All counts are SQL-side so the caller doesn't have to hydrate the full
    template list just to count it. Every query respects tenant scoping.
    """
    now = datetime.now(UTC)
    cutoff_24h = now - timedelta(hours=24)

    # `total` and `frozen` scope to active templates so the top-of-card
    # number matches what the operator can see on /ui/ (which hides
    # inactive). `inactive` stays as a separate side-count so the
    # breakdown adds up cleanly: total = frozen + draft, inactive shown
    # alongside as an "elsewhere" indicator.
    tmpl_q = select(
        func.count(Template.id).filter(Template.is_active.is_(True)).label("total"),
        func.count(Template.id).filter(
            Template.is_active.is_(True), Template.current_version.is_not(None)
        ).label("frozen"),
        func.count(Template.id).filter(Template.is_active.is_(False)).label("inactive"),
    )
    if tenant_uuid is not None:
        tmpl_q = tmpl_q.where(Template.tenant_id == tenant_uuid)
    tmpl_row = (await session.execute(tmpl_q)).one()
    templates_total = tmpl_row.total or 0
    templates_frozen = tmpl_row.frozen or 0
    templates_inactive = tmpl_row.inactive or 0

    jobs_q = (
        select(RenderJob.state, func.count().label("c"))
        .where(RenderJob.submitted_at >= cutoff_24h)
        .group_by(RenderJob.state)
    )
    if tenant_uuid is not None:
        jobs_q = jobs_q.where(RenderJob.tenant_id == tenant_uuid)
    jobs_by_state = {
        r.state: r.c for r in (await session.execute(jobs_q)).all()
    }

    # "Pending drafts": templates whose newest default-locale version is a
    # non-rejected draft — i.e. someone uploaded a docx and it's sitting
    # waiting for a human to review & freeze.
    latest_v_sq = (
        select(
            TemplateVersion.template_id.label("tid"),
            func.max(TemplateVersion.version).label("max_v"),
        )
        .where(TemplateVersion.locale == "default")
        .group_by(TemplateVersion.template_id)
        .subquery()
    )
    # Always join Template so we can exclude soft-deleted (is_active=False)
    # ones — a draft on an inactive template is unreachable from the UI, so
    # counting it just leaves a ghost badge on the attention rail.
    pending_q = (
        select(func.count(func.distinct(TemplateVersion.template_id)))
        .join(
            latest_v_sq,
            (latest_v_sq.c.tid == TemplateVersion.template_id)
            & (latest_v_sq.c.max_v == TemplateVersion.version),
        )
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(
            TemplateVersion.locale == "default",
            TemplateVersion.is_frozen.is_(False),
            TemplateVersion.status != "rejected",
            Template.is_active.is_(True),
        )
    )
    if tenant_uuid is not None:
        pending_q = pending_q.where(Template.tenant_id == tenant_uuid)
    pending_drafts = (await session.execute(pending_q)).scalar_one() or 0

    warn_q = (
        select(func.count(func.distinct(TemplateVersion.template_id)))
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(
            TemplateVersion.is_frozen.is_(False),
            TemplateVersion.status != "rejected",
            TemplateVersion.preflight["outcome"].astext == "accept-with-warnings",
            Template.is_active.is_(True),
        )
    )
    if tenant_uuid is not None:
        warn_q = warn_q.where(Template.tenant_id == tenant_uuid)
    warnings_count = (await session.execute(warn_q)).scalar_one() or 0

    return {
        "templates_total": templates_total,
        "templates_frozen": templates_frozen,
        "templates_draft": templates_total - templates_frozen,
        "templates_inactive": templates_inactive,
        "pending_drafts": pending_drafts,
        "warnings_count": warnings_count,
        "renders_24h_total": sum(jobs_by_state.values()),
        "renders_24h_succeeded": jobs_by_state.get("succeeded", 0),
        "renders_24h_failed": jobs_by_state.get("failed", 0),
        "renders_24h_in_flight": (
            jobs_by_state.get("queued", 0) + jobs_by_state.get("running", 0)
        ),
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tenant: str | None = None,
) -> HTMLResponse:
    """Operational dashboard: at-a-glance stats + recent activity + failures."""
    from docgen.store.models import Tenant

    tenants_stmt = select(Tenant).order_by(Tenant.name.asc())
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenants_stmt = tenants_stmt.where(Tenant.id == ident.tenant_id)
    tenants_res = await session.execute(tenants_stmt)
    all_tenants = tenants_res.scalars().all()

    tenant_uuid = _resolve_list_tenant_filter(request, tenant)

    stats = await _core_stats(session, tenant_uuid)
    sparkline = await _sparkline_24h(session, tenant_uuid)
    now = datetime.now(UTC)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    # Recent renders (any state) — join through the version to get template name.
    recent_renders_q = (
        select(
            RenderJob.id,
            RenderJob.state,
            RenderJob.caller,
            RenderJob.submitted_at,
            RenderJob.completed_at,
            Template.name.label("template_name"),
            Template.id.label("template_id"),
        )
        .join(TemplateVersion, TemplateVersion.id == RenderJob.template_version_id)
        .join(Template, Template.id == TemplateVersion.template_id)
        .order_by(RenderJob.submitted_at.desc())
        .limit(10)
    )
    if tenant_uuid is not None:
        recent_renders_q = recent_renders_q.where(RenderJob.tenant_id == tenant_uuid)
    recent_renders = [dict(r._mapping) for r in (await session.execute(recent_renders_q)).all()]

    recent_pub_q = (
        select(
            PublishEvent.id,
            PublishEvent.event_kind,
            PublishEvent.version,
            PublishEvent.previous_version,
            PublishEvent.actor,
            PublishEvent.created_at,
            Template.name.label("template_name"),
            Template.id.label("template_id"),
        )
        .join(Template, Template.id == PublishEvent.template_id)
        .order_by(PublishEvent.created_at.desc())
        .limit(10)
    )
    if tenant_uuid is not None:
        recent_pub_q = recent_pub_q.where(Template.tenant_id == tenant_uuid)
    recent_publishes = [dict(r._mapping) for r in (await session.execute(recent_pub_q)).all()]

    # Failing jobs in the last 7 days — surface for triage.
    fail_q = (
        select(
            RenderJob.id,
            RenderJob.caller,
            RenderJob.error,
            RenderJob.submitted_at,
            Template.name.label("template_name"),
            Template.id.label("template_id"),
        )
        .join(TemplateVersion, TemplateVersion.id == RenderJob.template_version_id)
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(RenderJob.state == "failed", RenderJob.submitted_at >= cutoff_7d)
        .order_by(RenderJob.submitted_at.desc())
        .limit(10)
    )
    if tenant_uuid is not None:
        fail_q = fail_q.where(RenderJob.tenant_id == tenant_uuid)
    failing_jobs = [dict(r._mapping) for r in (await session.execute(fail_q)).all()]

    top_q = (
        select(
            Template.id.label("template_id"),
            Template.name.label("template_name"),
            func.count(RenderJob.id).label("render_count"),
        )
        .join(TemplateVersion, TemplateVersion.template_id == Template.id)
        .join(RenderJob, RenderJob.template_version_id == TemplateVersion.id)
        .where(RenderJob.submitted_at >= cutoff_30d)
        .group_by(Template.id, Template.name)
        .order_by(func.count(RenderJob.id).desc())
        .limit(10)
    )
    if tenant_uuid is not None:
        top_q = top_q.where(RenderJob.tenant_id == tenant_uuid)
    top_templates = [dict(r._mapping) for r in (await session.execute(top_q)).all()]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        await _base_ctx(
            request,
            session,
            active="dashboard",
            all_tenants=all_tenants,
            selected_tenant=tenant_uuid,
            stats=stats,
            sparkline=sparkline,
            recent_renders=recent_renders,
            recent_publishes=recent_publishes,
            failing_jobs=failing_jobs,
            top_templates=top_templates,
        ),
    )


async def _sparkline_24h(
    session: AsyncSession, tenant_uuid: uuid.UUID | None
) -> list[dict[str, Any]]:
    """24 hourly buckets ending at the current hour — shared by the HTML render
    of the dashboard and the polling JSON endpoint."""
    now = datetime.now(UTC)
    cutoff_24h = now - timedelta(hours=24)
    q = select(RenderJob.submitted_at, RenderJob.state).where(
        RenderJob.submitted_at >= cutoff_24h
    )
    if tenant_uuid is not None:
        q = q.where(RenderJob.tenant_id == tenant_uuid)
    end_hr = now.replace(minute=0, second=0, microsecond=0)
    bucket_starts = [end_hr - timedelta(hours=(23 - i)) for i in range(24)]
    counts = [{"total": 0, "failed": 0} for _ in range(24)]
    for submitted_at, state in (await session.execute(q)).all():
        diff_h = int((now - submitted_at).total_seconds() // 3600)
        idx = 23 - diff_h
        if 0 <= idx < 24:
            counts[idx]["total"] += 1
            if state == "failed":
                counts[idx]["failed"] += 1
    max_bucket = max((c["total"] for c in counts), default=0)
    return [
        {
            "hr": bucket_starts[i].strftime("%H:00"),
            "total": counts[i]["total"],
            "failed": counts[i]["failed"],
            "pct": (counts[i]["total"] / max_bucket * 100) if max_bucket else 0,
        }
        for i in range(24)
    ]


@router.get("/dashboard.json")
async def dashboard_json(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tenant: str | None = None,
) -> JSONResponse:
    """Live-refresh feed for the dashboard — stats + sparkline only.

    Recent-activity lists (renders, publishes, failures, top templates) are
    intentionally left out: they change infrequently and refreshing them
    would cause jarring scroll shifts. The dashboard polls this every 30s.
    """
    tenant_uuid = _resolve_list_tenant_filter(request, tenant)

    stats = await _core_stats(session, tenant_uuid)
    sparkline = await _sparkline_24h(session, tenant_uuid)
    return JSONResponse(
        {
            "stats": stats,
            "sparkline": sparkline,
            "as_of": datetime.now(UTC).isoformat(),
        }
    )


@router.get("/_palette.json")
async def palette_json(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Backing data for the Cmd/Ctrl+K command palette.

    Loaded lazily on first open. Templates + tenants are the headline jumps;
    profiles and programs get the same treatment so operators can hop
    straight to a caller-mapping or a program without walking the template.
    Each list is capped so the palette stays a quick-jump, not a browser.
    """
    from docgen.store.models import Tenant

    scope = _forced_tenant_scope(request)
    tmpl_q = (
        select(Template.id, Template.name, Tenant.name.label("tenant_name"))
        .join(Tenant, Tenant.id == Template.tenant_id)
        .where(Template.is_active.is_(True))
        .order_by(Template.created_at.desc())
        .limit(200)
    )
    if scope is not None:
        tmpl_q = tmpl_q.where(Template.tenant_id == scope)
    tmpls = [
        {"id": str(r.id), "name": r.name, "tenant": r.tenant_name}
        for r in (await session.execute(tmpl_q)).all()
    ]
    tenants_stmt = select(Tenant.id, Tenant.name).order_by(Tenant.name.asc()).limit(50)
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenants_stmt = tenants_stmt.where(Tenant.id == ident.tenant_id)
    tenant_rows = (await session.execute(tenants_stmt)).all()
    tenants = [{"id": str(r.id), "name": r.name} for r in tenant_rows]

    # Caller profiles — tenant-scoped mapping wizards. Cap at 200 recent.
    prof_q = (
        select(
            CallerProfile.id,
            CallerProfile.name,
            CallerProfile.caller,
            Template.name.label("template_name"),
        )
        .join(Template, Template.id == CallerProfile.template_id)
        .order_by(CallerProfile.updated_at.desc())
        .limit(200)
    )
    if scope is not None:
        prof_q = prof_q.where(CallerProfile.tenant_id == scope)
    profiles = [
        {
            "id": str(r.id),
            "name": r.name,
            "caller": r.caller,
            "template": r.template_name,
        }
        for r in (await session.execute(prof_q)).all()
    ]

    # Programs — one row per PAP program. Slug is what appears in URLs.
    prog_q = (
        select(Program.id, Program.name, Program.slug, Tenant.name.label("tenant_name"))
        .join(Tenant, Tenant.id == Program.tenant_id)
        .order_by(Program.name.asc())
        .limit(100)
    )
    if scope is not None:
        prog_q = prog_q.where(Program.tenant_id == scope)
    programs = [
        {"id": str(r.id), "name": r.name, "slug": r.slug, "tenant": r.tenant_name}
        for r in (await session.execute(prog_q)).all()
    ]

    return JSONResponse(
        {
            "templates": tmpls,
            "tenants": tenants,
            "profiles": profiles,
            "programs": programs,
        }
    )


_RENDER_STATE_FILTERS = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_RENDER_SINCE_FILTERS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@router.get("/renders", response_class=HTMLResponse)
async def renders(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tenant: str | None = None,
    state: str | None = None,
    since: str | None = None,
    q: str | None = None,
    caller: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
) -> HTMLResponse:
    """Paginated history of render jobs across all templates.

    The dashboard's recent-renders widget only shows the last 10; this page is
    where operators go to triage a specific failure or answer "did the 3pm
    batch run?". Filters: tenant, state, since (24h/7d/30d), caller substring,
    and a free-form `q` that matches job-id prefix / caller / error.
    """
    from docgen.store.models import Tenant

    tenants_stmt = select(Tenant).order_by(Tenant.name.asc())
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenants_stmt = tenants_stmt.where(Tenant.id == ident.tenant_id)
    tenants_res = await session.execute(tenants_stmt)
    all_tenants = tenants_res.scalars().all()

    tenant_uuid = _resolve_list_tenant_filter(request, tenant)

    state_filter = state if state in _RENDER_STATE_FILTERS else None
    since_filter = since if since in _RENDER_SINCE_FILTERS else None
    caller_filter = (caller or "").strip() or None
    q_filter = (q or "").strip() or None
    page_size = clamp_page_size(limit)
    cur = Cursor.decode(cursor)

    query = (
        select(
            RenderJob.id,
            RenderJob.state,
            RenderJob.caller,
            RenderJob.submitted_at,
            RenderJob.completed_at,
            RenderJob.error,
            RenderJob.tenant_id,
            Template.id.label("template_id"),
            Template.name.label("template_name"),
            Tenant.name.label("tenant_name"),
        )
        .join(TemplateVersion, TemplateVersion.id == RenderJob.template_version_id)
        .join(Template, Template.id == TemplateVersion.template_id)
        .join(Tenant, Tenant.id == RenderJob.tenant_id)
        .order_by(RenderJob.submitted_at.desc(), RenderJob.id.desc())
        .limit(page_size + 1)
    )
    if tenant_uuid is not None:
        query = query.where(RenderJob.tenant_id == tenant_uuid)
    if state_filter:
        query = query.where(RenderJob.state == state_filter)
    if since_filter:
        cutoff = datetime.now(UTC) - _RENDER_SINCE_FILTERS[since_filter]
        query = query.where(RenderJob.submitted_at >= cutoff)
    if caller_filter:
        query = query.where(RenderJob.caller.ilike(f"%{caller_filter}%"))
    if q_filter:
        # Search matches: caller substring, error substring, or job-id prefix.
        # ID is a UUID column; cast to text for prefix match so users can paste
        # a short prefix from the URL of a failing job.
        like = f"%{q_filter}%"
        prefix = f"{q_filter}%"
        query = query.where(
            (RenderJob.caller.ilike(like))
            | (RenderJob.error.ilike(like))
            | (func.cast(RenderJob.id, String).ilike(prefix))
        )
    keyset = keyset_where(RenderJob.submitted_at, RenderJob.id, cur)
    if keyset is not None:
        query = query.where(keyset)

    raw_rows = (await session.execute(query)).all()
    items = [dict(r._mapping) for r in raw_rows]
    page = Page.from_rows(
        items,
        page_size,
        cursor_from_row=lambda row: Cursor(ts=row["submitted_at"], id=row["id"]),
    )

    # Preserve the current filter set in the pagination + reset links so users
    # don't lose their view when they click "Next".
    query_parts: list[str] = []
    if state_filter:
        query_parts.append(f"state={state_filter}")
    if since_filter:
        query_parts.append(f"since={since_filter}")
    if tenant_uuid is not None:
        query_parts.append(f"tenant={tenant_uuid}")
    if caller_filter:
        query_parts.append(f"caller={quote_plus(caller_filter)}")
    if q_filter:
        query_parts.append(f"q={quote_plus(q_filter)}")
    filter_qs = "&".join(query_parts)

    return templates.TemplateResponse(
        request,
        "renders.html",
        await _base_ctx(
            request,
            session,
            active="renders",
            all_tenants=all_tenants,
            selected_tenant=tenant_uuid,
            renders=page.items,
            next_cursor=page.next_cursor,
            is_paged=cur is not None,
            page_size=page_size,
            state_filter=state_filter,
            since_filter=since_filter,
            caller_filter=caller_filter,
            q_filter=q_filter,
            filter_qs=filter_qs,
        ),
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from docgen.store.models import Tenant
    stmt = select(Tenant).order_by(Tenant.name.asc())
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        stmt = stmt.where(Tenant.id == ident.tenant_id)
    res = await session.execute(stmt)
    all_tenants = res.scalars().all()
    return templates.TemplateResponse(
        request,
        "upload.html",
        await _base_ctx(request, session, active="upload", all_tenants=all_tenants),
    )


@router.post("/upload", response_model=None)
async def upload_submit(
    request: Request,
    name: str = Form(...),
    owner: str | None = Form(None),
    tenant_id: uuid.UUID | None = Form(None),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    fname = (file.filename or "").lower()
    is_docx = fname.endswith(".docx")
    is_pdf = fname.endswith(".pdf")

    async def _rerender(flash: str, *, status_code: int = 400) -> HTMLResponse:
        # Re-render the upload form in-place so the operator sees the error
        # inline and keeps their name/owner. File inputs can't be preserved
        # (browser security) — they'll have to re-pick the file.
        from docgen.store.models import Tenant
        stmt = select(Tenant).order_by(Tenant.name.asc())
        ident = _identity(request)
        if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
            stmt = stmt.where(Tenant.id == ident.tenant_id)
        res = await session.execute(stmt)
        return templates.TemplateResponse(
            request,
            "upload.html",
            await _base_ctx(
                request,
                session,
                active="upload",
                flash=flash,
                flash_kind="error",
                all_tenants=res.scalars().all(),
                form_name=name,
                form_owner=owner,
                form_tenant_id=str(tenant_id) if tenant_id else None,
            ),
            status_code=status_code,
        )

    if not (is_docx or is_pdf):
        return await _rerender("Upload must be a .docx or .pdf file.")
    file_bytes = await file.read()
    tid = await _get_or_default_tenant_id(session, tenant_id, request=request)
    try:
        await check_active_template_quota(session, tid)
    except QuotaExceeded as e:
        return await _rerender(
            f"Active-template quota exceeded ({e.current}/{e.limit}). "
            "Deactivate or delete an existing template, or ask an admin to raise the cap.",
            status_code=429,
        )
    try:
        if is_pdf:
            template, _version, report = await upload_pdf_template(
                session, name=name, pdf_bytes=file_bytes, owner=(owner or None), tenant_id=tid
            )
        else:
            template, _version, report = await upload_template(
                session, name=name, docx_bytes=file_bytes, owner=(owner or None), tenant_id=tid
            )
    except InvalidDocxError as e:
        return await _rerender(str(e))
    except PdfPreflightRejected as e:
        return await _rerender(_pdf_reject_flash(e))
    kind = {"accept": "success", "accept-with-warnings": "info", "reject": "error"}.get(
        report.outcome, "info"
    )
    msg = f"Uploaded '{template.name}'. Preflight: {report.outcome}."
    if report.findings:
        msg += f" {len(report.findings)} finding(s) — see version detail."
    return _redirect(f"/ui/templates/{template.id}", flash=msg, flash_kind=kind)


def _pdf_reject_flash(exc: PdfPreflightRejected) -> str:
    """Format the preflight finding into an inline flash for the operator.

    Includes the finding message and (if present) the hint — both are what
    the operator needs to fix the problem. Nothing was persisted, so there
    is no version detail page to redirect them to.
    """
    findings = exc.report.findings
    if not findings:
        return "Upload rejected by preflight."
    f = findings[0]
    parts = [f"Upload rejected: {f.message}"]
    if f.hint:
        parts.append(f.hint)
    return " ".join(parts)


_SAMPLE_KINDS = {
    "docx": ("denial_letter.docx", "Sample: Denial Letter (DOCX)"),
    "acroform": ("denial_letter_acroform.pdf", "Sample: Denial Letter (AcroForm PDF)"),
    "flat": ("denial_letter_flat.pdf", "Sample: Denial Letter (flat PDF)"),
    # Explanation of Benefits — richer sample to show off type inference: has a
    # docxtpl {%tr for %} service-line loop (→ list) and an {% if is_denied %}
    # branch (→ boolean) in addition to the scalar/date/currency placeholders.
    "eob": ("insurance_eob.docx", "Sample: Explanation of Benefits (DOCX)"),
}


@router.post("/upload/sample", response_model=None)
async def upload_sample_template(
    request: Request,
    kind: str = Form("docx"),
    tenant_id: uuid.UUID | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """One-click install of a bundled sample template for empty tenants.

    Reads a bundled sample off disk and pushes it through the normal upload
    pipeline. `kind` picks which of the three flavors — docx / acroform / flat —
    the operator wants so they can compare deterministic render paths without
    hunting for a template first.
    """
    from pathlib import Path

    if kind not in _SAMPLE_KINDS:
        return _redirect(
            "/ui/", flash=f"Unknown sample kind: {kind}", flash_kind="error",
        )
    filename, display_name = _SAMPLE_KINDS[kind]
    sample_path = Path(__file__).resolve().parents[3] / "sample_templates" / filename
    if not sample_path.exists():
        return _redirect(
            "/ui/", flash="Sample template not bundled in this build.",
            flash_kind="error",
        )
    file_bytes = sample_path.read_bytes()
    tid = await _get_or_default_tenant_id(session, tenant_id, request=request)
    actor = getattr(request.state, "admin_user", None)
    owner_name = getattr(actor, "username", None) if actor else None
    try:
        if kind == "docx":
            template, _version, report = await upload_template(
                session, name=display_name, docx_bytes=file_bytes,
                owner=owner_name, tenant_id=tid,
            )
        else:
            template, _version, report = await upload_pdf_template(
                session, name=display_name, pdf_bytes=file_bytes,
                owner=owner_name, tenant_id=tid,
            )
    except InvalidDocxError as e:
        return _redirect("/ui/", flash=str(e), flash_kind="error")
    except PdfPreflightRejected as e:
        # Bundled sample PDFs must always pass preflight — reject here is a
        # build-time bug, not user error. Surface the same flash for
        # visibility until the sample is fixed.
        return _redirect("/ui/", flash=_pdf_reject_flash(e), flash_kind="error")
    return _redirect(
        f"/ui/templates/{template.id}",
        flash=f"Bundled sample installed. Preflight: {report.outcome}. "
              "Freeze the version to render.",
        flash_kind="success",
    )


@router.post("/templates/{template_id}/versions", response_model=None)
async def upload_new_version(
    template_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Add a new draft version to an existing template.

    Used to fix mapping/type mistakes: frozen versions are immutable (§9), so schema
    changes require a fresh draft the user re-maps and re-freezes.
    """
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    fname = (file.filename or "").lower()
    # A template's kind is fixed at first upload — new versions must match.
    expected_ext = ".pdf" if template.kind == "pdf" else ".docx"
    if not fname.endswith(expected_ext):
        return _redirect(
            f"/ui/templates/{template_id}",
            flash=f"New versions of this template must be {expected_ext} files.",
            flash_kind="error",
        )
    file_bytes = await file.read()
    try:
        if template.kind == "pdf":
            _template, tv, report = await upload_pdf_template(
                session,
                name=template.name,
                pdf_bytes=file_bytes,
                owner=template.owner,
                tenant_id=template.tenant_id,
                template_id=template.id,
            )
        else:
            _template, tv, report = await upload_template(
                session,
                name=template.name,
                docx_bytes=file_bytes,
                owner=template.owner,
                tenant_id=template.tenant_id,
                template_id=template.id,
            )
    except InvalidDocxError as e:
        return _redirect(
            f"/ui/templates/{template_id}", flash=str(e), flash_kind="error",
        )
    except PdfPreflightRejected as e:
        # New version rejected — the existing template row and its current
        # frozen version(s) stay intact. Redirect back to the template detail
        # so the operator can retry with a fixed PDF.
        return _redirect(
            f"/ui/templates/{template_id}",
            flash=_pdf_reject_flash(e),
            flash_kind="error",
        )
    kind = {"accept": "success", "accept-with-warnings": "info", "reject": "error"}.get(
        report.outcome, "info"
    )
    msg = f"Uploaded v{tv.version}. Preflight: {report.outcome}."
    if report.findings:
        msg += f" {len(report.findings)} finding(s)."
    return _redirect(f"/ui/templates/{template_id}", flash=msg, flash_kind=kind)


@router.post("/templates/{template_id}/versions/{version_number}/locales", response_model=None)
async def upload_new_locale(
    template_id: uuid.UUID,
    version_number: int,
    request: Request,
    locale: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Attach a localized draft variant to an existing version_number.

    Freeze-time invariant then guarantees the localized variant advertises the
    same schema fields as its siblings (§14).
    """
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    locale = (locale or "").strip()
    if not locale or locale == "default":
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="Locale tag required (e.g. en-GB, es-419).", flash_kind="error",
        )
    if not file.filename or not file.filename.lower().endswith(".docx"):
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="Upload must be a .docx file.", flash_kind="error",
        )
    docx_bytes = await file.read()
    try:
        _tv, report = await upload_locale_variant(
            session,
            template_id=template_id,
            tenant_id=template.tenant_id,
            version_number=version_number,
            locale=locale,
            docx_bytes=docx_bytes,
        )
    except ValueError as e:
        return _redirect(
            f"/ui/templates/{template_id}", flash=str(e), flash_kind="error",
        )
    kind = {"accept": "success", "accept-with-warnings": "info", "reject": "error"}.get(
        report.outcome, "info"
    )
    msg = f"Uploaded {locale} variant of v{version_number}. Preflight: {report.outcome}."
    if report.findings:
        msg += f" {len(report.findings)} finding(s)."
    return _redirect(f"/ui/templates/{template_id}", flash=msg, flash_kind=kind)


@router.post("/templates/{template_id}/publish", response_model=None)
async def publish_action(
    template_id: uuid.UUID,
    request: Request,
    version: int = Form(...),
    subversion: int | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    actor = getattr(request.state, "admin_user", None)
    try:
        _template, event = await publish_version(
            session,
            template_id=template_id,
            tenant_id=template.tenant_id,
            version=version,
            subversion=subversion,
            actor=actor,
        )
    except ValueError as e:
        return _redirect(f"/ui/templates/{template_id}", flash=str(e), flash_kind="error")
    verb = "Rolled back to" if event.event_kind == "rollback" else "Published"
    return _redirect(
        f"/ui/templates/{template_id}",
        flash=f"{verb} v{event.version}.{event.subversion}"
              + (
                  f" (was v{event.previous_version}.{event.previous_subversion or 0})"
                  if event.previous_version else ""
              )
              + ".",
        flash_kind="success",
    )


@router.post("/templates/{template_id}/activation", response_model=None)
async def toggle_activation(
    template_id: uuid.UUID,
    request: Request,
    active: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Soft-delete toggle. `active=false` hides the template from all listings
    and render paths; `active=true` restores it. Versions and blobs are kept
    intact so reactivation is lossless.
    """
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    want_active = active.lower() == "true"
    if template.is_active == want_active:
        return _redirect(f"/ui/templates/{template_id}")
    template.is_active = want_active
    await session.commit()
    verb = "reactivated" if want_active else "inactivated"
    return _redirect(
        f"/ui/templates/{template_id}",
        flash=f"Template {verb}.",
        flash_kind="success",
    )


@router.post("/templates/bulk", response_model=None)
async def bulk_templates(
    request: Request,
    session: AsyncSession = Depends(get_session),
    action: str = Form(...),
    ids: list[str] = Form(default=[]),
) -> RedirectResponse:
    """Apply an action to every checked template on the listing page.

    Currently supported: `activate` / `deactivate`. Silently skips ids that
    don't parse or don't exist so a stale checkbox doesn't tank the whole
    batch.
    """
    if action not in {"activate", "deactivate"}:
        return _redirect("/ui/", flash="Unknown bulk action.", flash_kind="error")
    want_active = action == "activate"
    parsed: list[uuid.UUID] = []
    for raw in ids:
        try:
            parsed.append(uuid.UUID(raw))
        except (ValueError, AttributeError):
            continue
    if not parsed:
        return _redirect("/ui/", flash="No templates selected.", flash_kind="info")
    query = select(Template).where(Template.id.in_(parsed))
    scope = _forced_tenant_scope(request)
    if scope is not None:
        query = query.where(Template.tenant_id == scope)
    result = await session.execute(query)
    templates_to_update = list(result.scalars().all())
    changed = 0
    for tpl in templates_to_update:
        if tpl.is_active != want_active:
            tpl.is_active = want_active
            changed += 1
    if changed:
        await session.commit()
    verb = "reactivated" if want_active else "inactivated"
    msg = (
        f"{changed} template{'s' if changed != 1 else ''} {verb}."
        if changed
        else f"No templates needed to be {verb}."
    )
    return _redirect("/ui/", flash=msg, flash_kind="success" if changed else "info")


@router.get("/templates/{template_id}", response_class=HTMLResponse)
async def template_detail(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    versions_result = await session.execute(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == template_id)
        .order_by(
            TemplateVersion.version.desc(),
            TemplateVersion.subversion.desc(),
            TemplateVersion.locale,
        )
    )
    versions = versions_result.scalars().all()
    # "Latest" = default locale, highest (version, subversion). The listing is
    # already sorted that way so the first default-locale hit wins.
    latest = next(
        (v for v in versions if v.locale == "default"), versions[0] if versions else None
    )
    # Per-template, the tip (version, subversion) tuple governs which rows the
    # editor treats as editable. Compute once here and pass to the template so
    # the "Edit →" link only lights up on the tip row.
    tip_tuple = None
    for v in versions:
        if v.locale != "default" or not v.is_frozen:
            continue
        key = (v.version, v.subversion)
        if tip_tuple is None or key > tip_tuple:
            tip_tuple = key
    publish_history = await list_publish_events(
        session, template_id=template_id, tenant_id=template.tenant_id
    )
    audit_result = await session.execute(
        select(TemplateAuditEvent)
        .where(TemplateAuditEvent.template_id == template_id)
        .order_by(TemplateAuditEvent.created_at.desc())
        .limit(50)
    )
    audit_history = list(audit_result.scalars().all())
    callers_cutoff = datetime.now(UTC) - timedelta(days=30)
    callers_result = await session.execute(
        select(
            RenderJob.caller,
            func.count(RenderJob.id).label("total"),
            func.sum(case((RenderJob.state == "failed", 1), else_=0)).label("failed"),
            func.max(RenderJob.submitted_at).label("last_seen"),
        )
        .join(TemplateVersion, TemplateVersion.id == RenderJob.template_version_id)
        .where(
            TemplateVersion.template_id == template_id,
            RenderJob.submitted_at >= callers_cutoff,
        )
        .group_by(RenderJob.caller)
        .order_by(func.count(RenderJob.id).desc())
        .limit(20)
    )
    callers_30d = [
        {
            "caller": row.caller,
            "total": row.total,
            "failed": int(row.failed or 0),
            "last_seen": row.last_seen,
        }
        for row in callers_result.all()
    ]
    return templates.TemplateResponse(
        request,
        "template_detail.html",
        await _base_ctx(
            request,
            session,
            template=template,
            versions=versions,
            publish_history=publish_history,
            audit_history=audit_history,
            callers_30d=callers_30d,
            current_version=template.current_version,
            current_subversion=template.current_subversion,
            tip_version=tip_tuple[0] if tip_tuple else None,
            tip_subversion=tip_tuple[1] if tip_tuple else None,
            latest=latest,
            flash=flash,
            flash_kind=flash_kind,
        ),
    )


@router.get("/versions/{version_id}/map", response_class=HTMLResponse)
async def map_form(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.is_frozen:
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash=f"v{tv.version} is already frozen.",
            flash_kind="info",
        )
    # PDF templates skip the DOCX Jinja-tag → field mapping step: the placement
    # name IS the payload key. Freeze runs straight off placements.
    if tv.pdf_flavor is not None:
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash="PDF templates don't need field mapping — placement names are the payload keys.",
            flash_kind="info",
        )
    template = await session.get(Template, tv.template_id)
    try:
        proposal = await propose_mapping_for_version(session, version_id)
    except ValueError as e:
        return _redirect(
            f"/ui/templates/{tv.template_id}", flash=f"Mapping failed: {e}", flash_kind="error"
        )
    return templates.TemplateResponse(
        request,
        "mapping_review.html",
        await _base_ctx(request, session, template=template, version=tv, proposal=proposal),
    )


async def _placements_locked(
    session: AsyncSession, tv: TemplateVersion
) -> tuple[bool, Template | None]:
    """Return (locked, template).

    A version is locked once a newer (version, subversion) tuple exists for
    the same template + locale. History rows stay immutable; only the tip
    sub of the tip version is editable. Frozen tip stays editable — saves
    detect real changes and clone into a fresh sub-version.
    """
    template = await session.get(Template, tv.template_id)
    from sqlalchemy import tuple_

    result = await session.execute(
        select(TemplateVersion.id)
        .where(
            TemplateVersion.template_id == tv.template_id,
            TemplateVersion.locale == tv.locale,
            tuple_(TemplateVersion.version, TemplateVersion.subversion)
            > tuple_(tv.version, tv.subversion),
        )
        .limit(1)
    )
    has_newer = result.scalar_one_or_none() is not None
    return (has_newer, template)


@router.get(
    "/versions/{version_id}/placements",
    response_class=HTMLResponse,
    response_model=None,
)
async def placement_editor(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    """The canvas-over-page-image editor for PDF placements."""
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor is None:
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash="Placement editor is for PDF templates only.",
            flash_kind="error",
        )
    locked, _template = await _placements_locked(session, tv)
    if locked:
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash=f"v{tv.version} is published — placements are immutable.",
            flash_kind="info",
        )

    # Page count lives inside the PDF; read it lazily so we don't pay the
    # cost when other routes fetch the version row.
    import pypdfium2  # type: ignore[import-untyped]

    settings = get_settings()
    pdf_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)
    doc = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page_count = len(doc)
    finally:
        doc.close()

    template = await session.get(Template, tv.template_id)
    placements = (tv.pdf_field_placements or {}).get("placements", [])

    # One-shot legacy migration: older uploads stored pages as 0-based and
    # percentages as 0-100. If we see that shape, rewrite in place so the
    # editor and render pipeline (both now on 1-based + 0-1 fractions) get
    # sensible values. Detection is per-list, not per-item, so multi-page
    # docs with page:1 meaning "second page" all shift together.
    migrated = _normalize_legacy_placements(placements)
    if migrated is not None:
        tv.pdf_field_placements = {"placements": migrated}
        await session.commit()
        placements = migrated
        log.info(
            "pdf_placements_migrated_legacy",
            version_id=str(tv.id),
            template_id=str(tv.template_id),
            count=len(migrated),
        )

    realign_summary = (tv.pdf_field_placements or {}).get("realign")
    return templates.TemplateResponse(
        request,
        "placement_editor.html",
        await _base_ctx(
            request,
            session,
            template=template,
            version=tv,
            page_count=page_count,
            placements_json=json.dumps(placements),
            realign_json=json.dumps(realign_summary),
        ),
    )


def _normalize_legacy_placements(
    placements: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Rewrite pre-units-fix placements to the current convention.

    Old harvester stored 0-based pages AND 0-100 percentages. The two
    conventions were changed together, so if EITHER signal is present the
    whole list is treated as legacy and both dimensions are corrected.

    A list can look "legacy" on only one axis: e.g. all widgets on the 4th
    page means pages are 3, 3, 3 (no page < 1) but percentages are >1.5.
    Detecting on percentages alone would leave those pages one short —
    hence the OR.

    Returns None when the list already looks correct.
    """
    if not placements:
        return None
    pages_look_zero_based = any(
        isinstance(p.get("page"), int) and p["page"] < 1 for p in placements
    )
    pct_keys = ("x_pct", "y_pct", "w_pct", "h_pct")
    pcts_look_percentage = any(
        isinstance(p.get(k), (int, float)) and p[k] > 1.5
        for p in placements
        for k in pct_keys
    )
    if not pages_look_zero_based and not pcts_look_percentage:
        return None
    fixed: list[dict[str, Any]] = []
    for p in placements:
        q = dict(p)
        if isinstance(q.get("page"), int):
            q["page"] = q["page"] + 1
        for k in pct_keys:
            v = q.get(k)
            if isinstance(v, (int, float)) and v > 1.5:
                q[k] = round(v / 100.0, 5)
        fixed.append(q)
    return fixed


@router.post("/versions/{version_id}/placements")
async def placements_save(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Persist the placement list from the editor.

    Uses POST (not PATCH) because the CSRF middleware / form-encoded flow
    is already there for POST. The body is JSON; each placement is
    validated for shape before write. Whole-list replace (not per-field
    patch) — the editor always sends the full canonical list.

    Draft (unfrozen) rows are edited in place. Frozen tip rows clone into a
    new sub-version when the cleaned placement list actually differs — this
    keeps v1.0 immutable while letting the user iterate as v1.1, v1.2, …
    History rows (anything with a newer sub already) are rejected.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor is None:
        raise HTTPException(400, "Not a PDF template")
    locked, _template = await _placements_locked(session, tv)
    if locked:
        raise HTTPException(
            409, "This version is superseded — edit the latest sub-version instead."
        )

    body = await request.json()
    incoming = body.get("placements")
    if not isinstance(incoming, list):
        raise HTTPException(400, "body.placements must be a list")

    cleaned: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, p in enumerate(incoming):
        if not isinstance(p, dict):
            raise HTTPException(400, f"placements[{i}] must be an object")
        name = str(p.get("name", "")).strip()
        if not name:
            raise HTTPException(400, f"placements[{i}].name is required")
        if name in seen_names:
            raise HTTPException(400, f"placements[{i}].name '{name}' is duplicated")
        seen_names.add(name)
        try:
            page = int(p.get("page", 1))
            x_pct = float(p.get("x_pct", 0))
            y_pct = float(p.get("y_pct", 0))
            w_pct = float(p.get("w_pct", 0.15))
            font_size = float(p.get("font_size", 10))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"placements[{i}]: {e}") from e
        if page < 1:
            raise HTTPException(400, f"placements[{i}].page must be >= 1")
        # Percentages must fit on the page (allow small overshoot for text
        # that flows past the field — value overlays don't wrap).
        for k, v in (("x_pct", x_pct), ("y_pct", y_pct), ("w_pct", w_pct)):
            if not (0 <= v <= 1.5):
                raise HTTPException(400, f"placements[{i}].{k} out of range: {v}")
        label_raw = p.get("label")
        label = str(label_raw).strip() if isinstance(label_raw, str) else None
        # Preserve h_pct and field_type if they came in — the harvester sets
        # them on acroform templates and the render pipeline uses them (h_pct
        # for baseline math, field_type to skip padding on checkboxes).
        h_pct_raw = p.get("h_pct")
        h_pct = float(h_pct_raw) if isinstance(h_pct_raw, (int, float)) else None
        ft_raw = p.get("field_type")
        # "radio" was omitted from this whitelist before radio detection existed;
        # detector-emitted radios were being nulled out on save so the placement
        # editor lost the circular render and re-classified them as text.
        field_type = ft_raw if ft_raw in ("text", "checkbox", "choice", "radio") else None
        # `type` is the render-pipeline discriminator ("text" | "checkbox" |
        # "radio"). Distinct from `field_type` (source-widget signal, present
        # only on acroform-harvested placements) and `payload_type` (schema
        # type). Radio has no legacy fallback signal so we must preserve it
        # through save — otherwise the tool click sets it, the save strips
        # it, and the placement renders as text on the next load.
        type_raw = p.get("type")
        placement_type = type_raw if type_raw in ("text", "checkbox", "radio") else None
        # payload_type is the SCHEMA type ("string" or "boolean") — distinct
        # from field_type which describes the widget's render geometry
        # (text/checkbox/choice). Defaulted from placement_type/field_type so a
        # fresh checkbox or radio placement validates against bool payloads
        # without the user having to touch the dropdown.
        pt_raw = p.get("payload_type")
        if pt_raw in ("string", "boolean"):
            payload_type = pt_raw
        elif placement_type in ("checkbox", "radio") or field_type in ("checkbox", "radio"):
            payload_type = "boolean"
        else:
            payload_type = "string"
        # Realign metadata — only present on placements re-aligned from a
        # prior version (see designtime/realign_pdf.py). Passed through
        # verbatim so a save doesn't strip the "needs review" state before
        # the operator has actually reviewed it. prior_coords is the
        # geometry snapshot the editor renders as a ghost outline; it gets
        # cleared when the operator accepts (or is untouched when they
        # revert, though revert also flips status to confirmed).
        realign_source = p.get("realign_source")
        realign_status = p.get("realign_status")
        realign_confidence = p.get("realign_confidence")
        realign_source = realign_source if isinstance(realign_source, str) else None
        if realign_status not in ("auto", "needs_review", "confirmed", "rejected"):
            realign_status = None
        realign_confidence = (
            float(realign_confidence)
            if isinstance(realign_confidence, (int, float))
            else None
        )
        prior_coords_raw = p.get("prior_coords")
        prior_coords: dict[str, Any] | None = None
        if isinstance(prior_coords_raw, dict):
            try:
                prior_coords = {
                    "page": int(prior_coords_raw.get("page") or 1),
                    "x_pct": float(prior_coords_raw.get("x_pct") or 0),
                    "y_pct": float(prior_coords_raw.get("y_pct") or 0),
                    "w_pct": float(prior_coords_raw.get("w_pct") or 0),
                    "h_pct": float(prior_coords_raw.get("h_pct") or 0),
                }
            except (TypeError, ValueError):
                prior_coords = None
        cleaned.append({
            "name": name,
            "label": label or None,
            "type": placement_type,
            "field_type": field_type,
            "payload_type": payload_type,
            "page": page,
            "x_pct": x_pct,
            "y_pct": y_pct,
            "w_pct": w_pct,
            "h_pct": h_pct,
            "font_size": font_size,
            "anchor": str(p.get("anchor", "top-left")),
            "locked": bool(p.get("locked", False)),
            "realign_source": realign_source,
            "realign_status": realign_status,
            "realign_confidence": realign_confidence,
            "prior_coords": prior_coords,
        })

    actor = getattr(request.state, "admin_user", None)

    if tv.is_frozen:
        # Diff cleaned vs stored: if the user opened the editor but didn't
        # actually change anything, we don't spawn a redundant v1.1.
        existing = (tv.pdf_field_placements or {}).get("placements", [])
        if _placements_equal(existing, cleaned):
            return {"placements": cleaned, "redirect": None, "changed": False}
        try:
            new_tv = await create_pdf_subversion(
                session,
                parent_version_id=tv.id,
                placements=cleaned,
                actor=actor,
            )
        except ValueError as e:
            raise HTTPException(400, f"Sub-version create failed: {e}") from e
        log.info(
            "pdf_placements_saved_as_subversion",
            parent_version_id=str(tv.id),
            new_version_id=str(new_tv.id),
            template_id=str(tv.template_id),
            version=new_tv.version,
            subversion=new_tv.subversion,
            count=len(cleaned),
        )
        return {
            "placements": cleaned,
            "changed": True,
            "redirect": f"/ui/versions/{new_tv.id}/placements",
            "new_version_id": str(new_tv.id),
            "version": new_tv.version,
            "subversion": new_tv.subversion,
        }

    tv.pdf_field_placements = {"placements": cleaned}
    # Reflect the placement list in the draft schema so the mapping/schema
    # viewer stays consistent pre-freeze. Freeze re-derives from placements
    # so this is a convenience-only shadow.
    tv.schema = {"fields": [{"name": p["name"], "type": p["payload_type"]} for p in cleaned]}
    await record_audit_event(
        session,
        template_id=tv.template_id,
        version_id=tv.id,
        actor=actor,
        event_kind="placements_edited",
        summary={"field_count": len(cleaned)},
    )
    await session.commit()
    await session.refresh(tv)
    log.info(
        "pdf_placements_saved",
        version_id=str(tv.id),
        template_id=str(tv.template_id),
        count=len(cleaned),
    )
    return {"placements": cleaned, "changed": True, "redirect": None}


# Fields compared for the "did anything actually change" diff. Same set the
# render pipeline consumes — cosmetic-only fields (e.g. UI-derived _nameManual)
# are stripped by the cleaner above, so this list is exhaustive.
_PLACEMENT_DIFF_KEYS = (
    "name", "label", "type", "field_type", "payload_type",
    "page", "x_pct", "y_pct", "w_pct", "h_pct",
    "font_size", "anchor", "locked",
)


def _placements_equal(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """True when two placement lists would produce identical frozen output."""
    if len(a) != len(b):
        return False
    for pa, pb in zip(a, b, strict=False):
        for k in _PLACEMENT_DIFF_KEYS:
            if pa.get(k) != pb.get(k):
                return False
    return True


from docgen.designtime.pdf_labeler_backend import (  # noqa: E402  (lazy: after local helpers)
    LABELER_UNCONFIGURED as _PDF_LABELER_UNCONFIGURED,
)
from docgen.designtime.pdf_labeler_backend import (  # noqa: E402
    pick_pdf_labeler_backend as _pick_pdf_labeler_backend,
)


@router.post("/versions/{version_id}/placements/ai-draw")
async def placements_ai_draw(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Detect fillable field boxes on a flat PDF via deterministic geometry.

    Only valid for `pdf_flavor == "flat"` — acroform templates already have
    widget-derived placements. Returns a list of placement proposals with
    generic names (`field_1`, `field_2`, …); the client applies them, the
    user reviews positions, then runs the existing "AI Label fields" button
    to fill in real names/labels from page captions.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor != "flat":
        raise HTTPException(400, "AI draw is only available for flat PDFs")
    locked, _template = await _placements_locked(session, tv)
    if locked:
        raise HTTPException(409, "Version is published — placements are immutable")

    settings = get_settings()

    from docgen.designtime.pdf_field_detector import detect_fields_in_pdf

    pdf_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)

    try:
        boxes = await detect_fields_in_pdf(pdf_bytes)
    except Exception as e:
        log.warning(
            "pdf_ai_draw_failed",
            version_id=str(tv.id),
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(502, f"AI field detection failed: {e}") from e

    placements: list[dict[str, Any]] = []
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
            "payload_type": "boolean" if ftype in ("checkbox", "radio") else "string",
        })

    log.info(
        "pdf_placements_ai_drawn",
        version_id=str(tv.id),
        template_id=str(tv.template_id),
        detected_count=len(placements),
    )
    return {"placements": placements, "detected_count": len(placements)}


@router.post("/versions/{version_id}/placements/ai-detect")
async def placements_ai_detect(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Label existing placements via text-only Nemotron.

    Positions come from the PDF widgets or a prior AI-draw pass (accurate);
    the labeler extracts per-line OCR text with pdfplumber and asks Nemotron
    to match each field rect to its caption based on coordinates + text.
    No image is sent to the model. The response is a proposal — the client
    applies suggested labels to matching placement indices and the user
    reviews before saving.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor is None:
        raise HTTPException(400, "Not a PDF template")
    locked, _template = await _placements_locked(session, tv)
    if locked:
        raise HTTPException(409, "Version is published — placements are immutable")

    settings = get_settings()
    backend = _pick_pdf_labeler_backend(settings)
    if backend is None:
        raise HTTPException(400, _PDF_LABELER_UNCONFIGURED)

    body = await request.json()
    incoming = body.get("placements")
    if not isinstance(incoming, list):
        raise HTTPException(400, "body.placements must be a list")

    fields: list[dict[str, Any]] = []
    for i, p in enumerate(incoming):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        fields.append({
            "index": i,
            "name": name,
            "page": int(p.get("page") or 1),
            "x_pct": float(p.get("x_pct") or 0),
            "y_pct": float(p.get("y_pct") or 0),
            "w_pct": float(p.get("w_pct") or 0),
            "h_pct": float(p.get("h_pct") or 0),
            "field_type": p.get("field_type"),
        })

    from docgen.designtime.pdf_field_detector import (
        label_fields_in_pdf,
        pdf_has_extractable_text,
    )

    pdf_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)

    try:
        labels_by_index = await label_fields_in_pdf(
            pdf_bytes,
            fields,
            api_key=backend["api_key"],
            text_model=backend["text_model"],
            base_url=backend["base_url"],
            timeout=backend["timeout"],
            pdf_flavor=tv.pdf_flavor,
            extra_body=backend["extra_body"],
            vision_model=backend.get("vision_model", ""),
            labeler_mode=backend.get("labeler_mode", "auto"),
        )
    except Exception as e:
        log.warning(
            "pdf_ai_label_failed",
            version_id=str(tv.id),
            provider=backend["provider"],
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(502, f"AI labeling failed: {e}") from e

    suggestions: list[dict[str, Any]] = [
        {
            "index": idx,
            "suggested_label": item.get("label", ""),
            "suggested_section": item.get("section", ""),
        }
        for idx, item in labels_by_index.items()
    ]

    labeled_count = len(labels_by_index)
    total_count = len(fields)
    # If nothing was labeled AND the PDF has no extractable text (vector-glyph
    # form), we fell back to visual labeling via a VLM but even that returned
    # nothing usable. Surface this so the UI can explain it instead of
    # showing a bare "0 of N" result. On PDFs WITH extractable text a 0 result
    # is a genuine model failure (retry-worthy) — don't show the message.
    no_extractable_text = (
        labeled_count == 0
        and total_count > 0
        and not pdf_has_extractable_text(pdf_bytes)
    )
    log.info(
        "pdf_placements_ai_labeled",
        version_id=str(tv.id),
        template_id=str(tv.template_id),
        labeled_count=labeled_count,
        total_count=total_count,
        no_extractable_text=no_extractable_text,
    )
    return {
        "suggestions": suggestions,
        "labeled_names": labeled_count,
        "total_names": total_count,
        "no_extractable_text": no_extractable_text,
    }


@router.post("/versions/{version_id}/placements/ai-realign")
async def placements_ai_realign(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Phase-C AI fallback: match needs-review placements to freshly-detected
    fields on the re-uploaded PDF by comparing their labels.

    The heuristic realigner (upload path) hits its limits on flat re-uploads
    or heavy layout drift — everything falls through to preserve_coords
    with confidence 0.5. This endpoint runs `detect_fields_in_pdf` +
    `label_fields_in_pdf` on the current version's stored PDF, then hands
    the labeled detections to `match_placements_by_label`, which pairs
    them 1:1 to the incoming placements by normalized label similarity.

    Body: `{"placements": [...]}` — the editor's current placement list.
    Returns `{"matches": [{index, page, x_pct, y_pct, w_pct, h_pct,
    matched_label, similarity}, ...], "detected_count": N, "labeled_count": M}`.
    The client applies each match to its placement (updating geometry,
    stamping realign_source="ai_label_match" and status="auto") and shows
    a toast with the counts.

    Guarded on the same conditions as the label endpoint above: draft only,
    vision backend required. Reuses the same OCR + vision configuration."""
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor is None:
        raise HTTPException(400, "Not a PDF template")
    locked, _template = await _placements_locked(session, tv)
    if locked:
        raise HTTPException(409, "Version is published — placements are immutable")

    settings = get_settings()
    backend = _pick_pdf_labeler_backend(settings)
    if backend is None:
        raise HTTPException(400, _PDF_LABELER_UNCONFIGURED)

    body = await request.json()
    incoming = body.get("placements")
    if not isinstance(incoming, list):
        raise HTTPException(400, "body.placements must be a list")

    from docgen.designtime.pdf_field_detector import (
        detect_fields_in_pdf,
        label_fields_in_pdf,
    )
    from docgen.designtime.realign_pdf import match_placements_by_label

    pdf_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)

    try:
        detected = await detect_fields_in_pdf(pdf_bytes)
    except Exception as e:
        log.warning(
            "pdf_ai_realign_detect_failed",
            version_id=str(tv.id),
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(502, f"AI field detection failed: {e}") from e

    # Label the detected fields so we have something to match against. The
    # labeler wants an `index` per field; assign monotonically so the
    # returned dict keys align with our `detected` list.
    label_input: list[dict[str, Any]] = []
    for i, d in enumerate(detected):
        label_input.append({"index": i, **d})

    try:
        labels_by_index = await label_fields_in_pdf(
            pdf_bytes,
            label_input,
            api_key=backend["api_key"],
            text_model=backend["text_model"],
            base_url=backend["base_url"],
            timeout=backend["timeout"],
            extra_body=backend["extra_body"],
            vision_model=backend.get("vision_model", ""),
            labeler_mode=backend.get("labeler_mode", "auto"),
        )
    except Exception as e:
        log.warning(
            "pdf_ai_realign_label_failed",
            version_id=str(tv.id),
            provider=backend["provider"],
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(502, f"AI field labeling failed: {e}") from e

    labeled_detected: list[dict[str, Any]] = []
    for i, d in enumerate(detected):
        label_entry = labels_by_index.get(i) or {}
        label = str(label_entry.get("label") or "").strip()
        if not label:
            continue
        labeled_detected.append({**d, "label": label})

    matches = match_placements_by_label(
        placements=incoming, detected_fields=labeled_detected,
    )

    log.info(
        "pdf_placements_ai_realigned",
        version_id=str(tv.id),
        template_id=str(tv.template_id),
        detected_count=len(detected),
        labeled_count=len(labeled_detected),
        match_count=len(matches),
    )
    return {
        "matches": [
            {
                "index": m.index,
                "page": m.page,
                "x_pct": m.x_pct,
                "y_pct": m.y_pct,
                "w_pct": m.w_pct,
                "h_pct": m.h_pct,
                "matched_label": m.matched_label,
                "similarity": m.similarity,
            }
            for m in matches
        ],
        "detected_count": len(detected),
        "labeled_count": len(labeled_detected),
    }


@router.get("/versions/{version_id}/pdf-page/{page_num}.png")
async def pdf_page_preview(
    version_id: uuid.UUID,
    page_num: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Render one PDF page to PNG, cached to blob storage per version.

    The placement editor loads one of these per page as the canvas backing
    image. Renders are stable per (template_id, version) — a new upload
    gets a fresh version number and its own cache namespace, so we never
    need explicit invalidation.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor is None:
        raise HTTPException(400, "Not a PDF template")
    if page_num < 1:
        raise HTTPException(400, "page_num is 1-indexed")

    settings = get_settings()
    cache_key = f"previews/{tv.template_id}/v{tv.version}/page_{page_num}.png"
    try:
        png = get_object(settings.s3_bucket_documents, cache_key)
    except Exception:
        # Cache miss: raster the page. pypdfium2 (Apache-2.0, wraps Google's
        # PDFium) is imported lazily so the UI module doesn't pay the import
        # cost at process start.
        import pypdfium2  # type: ignore[import-untyped]

        pdf_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)
        doc = pypdfium2.PdfDocument(pdf_bytes)
        try:
            page_count = len(doc)
            if page_num > page_count:
                raise HTTPException(
                    404, f"page_num {page_num} > page_count {page_count}"
                ) from None
            page = doc[page_num - 1]
            # 150 DPI = 2.083x the 72-DPI PDF user space. Enough for
            # legible text in the editor without blowing up payload size.
            pil_image = page.render(scale=2.083).to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            png = buf.getvalue()
        finally:
            doc.close()
        put_object(settings.s3_bucket_documents, cache_key, png, content_type="image/png")

    return StreamingResponse(
        io.BytesIO(png),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/versions/{version_id}/pdf-preview.pdf")
async def pdf_preview_merged(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Merge the draft placements with a placeholder payload so the user can
    visually verify positions before freezing.

    Each placement's value is its own name in square brackets — a stand-in
    that makes it obvious which box each placement corresponds to and lets
    the user spot mis-positioned or overlapping fields at a glance.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)
    if tv.pdf_flavor is None:
        raise HTTPException(400, "Not a PDF template")

    placements = (tv.pdf_field_placements or {}).get("placements") or []
    if not placements:
        raise HTTPException(
            422,
            "No placements yet — add fields in the editor before previewing.",
        )

    from docgen.core.render_pipeline_pdf import PdfRenderError, render_pdf_template

    settings = get_settings()
    pdf_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)
    sample = {p["name"]: f"[{p['name']}]" for p in placements}
    try:
        output = render_pdf_template(pdf_bytes, placements, sample)
    except PdfRenderError as e:
        raise HTTPException(422, str(e)) from e
    return StreamingResponse(
        io.BytesIO(output),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="preview-v{tv.version}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/versions/{version_id}/freeze")
async def freeze_submit(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    await _enforce_version_scope(request, session, tv)

    # PDF freeze takes no form input — placements are the source of truth,
    # already persisted from the editor. Skip the mapping-form parse.
    if tv.pdf_flavor is not None:
        try:
            frozen = await freeze_pdf_version(session, version_id=version_id)
        except ValueError as e:
            return _redirect(
                f"/ui/templates/{tv.template_id}",
                flash=f"Freeze rejected: {e}",
                flash_kind="error",
            )
        return _redirect(
            f"/ui/templates/{frozen.template_id}",
            flash=f"Froze v{frozen.version} (hash {frozen.version_hash[:12]}…).",
            flash_kind="success",
        )

    form = await request.form()
    row_count = int(form.get("row_count", "0"))
    suggestions: list[Suggestion] = []
    for i in range(row_count):
        placeholder = str(form.get(f"placeholder_{i}", "")).strip()
        jinja = str(form.get(f"jinja_{i}", "")).strip()
        field_name = str(form.get(f"field_name_{i}", "")).strip()
        if not (placeholder and jinja and field_name):
            continue
        field = FieldSpec(
            name=field_name,
            type=str(form.get(f"field_type_{i}", "string")),
            description=str(form.get(f"field_desc_{i}", "")) or None,
            required=bool(form.get(f"field_required_{i}")),
        )
        suggestions.append(Suggestion(placeholder=placeholder, jinja=jinja, field=field))
    notes = str(form.get("notes", "")) or None

    try:
        frozen = await freeze_version(
            session,
            version_id=version_id,
            proposal=MappingProposal(suggestions=suggestions, notes=notes),
        )
    except ValueError as e:
        return _redirect(
            f"/ui/templates/{tv.template_id}", flash=f"Freeze rejected: {e}", flash_kind="error"
        )
    return _redirect(
        f"/ui/templates/{frozen.template_id}",
        flash=f"Froze v{frozen.version} (hash {frozen.version_hash[:12]}…).",
        flash_kind="success",
    )


@router.post("/versions/{version_id}/supersede", response_model=None)
async def supersede_submit(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Clone a frozen docx version into a draft sub-version, then redirect
    the admin into the required-flags editor. Multi-locale versions clone
    every locale in one shot so the cross-locale field-set invariant holds."""
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    template = await session.get(Template, tv.template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    actor = getattr(request.state, "admin_user", None)
    try:
        new_rows = await supersede_docx_version(
            session, parent_version_id=version_id, actor=actor,
        )
    except ValueError as e:
        return _redirect(
            f"/ui/templates/{tv.template_id}", flash=str(e), flash_kind="error",
        )
    # Edit the default-locale draft first — the field set is invariant across
    # locales so editing any one of them stages the change for the whole set.
    default_draft = next(
        (r for r in new_rows if r.locale == "default"), new_rows[0],
    )
    return _redirect(
        f"/ui/versions/{default_draft.id}/edit-required",
        flash=(
            f"Superseded v{tv.version}.{tv.subversion} → "
            f"draft v{default_draft.version}.{default_draft.subversion}."
        ),
        flash_kind="success",
    )


@router.get("/versions/{version_id}/edit-required", response_class=HTMLResponse)
async def edit_required_form(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    template = await session.get(Template, tv.template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    if tv.is_frozen:
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash=f"v{tv.version}.{tv.subversion} is already frozen — supersede it again to edit.",
            flash_kind="error",
        )
    fields = tv.schema.get("fields", [])
    return templates.TemplateResponse(
        request,
        "edit_required.html",
        await _base_ctx(
            request,
            session,
            template=template,
            version=tv,
            fields=fields,
            flash=flash,
            flash_kind=flash_kind,
        ),
    )


@router.post("/versions/{version_id}/edit-required", response_model=None)
async def edit_required_submit(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    template = await session.get(Template, tv.template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    if tv.is_frozen:
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash=f"v{tv.version}.{tv.subversion} is already frozen.",
            flash_kind="error",
        )
    form = await request.form()
    # Form posts every field name; checked checkboxes contribute their value,
    # unchecked ones are absent. Derive required-map by checking presence
    # against the full field roster.
    required_map: dict[str, bool] = {}
    for f in tv.schema.get("fields", []):
        name = f.get("name")
        if not name:
            continue
        required_map[name] = form.get(f"required__{name}") == "on"

    # Propagate to every locale of this (version, subversion) — cross-locale
    # invariant means required flags must match across siblings.
    siblings = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == tv.template_id,
            TemplateVersion.version == tv.version,
            TemplateVersion.subversion == tv.subversion,
        )
    )
    for sib in siblings.scalars():
        sib.schema = apply_required_edits(sib.schema, required_map)
    await session.commit()

    action = str(form.get("action", "save")).strip()
    if action == "freeze":
        actor = getattr(request.state, "admin_user", None)
        try:
            frozen = await refreeze_docx_subversion(
                session, version_id=version_id, actor=actor,
            )
            # Freeze every sibling locale too so the (version, subversion)
            # tuple becomes fully published-ready.
            siblings2 = await session.execute(
                select(TemplateVersion).where(
                    TemplateVersion.template_id == tv.template_id,
                    TemplateVersion.version == tv.version,
                    TemplateVersion.subversion == tv.subversion,
                    TemplateVersion.id != tv.id,
                    TemplateVersion.is_frozen.is_(False),
                )
            )
            for sib in siblings2.scalars():
                await refreeze_docx_subversion(
                    session, version_id=sib.id, actor=actor,
                )
        except ValueError as e:
            return _redirect(
                f"/ui/versions/{version_id}/edit-required",
                flash=f"Freeze rejected: {e}", flash_kind="error",
            )
        return _redirect(
            f"/ui/templates/{tv.template_id}",
            flash=(
                f"Froze v{frozen.version}.{frozen.subversion} "
                f"(hash {frozen.version_hash[:12]}…). Publish to serve it."
            ),
            flash_kind="success",
        )
    return _redirect(
        f"/ui/versions/{version_id}/edit-required",
        flash="Saved.", flash_kind="success",
    )


@router.get("/templates/{template_id}/schema", response_class=HTMLResponse)
async def schema_view(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    version: int | None = None,
    locale: str | None = None,
) -> HTMLResponse:
    """Read-only view of a frozen version's accepted schema fields."""
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    version = version or template.current_version
    if version is None:
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="No frozen version to inspect yet.",
            flash_kind="error",
        )
    target_locale = locale or "default"
    result = await session.execute(
        select(TemplateVersion)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version,
            TemplateVersion.locale == target_locale,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.subversion.desc())
        .limit(1)
    )
    tv = result.scalar_one_or_none()
    if tv is None:
        raise HTTPException(
            404, f"Frozen v{version} (locale '{target_locale}') not found"
        )

    locales_result = await session.execute(
        select(TemplateVersion.locale)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.locale)
        .distinct()
    )
    available_locales = [row[0] for row in locales_result.all()]

    ts = TemplateSchema.model_validate(tv.schema)
    schema_json = json.dumps(tv.schema, indent=2, sort_keys=True)
    return templates.TemplateResponse(
        request,
        "schema_view.html",
        await _base_ctx(
            request,
            session,
            template=template,
            version=tv,
            schema_fields=ts.fields,
            schema_json=schema_json,
            available_locales=available_locales,
            selected_locale=target_locale,
        ),
    )


@router.get("/templates/{template_id}/render", response_class=HTMLResponse)
async def render_form(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    version: int | None = None,
    locale: str | None = None,
) -> HTMLResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    if not template.is_active:
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="Template is inactive — reactivate before rendering.",
            flash_kind="error",
        )
    version = version or template.current_version
    if version is None:
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="No frozen version to render yet.",
            flash_kind="error",
        )
    target_locale = locale or "default"
    result = await session.execute(
        select(TemplateVersion)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version,
            TemplateVersion.locale == target_locale,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.subversion.desc())
        .limit(1)
    )
    tv = result.scalar_one_or_none()
    if tv is None:
        raise HTTPException(
            404, f"Frozen v{version} (locale '{target_locale}') not found"
        )

    locales_result = await session.execute(
        select(TemplateVersion.locale)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.locale)
        .distinct()
    )
    available_locales = [row[0] for row in locales_result.all()]

    ts = TemplateSchema.model_validate(tv.schema)
    example = {f.name: _example_for(f.type) for f in ts.fields}
    profiles_result = await session.execute(
        select(CallerProfile)
        .where(CallerProfile.template_id == template_id)
        .order_by(CallerProfile.caller.asc(), CallerProfile.name.asc())
    )
    profiles = profiles_result.scalars().all()
    return templates.TemplateResponse(
        request,
        "render_form.html",
        await _base_ctx(
            request,
            session,
            template=template,
            version=tv,
            schema_fields=ts.fields,
            example_payload=json.dumps(example, indent=2),
            available_locales=available_locales,
            selected_locale=target_locale,
            profiles=profiles,
        ),
    )


@router.post("/templates/{template_id}/render", response_class=HTMLResponse)
async def render_submit(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    version: int = Form(...),
    locale: str = Form("default"),
    payload: str = Form(...),
    output_format: str = Form("docx"),
    caller: str | None = Form(None),
) -> HTMLResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    if not template.is_active:
        raise HTTPException(409, "Template is inactive")
    target_locale = locale or "default"
    result = await session.execute(
        select(TemplateVersion)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version,
            TemplateVersion.locale == target_locale,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.subversion.desc())
        .limit(1)
    )
    tv = result.scalar_one_or_none()
    if tv is None:
        raise HTTPException(
            404, f"Frozen v{version} (locale '{target_locale}') not found"
        )

    ts = TemplateSchema.model_validate(tv.schema)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        return await _render_page_with(
            request, template, tv, ts, payload, session=session,
            flash=f"Invalid JSON: {e}", flash_kind="error",
        )

    ok, errors = validate_payload(tv.schema, data)
    if not ok:
        return await _render_page_with(
            request, template, tv, ts, payload, session=session,
            flash="Payload validation failed: " + "; ".join(errors),
            flash_kind="error",
        )

    # PDF templates always emit PDF — there's no DOCX intermediate to convert
    # from, and asking LibreOffice to "convert" a PDF just re-flattens it. Force
    # the format server-side so a stale form selection can't produce nonsense.
    if tv.pdf_flavor is not None:
        output_format = "pdf"

    try:
        await check_render_quota(session, template.tenant_id)
    except QuotaExceeded as e:
        return await _render_page_with(
            request, template, tv, ts, payload, session=session,
            flash=f"Render quota exceeded ({e.current}/{e.limit} per day). "
                  "Wait for the 24h window to roll off or ask an admin to raise the cap.",
            flash_kind="error",
        )

    settings = get_settings()
    job = RenderJob(
        tenant_id=template.tenant_id,
        template_version_id=tv.id, state="queued", caller=(caller or None),
        payload=data, output_format=output_format,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    started_at = datetime.now(UTC)
    try:
        output = execute_merge(tv, data, output_format, settings=settings)
    except Exception as e:
        job.state = "failed"
        job.error = f"{type(e).__name__}: {e}"
        await session.commit()
        log.error(
            "ui_render_failed", job_id=str(job.id), template_id=str(template_id),
            version=version, error=job.error,
        )
        observe_render(
            tenant_id=str(template.tenant_id) if template.tenant_id else None,
            format=output_format,
            state="failed",
            duration_seconds=(datetime.now(UTC) - started_at).total_seconds(),
        )
        return await _render_page_with(
            request, template, tv, ts, payload, session=session,
            flash=f"Render failed: {e}", flash_kind="error", last_job=job,
        )

    result_key = persist_result(tv, job.id, output, output_format, settings=settings)
    job.state = "succeeded"
    job.result_blob_key = result_key
    job.result_bytes = len(output)
    session.add(
        Provenance(
            render_job_id=job.id, template_version_id=tv.id,
            version_hash=tv.version_hash,
            render_stack_hash=render_stack_hash(),
            caller=(caller or None),
        )
    )
    await session.commit()
    log.info(
        "ui_render_succeeded", job_id=str(job.id), template_id=str(template_id),
        version=version, format=output_format, result_bytes=job.result_bytes,
    )
    observe_render(
        tenant_id=str(template.tenant_id) if template.tenant_id else None,
        format=output_format,
        state="succeeded",
        duration_seconds=(datetime.now(UTC) - started_at).total_seconds(),
    )
    return await _render_page_with(
        request, template, tv, ts, payload, session=session,
        flash=f"Rendered {job.result_bytes} bytes.", flash_kind="success", last_job=job,
    )


@router.get("/jobs/{job_id}/result")
async def job_result_download(
    job_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Session-authenticated download for admin UI. The REST equivalent at
    /jobs/{id}/result requires a Bearer key — browsers only have the session
    cookie, so we expose the same stream under /ui.
    """
    job = await session.get(RenderJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _enforce_scope(request, job.tenant_id)
    if job.state != "succeeded" or not job.result_blob_key:
        raise HTTPException(409, f"Job not ready (state={job.state})")

    data = get_object(get_settings().s3_bucket_documents, job.result_blob_key)
    ext, ctype = format_meta(job.output_format)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=ctype,
        headers={"Content-Disposition": f'attachment; filename="{job_id}.{ext}"'},
    )


def _safe_source_filename(template_name: str, version: int, locale: str, ext: str) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", template_name).strip("-") or "template"
    tag = f"v{version}" if locale == "default" else f"v{version}-{locale}"
    return f"{slug}-{tag}.{ext}"


@router.get("/versions/{version_id}/download")
async def download_frozen_version(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    inline: bool = False,
) -> StreamingResponse:
    """Download the frozen source template — .docx for Word, .pdf for PDF flavors.

    `inline=1` swaps Content-Disposition to `inline` so the PDF preview iframe
    can render via the browser's native PDF viewer instead of triggering a save.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    if not tv.is_frozen or not tv.blob_key:
        raise HTTPException(409, "Version is not frozen")
    template = await session.get(Template, tv.template_id)
    if template is None:
        raise HTTPException(404, "Version not found")
    _enforce_scope(request, template.tenant_id)
    data = get_object(get_settings().s3_bucket_templates, tv.blob_key)
    fmt = "pdf" if tv.pdf_flavor is not None else "docx"
    ext, ctype = FORMAT_META[fmt]
    fname = _safe_source_filename(template.name, tv.version, tv.locale, ext)
    disposition = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{fname}"'}
    if inline:
        # Base CSP + middleware stamp `frame-ancestors 'none'` and
        # `X-Frame-Options: DENY` on every response, which blocks the
        # preview page's <iframe src="…download?inline=1"> from rendering
        # the PDF at all. Relax both headers when the caller opted into
        # inline — same-origin only, so the exposure is minimal.
        nonce = getattr(request.state, "csp_nonce", "") or ""
        headers["content-security-policy"] = "; ".join([
            "default-src 'self'",
            "frame-ancestors 'self'",
            "object-src 'none'",
            f"script-src 'self' 'nonce-{nonce}'",
            f"style-src 'self' 'nonce-{nonce}'",
        ])
        headers["x-frame-options"] = "SAMEORIGIN"
    return StreamingResponse(
        io.BytesIO(data),
        media_type=ctype,
        headers=headers,
    )


_DOCX_PREVIEW_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Preview</title>
<style nonce="__NONCE__">
  html, body { margin: 0; padding: 0; background: #e5e7eb; min-height: 100vh; }
  body { font-family: 'Segoe UI', Calibri, Arial, sans-serif; color: #1f2937; padding: 24px 0; }
  #status { text-align: center; color: #6b7280; padding: 40px 20px; font-size: 14px; }
  #docx {
    max-width: calc(100% - 32px);
    margin: 0 auto;
  }
  /* docx-preview renders each page as a .docx-wrapper > section.docx element.
     Give each section a Word-like page shadow so multi-page docs look right. */
  #docx > .docx-wrapper { background: transparent; padding: 0; }
  #docx > .docx-wrapper > section.docx {
    background: #ffffff !important;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15), 0 0 0 1px rgba(0,0,0,0.04);
    margin: 0 auto 24px !important;
    border-radius: 2px;
  }
</style>
<script src="https://unpkg.com/jszip@3.10.1/dist/jszip.min.js"></script>
<script src="https://unpkg.com/docx-preview@0.3.6/dist/docx-preview.min.js"></script>
</head>
<body>
<div id="status">Loading document…</div>
<div id="docx"></div>
<script nonce="__NONCE__">
(async function () {
  const status = document.getElementById('status');
  const container = document.getElementById('docx');
  try {
    const res = await fetch('__DOWNLOAD_URL__', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    await docx.renderAsync(blob, container, null, {
      inWrapper: true,
      ignoreWidth: false,
      ignoreHeight: false,
      breakPages: true,
      experimental: true,
    });
    status.remove();
  } catch (e) {
    status.textContent = 'Failed to load preview: ' + e.message;
    status.style.color = '#b91c1c';
  }
})();
</script>
</body>
</html>
"""


_PDF_PREVIEW_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Preview</title>
<style nonce="__NONCE__">
  html, body { margin: 0; padding: 0; height: 100%; background: #e5e7eb; }
  iframe { display: block; width: 100%; height: 100%; border: 0; background: #ffffff; }
</style>
</head>
<body>
<iframe src="__DOWNLOAD_URL__" title="PDF preview"></iframe>
</body>
</html>
"""


@router.get("/versions/{version_id}/preview.html", response_class=HTMLResponse)
async def preview_frozen_tagged_html(
    version_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """In-browser preview of the frozen template — docx-preview for .docx,
    browser-native PDF viewer for .pdf. Session cookie is sent by the fetch
    (docx) or the iframe request (pdf), so the same auth gate applies.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise HTTPException(404, "Version not found")
    if not tv.is_frozen or not tv.blob_key:
        raise HTTPException(409, "Version is not frozen")
    template = await session.get(Template, tv.template_id)
    if template is None:
        raise HTTPException(404, "Version not found")
    _enforce_scope(request, template.tenant_id)
    # Preview needs a relaxed CSP (unpkg for docx-preview, blob: for extracted
    # images) and must allow same-origin framing so template_detail.html can
    # embed it in an <iframe>. `headers.setdefault()` in the middleware means
    # these route-set headers win over the hardened defaults.
    nonce = getattr(request.state, "csp_nonce", "") or ""
    if tv.pdf_flavor is not None:
        # Inline disposition on the download so the browser renders the PDF
        # in-place instead of triggering a Save dialog inside the iframe.
        download_url = f"/ui/versions/{version_id}/download?inline=1"
        body = _PDF_PREVIEW_HTML.replace("__DOWNLOAD_URL__", download_url).replace(
            "__NONCE__", nonce
        )
    else:
        download_url = f"/ui/versions/{version_id}/download"
        body = _DOCX_PREVIEW_HTML.replace("__DOWNLOAD_URL__", download_url).replace(
            "__NONCE__", nonce
        )
    return HTMLResponse(
        content=body,
        headers={
            "content-security-policy": preview_csp(nonce),
            "x-frame-options": "SAMEORIGIN",
        },
    )


async def _load_frozen_variant(
    session: AsyncSession,
    template_id: uuid.UUID,
    version_number: int,
    locale: str,
) -> TemplateVersion | None:
    # Highest sub for the (version, locale) — diff always compares the tip
    # of one version against the tip of another.
    result = await session.execute(
        select(TemplateVersion)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version_number,
            TemplateVersion.locale == locale,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.subversion.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/templates/{template_id}/diff", response_class=HTMLResponse)
async def template_diff(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    from_version: int | None = None,
    to_version: int | None = None,
    from_locale: str = "default",
    to_locale: str = "default",
) -> HTMLResponse:
    """Side-by-side deltas between two frozen versions.

    Defaults: to = highest frozen default-locale, from = currently-published
    version (or the version below `to` if nothing is published yet). This
    matches the common "what would rollback change?" review question.
    """
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)

    frozen_defaults = (await session.execute(
        select(TemplateVersion)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.locale == "default",
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.version.desc())
    )).scalars().all()

    if len(frozen_defaults) < 2 and (from_version is None or to_version is None):
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="Need at least two frozen versions to diff.",
            flash_kind="info",
        )

    if to_version is None:
        to_version = frozen_defaults[0].version
    if from_version is None:
        from_version = (
            template.current_version
            if template.current_version and template.current_version != to_version
            else next(
                (v.version for v in frozen_defaults if v.version != to_version),
                to_version,
            )
        )

    tv_from = await _load_frozen_variant(session, template_id, from_version, from_locale)
    tv_to = await _load_frozen_variant(session, template_id, to_version, to_locale)
    if tv_from is None or tv_to is None:
        missing = []
        if tv_from is None:
            missing.append(f"v{from_version}/{from_locale}")
        if tv_to is None:
            missing.append(f"v{to_version}/{to_locale}")
        return _redirect(
            f"/ui/templates/{template_id}",
            flash=f"Cannot diff: no frozen variant for {', '.join(missing)}.",
            flash_kind="error",
        )

    settings = get_settings()
    from_bytes = get_object(settings.s3_bucket_templates, tv_from.blob_key)
    to_bytes = get_object(settings.s3_bucket_templates, tv_to.blob_key)
    diff = compute_template_diff(
        from_schema=tv_from.schema,
        to_schema=tv_to.schema,
        from_bytes=from_bytes,
        to_bytes=to_bytes,
        from_label=f"v{from_version}/{from_locale}",
        to_label=f"v{to_version}/{to_locale}",
    )

    # Version + locale menus for the compare-picker.
    all_frozen = (await session.execute(
        select(TemplateVersion)
        .where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(TemplateVersion.version.desc(), TemplateVersion.locale)
    )).scalars().all()
    version_locales: dict[int, list[str]] = {}
    for v in all_frozen:
        version_locales.setdefault(v.version, []).append(v.locale)

    return templates.TemplateResponse(
        request,
        "template_diff.html",
        await _base_ctx(
            request,
            session,
            template=template,
            tv_from=tv_from,
            tv_to=tv_to,
            diff=diff,
            version_locales=version_locales,
            current_version=template.current_version,
        ),
    )


@router.get("/templates/{template_id}/provenance", response_class=HTMLResponse)
async def provenance_view(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    result = await session.execute(
        select(Provenance)
        .join(TemplateVersion, TemplateVersion.id == Provenance.template_version_id)
        .where(TemplateVersion.template_id == template_id)
        .order_by(Provenance.rendered_at.desc())
    )
    entries = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "provenance.html",
        await _base_ctx(request, session, template=template, entries=entries),
    )


async def _render_page_with(
    request: Request,
    template: Template,
    tv: TemplateVersion,
    ts: TemplateSchema,
    payload: str,
    *,
    session: AsyncSession | None = None,
    flash: str | None = None,
    flash_kind: str | None = None,
    last_job: RenderJob | None = None,
) -> HTMLResponse:
    available_locales: list[str] = []
    profiles: list[CallerProfile] = []
    if session is not None:
        locales_result = await session.execute(
            select(TemplateVersion.locale)
            .where(
                TemplateVersion.template_id == template.id,
                TemplateVersion.version == tv.version,
                TemplateVersion.is_frozen.is_(True),
            )
            .order_by(TemplateVersion.locale)
            .distinct()
        )
        available_locales = [row[0] for row in locales_result.all()]
        profiles_result = await session.execute(
            select(CallerProfile)
            .where(CallerProfile.template_id == template.id)
            .order_by(CallerProfile.caller.asc(), CallerProfile.name.asc())
        )
        profiles = list(profiles_result.scalars().all())
    return templates.TemplateResponse(
        request,
        "render_form.html",
        await _base_ctx(
            request,
            session,
            template=template,
            version=tv,
            schema_fields=ts.fields,
            example_payload=payload,
            flash=flash,
            flash_kind=flash_kind,
            last_job=last_job,
            available_locales=available_locales,
            selected_locale=tv.locale,
            profiles=profiles,
        ),
    )


def _redirect(
    path: str,
    *,
    flash: str | None = None,
    flash_kind: str | None = None,
    new_key: str | None = None,
) -> RedirectResponse:
    params: dict[str, str] = {}
    if flash:
        params["flash"] = flash
        params["flash_kind"] = _normalize_flash_kind(flash_kind) or "info"
    if new_key:
        params["new_key"] = new_key
    if params:
        from urllib.parse import urlencode
        return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)
    return RedirectResponse(path, status_code=303)


def _example_for(field_type: str) -> Any:
    return {
        "string": "",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
        "date": "2026-01-01",
        "list": [],
        "object": {},
    }.get(field_type, "")


# ---------------------------------------------------------------------------
# Auth: login / logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str | None = None) -> HTMLResponse:
    # The 503 "disabled" state is gated on the env-var admin credentials only,
    # not the admin_users table, so this route stays cheap and can render before
    # migrations run on a fresh deploy. Once a DB user is seeded, an operator
    # can retire the env-var hash and this branch stops firing.
    settings = get_settings()
    disabled = not settings.admin_password_hash
    if disabled:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"disabled": True, "admin_user": None},
            status_code=503,
        )
    return templates.TemplateResponse(
        request, "login.html", {"disabled": False, "next": next, "admin_user": None}
    )


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    settings = get_settings()
    if not settings.admin_password_hash:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"disabled": True, "admin_user": None},
            status_code=503,
        )

    ip = client_ip(request)
    allowed, retry_after = await check_login_attempt(ip, username)
    if not allowed:
        log.warning(
            "admin_login_rate_limited",
            username=username,
            ip=ip,
            retry_after=retry_after,
            window_seconds=LOGIN_WINDOW_SECONDS,
            limit=LOGIN_MAX_ATTEMPTS,
        )
        resp = templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": (
                    f"Too many sign-in attempts. Try again in "
                    f"{max(1, retry_after // 60)} minute(s)."
                ),
                "disabled": False,
                "next": next,
                "admin_user": None,
            },
            status_code=429,
        )
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    identity = await authenticate_admin(session, username, password)
    if identity is None:
        log.info("admin_login_failed", username=username, ip=ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials.", "disabled": False, "next": next, "admin_user": None},
            status_code=401,
        )

    # Success — wipe this user's throttle bucket so previous typos don't count.
    await reset_login_attempts(ip, username)

    cookie_value = sign_session(username)
    dest = next or "/ui/dashboard"
    if not dest.startswith("/"):
        dest = "/ui/dashboard"
    resp = RedirectResponse(dest, status_code=303)
    cookie_secure = secure_cookie_flag(request)
    resp.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=cookie_secure,
        path="/",
    )
    # Issue a fresh CSRF token alongside the session so the first authenticated
    # POST already has a cookie to match against.
    resp.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(CSRF_TOKEN_BYTES),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=cookie_secure,
        path="/",
    )
    log.info("admin_login_success", username=username, ip=ip)
    return resp


@router.post("/logout")
async def logout_submit(request: Request) -> RedirectResponse:
    resp = RedirectResponse("/ui/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


import re as _re  # noqa: E402 — kept adjacent to the regex it compiles

_TENANT_SCOPED_PATH = _re.compile(
    r"^/ui/admin/tenants/[0-9a-fA-F-]{36}(/keys.*)?$"
)


@router.post("/select-tenant")
async def select_tenant(
    request: Request,
    tenant_id: str = Form(""),
    next: str = Form("/ui/"),
) -> RedirectResponse:
    """Persist the sidebar workspace picker's selection in a cookie.

    Redirect resolution:
      - If `next` has a `?tenant=` query param, rewrite it to match the picker's
        new selection (or strip it when picker chose "All workspaces"). Otherwise
        the page's own in-URL filter would trump the picker's choice on reload.
      - If `next` is a tenant-scoped admin path (.../tenants/<uuid>/keys),
        rewrite the UUID segment; if picker chose "All workspaces", fall back to
        the tenants list since there is no cross-tenant keys page.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    # tenant_admin's scope is immovable — quietly pin the cookie to their tenant.
    # Their sidebar renders only that tenant anyway, so this only bites when a
    # crafted form POST tries to smuggle another tenant's UUID.
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenant_id = str(ident.tenant_id)

    dest = next if next.startswith("/") else "/ui/"

    # Rewrite/strip ?tenant= in the query string to match the picker.
    parts = urlsplit(dest)
    if parts.query:
        qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "tenant"]
        if tenant_id:
            qs.append(("tenant", tenant_id))
        dest = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(qs), parts.fragment))

    # Rewrite the tenant UUID segment on tenant-scoped admin paths.
    path_only = urlsplit(dest).path
    m = _TENANT_SCOPED_PATH.match(path_only)
    if m:
        if tenant_id:
            dest = f"/ui/admin/tenants/{tenant_id}{m.group(1) or ''}"
        else:
            dest = "/ui/admin/tenants"

    resp = RedirectResponse(dest, status_code=303)
    if tenant_id:
        resp.set_cookie(
            "docgen_ui_tenant", tenant_id, max_age=60 * 60 * 24 * 30,
            httponly=False, samesite="lax",
            secure=secure_cookie_flag(request), path="/",
        )
    else:
        resp.delete_cookie("docgen_ui_tenant", path="/")
    return resp


# ---------------------------------------------------------------------------
# Admin: tenants + API keys (superuser)
# ---------------------------------------------------------------------------

@router.get("/admin/tenants", response_class=HTMLResponse)
async def admin_tenants(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """List of tenants, filtered by the sidebar workspace picker.

    Cookie from the picker narrows the list to a single tenant; "All workspaces"
    (no cookie) shows every tenant. Same rules as the templates list.
    """
    _require_super_admin(request)
    from docgen.store.models import Tenant
    stmt = select(Tenant).order_by(Tenant.name.asc())
    cookie_tid = request.cookies.get("docgen_ui_tenant")
    if cookie_tid:
        try:
            stmt = stmt.where(Tenant.id == uuid.UUID(cookie_tid))
        except ValueError:
            pass
    res = await session.execute(stmt)
    tenants_list = res.scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_tenants.html",
        await _base_ctx(request, session, active="admin", tenants=tenants_list),
    )


@router.post("/admin/tenants")
async def admin_create_tenant(
    request: Request,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.store.models import Tenant
    name_clean = name.strip()
    if not name_clean:
        return _redirect("/ui/admin/tenants", flash="Name is required.", flash_kind="error")
    session.add(Tenant(name=name_clean))
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        return _redirect("/ui/admin/tenants", flash=f"Create failed: {e}", flash_kind="error")
    log.info("tenant_created", name=name_clean)
    return _redirect(
        "/ui/admin/tenants", flash=f"Created tenant '{name_clean}'.", flash_kind="success"
    )


@router.get("/admin/tenants/{tenant_id}/keys", response_class=HTMLResponse)
async def admin_keys(
    tenant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    new_key: str | None = None,
) -> HTMLResponse:
    _enforce_scope(request, tenant_id)
    from docgen.store.models import Tenant
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    keys_res = await session.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at.desc())
    )
    keys = keys_res.scalars().all()
    # MCP endpoint for Claude Desktop config. Prefer the operator-set public URL;
    # fall back to the request's own scheme+host so a fresh deploy without
    # DOCGEN_PUBLIC_BASE_URL still yields a working snippet.
    settings = get_settings()
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}"
    mcp_url = f"{base}/mcp/"
    # Server key inside claude_desktop_config.json — a friendly slug of the
    # tenant name so operators running multiple tenants side-by-side can tell
    # entries apart without editing the snippet.
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", tenant.name.lower()).strip("-") or "tenant"
    mcp_server_name = f"quire-{slug}"
    return templates.TemplateResponse(
        request,
        "admin_keys.html",
        await _base_ctx(
            request, session, active="admin", tenant=tenant, keys=keys, new_key=new_key,
            mcp_url=mcp_url, mcp_server_name=mcp_server_name,
        ),
    )


@router.post("/admin/tenants/{tenant_id}/keys")
async def admin_create_key(
    tenant_id: uuid.UUID,
    request: Request,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _enforce_scope(request, tenant_id)
    from docgen.store.models import Tenant
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    name_clean = name.strip()
    if not name_clean:
        return _redirect(
            f"/ui/admin/tenants/{tenant_id}/keys",
            flash="Name is required.", flash_kind="error",
        )
    raw, prefix, digest = generate_api_key()
    session.add(ApiKey(tenant_id=tenant_id, name=name_clean, key_prefix=prefix, key_hash=digest))
    await session.commit()
    log.info("api_key_created", tenant_id=str(tenant_id), key_prefix=prefix, name=name_clean)
    return _redirect(f"/ui/admin/tenants/{tenant_id}/keys", new_key=raw)


@router.post("/admin/tenants/{tenant_id}/keys/{key_id}/revoke")
async def admin_revoke_key(
    tenant_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _enforce_scope(request, tenant_id)
    key = await session.get(ApiKey, key_id)
    if key is None or key.tenant_id != tenant_id:
        raise HTTPException(404, "Key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.commit()
        log.info(
            "api_key_revoked",
            tenant_id=str(tenant_id),
            key_id=str(key_id),
            key_prefix=key.key_prefix,
        )
    return _redirect(
        f"/ui/admin/tenants/{tenant_id}/keys",
        flash=f"Revoked key '{key.name}'.", flash_kind="success",
    )


# ---------------------------------------------------------------------------
# Admin: per-tenant inbox settings
#
# The inbox agent uses `tenants.inbound_config` to decide (a) which sender
# domains map to this tenant, (b) an optional subject_tag to force-route,
# (c) the auto-publish threshold, and (d) extra CC addresses on outbound
# replies. Both super_admin (any tenant) and tenant_admin (their own) can
# edit — scope is enforced by _enforce_scope.
# ---------------------------------------------------------------------------


def _parse_lines(raw: str | None) -> list[str]:
    """Split a textarea value into deduped, non-empty, stripped lines.

    Users type one item per line; commas within a line also split. Case
    normalization (lowercase) is applied to domains only, in the caller.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace(",", "\n").splitlines():
        v = chunk.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


@router.get("/admin/tenants/{tenant_id}/inbox", response_class=HTMLResponse)
async def admin_inbox_settings(
    tenant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _enforce_scope(request, tenant_id)
    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    settings = get_settings()
    cfg = dict(tenant.inbound_config or {})
    return templates.TemplateResponse(
        request,
        "admin_inbox_settings.html",
        await _base_ctx(
            request,
            session,
            active="admin",
            tenant=tenant,
            inbox_username=settings.inbox_username,
            inbox_enabled=settings.inbox_enabled,
            sender_domains="\n".join(cfg.get("sender_domains", []) or []),
            subject_tag=cfg.get("subject_tag", "") or "",
            notify_addresses="\n".join(cfg.get("notify_addresses", []) or []),
            auto_approve_threshold=cfg.get("auto_approve_threshold", 99.0),
        ),
    )


@router.post("/admin/tenants/{tenant_id}/inbox", response_model=None)
async def admin_save_inbox_settings(
    tenant_id: uuid.UUID,
    request: Request,
    sender_domains: str = Form(""),
    subject_tag: str = Form(""),
    notify_addresses: str = Form(""),
    auto_approve_threshold: str = Form("99"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _enforce_scope(request, tenant_id)
    from sqlalchemy.orm.attributes import flag_modified

    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    try:
        threshold = float(auto_approve_threshold.strip() or "99")
    except ValueError:
        return _redirect(
            f"/ui/admin/tenants/{tenant_id}/inbox",
            flash="Auto-approve threshold must be a number between 0 and 100.",
            flash_kind="error",
        )
    threshold = max(0.0, min(100.0, threshold))

    cfg = dict(tenant.inbound_config or {})
    cfg["sender_domains"] = [d.lower() for d in _parse_lines(sender_domains)]
    tag = subject_tag.strip()
    if tag:
        cfg["subject_tag"] = tag
    else:
        cfg.pop("subject_tag", None)
    cfg["notify_addresses"] = _parse_lines(notify_addresses)
    cfg["auto_approve_threshold"] = threshold
    tenant.inbound_config = cfg
    flag_modified(tenant, "inbound_config")
    await session.commit()
    log.info(
        "tenant_inbox_settings_saved",
        tenant_id=str(tenant_id),
        sender_domains=len(cfg["sender_domains"]),
        subject_tag=bool(tag),
        notify_addresses=len(cfg["notify_addresses"]),
        threshold=threshold,
    )
    return _redirect(
        f"/ui/admin/tenants/{tenant_id}/inbox",
        flash="Inbox routing settings saved.",
        flash_kind="success",
    )


# ---------------------------------------------------------------------------
# Admin: MDM connector (per-tenant history retrieval — ADR-0004)
#
# Every tenant carries its own `mdm_config` JSONB. Empty → the pipeline
# picks the simulated adapter (demo-friendly). `{"provider": "null"}`
# opts out. Anything else is treated as an HTTP connector name and needs
# `base_url` + `api_key_env` (env var *name*, not the raw key).
# ---------------------------------------------------------------------------

# Provider tag → CSS class on the sidecar badge. Anything unknown falls
# through to "http" so the operator sees the HTTP badge for a real
# connector name (e.g. "state-mdm-v1", "salesforce-hc").
_MDM_PROVIDER_CLASS = {"simulated": "simulated", "": "simulated", "null": "null"}


@router.get("/admin/tenants/{tenant_id}/mdm", response_class=HTMLResponse)
async def admin_mdm_settings(
    tenant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _enforce_scope(request, tenant_id)
    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    cfg = dict(tenant.mdm_config or {})
    provider = str(cfg.get("provider") or "").strip().lower()
    api_key_env = str(cfg.get("api_key_env") or "")
    families = cfg.get("program_families") or []
    if not isinstance(families, list):
        families = []
    return templates.TemplateResponse(
        request,
        "admin_mdm_settings.html",
        await _base_ctx(
            request,
            session,
            active="admin",
            tenant=tenant,
            provider=provider,
            provider_class=_MDM_PROVIDER_CLASS.get(provider, "http"),
            base_url=cfg.get("base_url") or "",
            api_key_env=api_key_env,
            # Reveal in the sidebar whether the named env var is
            # actually populated in this process — makes a stale
            # config obvious to the operator before it fails at runtime.
            api_key_env_present=bool(api_key_env and os.environ.get(api_key_env)),
            program_families="\n".join(str(f) for f in families),
            max_window_days=int(cfg.get("max_window_days") or 1825),
            max_events=int(cfg.get("max_events") or 200),
            timeout=cfg.get("timeout") or 30,
        ),
    )


@router.post("/admin/tenants/{tenant_id}/mdm", response_model=None)
async def admin_save_mdm_settings(
    tenant_id: uuid.UUID,
    request: Request,
    provider: str = Form(""),
    base_url: str = Form(""),
    api_key_env: str = Form(""),
    program_families: str = Form(""),
    max_window_days: str = Form("1825"),
    max_events: str = Form("200"),
    timeout: str = Form("30"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _enforce_scope(request, tenant_id)
    from sqlalchemy.orm.attributes import flag_modified

    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    def _coerce_num(raw: str, default, kind):
        # Silent fallback → confusing; surface as a flash instead so the
        # operator sees why their value didn't stick.
        raw_stripped = (raw or "").strip()
        if not raw_stripped:
            return default, None
        try:
            return kind(raw_stripped), None
        except ValueError:
            return default, f"Invalid {kind.__name__} value {raw_stripped!r}"

    window, window_err = _coerce_num(max_window_days, 1825, int)
    events, events_err = _coerce_num(max_events, 200, int)
    to, to_err = _coerce_num(timeout, 30.0, float)
    err = window_err or events_err or to_err
    if err:
        return _redirect(
            f"/ui/admin/tenants/{tenant_id}/mdm",
            flash=err,
            flash_kind="error",
        )

    cfg: dict[str, Any] = {}
    provider_clean = provider.strip().lower()
    if provider_clean:
        cfg["provider"] = provider_clean
    base_url_clean = base_url.strip()
    if base_url_clean:
        cfg["base_url"] = base_url_clean
    api_key_env_clean = api_key_env.strip()
    if api_key_env_clean:
        cfg["api_key_env"] = api_key_env_clean
    families = _parse_lines(program_families)
    if families:
        cfg["program_families"] = families
    # Only persist numeric fields when they differ from the dataclass
    # defaults — keeps the JSONB tidy so an operator reading it in psql
    # sees only what was actually customized.
    if window != 1825:
        cfg["max_window_days"] = window
    if events != 200:
        cfg["max_events"] = events
    if to != 30.0:
        cfg["timeout"] = to

    tenant.mdm_config = cfg
    flag_modified(tenant, "mdm_config")
    await session.commit()
    log.info(
        "tenant_mdm_settings_saved",
        tenant_id=str(tenant_id),
        provider=cfg.get("provider") or "(empty→simulated)",
        base_url_set=bool(cfg.get("base_url")),
        api_key_env=cfg.get("api_key_env") or None,
        families=len(cfg.get("program_families") or []),
    )
    return _redirect(
        f"/ui/admin/tenants/{tenant_id}/mdm",
        flash="MDM connector settings saved.",
        flash_kind="success",
    )


# ---------------------------------------------------------------------------
# Admin: per-tenant quotas (super_admin-only)
#
# Quotas are a platform-level knob — a tenant_admin should not be able to
# raise their own cap, so both routes are guarded by _require_super_admin.
# Blank field → NULL → unlimited (matches the `docgen.quotas` semantics).
# ---------------------------------------------------------------------------


@router.get("/admin/tenants/{tenant_id}/quotas", response_class=HTMLResponse)
async def admin_tenant_quotas(
    tenant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_super_admin(request)
    from datetime import datetime, timedelta

    from docgen.store.models import RenderJob, Template, Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    since = datetime.now(tz=UTC) - timedelta(hours=24)
    renders_24h = (
        await session.execute(
            select(func.count())
            .select_from(RenderJob)
            .where(RenderJob.tenant_id == tenant_id)
            .where(RenderJob.submitted_at >= since)
        )
    ).scalar_one()
    active_templates = (
        await session.execute(
            select(func.count())
            .select_from(Template)
            .where(Template.tenant_id == tenant_id)
            .where(Template.is_active.is_(True))
        )
    ).scalar_one()

    return templates.TemplateResponse(
        request,
        "admin_tenant_quotas.html",
        await _base_ctx(
            request, session, active="admin",
            tenant=tenant,
            usage_renders_24h=renders_24h,
            usage_active_templates=active_templates,
        ),
    )


@router.post("/admin/tenants/{tenant_id}/quotas", response_model=None)
async def admin_save_tenant_quotas(
    tenant_id: uuid.UUID,
    request: Request,
    quota_renders_per_day: str = Form(""),
    quota_active_templates: str = Form(""),
    quota_storage_bytes: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    def _parse(raw: str, field: str) -> int | None:
        s = raw.strip()
        if not s:
            return None
        try:
            v = int(s)
        except ValueError as e:
            raise ValueError(f"{field} must be a whole number or blank.") from e
        if v < 0:
            raise ValueError(f"{field} must be zero or positive.")
        return v

    try:
        renders_cap = _parse(quota_renders_per_day, "Renders per day")
        active_cap = _parse(quota_active_templates, "Active templates")
        storage_cap = _parse(quota_storage_bytes, "Storage bytes")
    except ValueError as e:
        return _redirect(
            f"/ui/admin/tenants/{tenant_id}/quotas",
            flash=str(e), flash_kind="error",
        )

    tenant.quota_renders_per_day = renders_cap
    tenant.quota_active_templates = active_cap
    tenant.quota_storage_bytes = storage_cap
    await session.commit()
    log.info(
        "tenant_quotas_saved",
        tenant_id=str(tenant_id),
        renders_per_day=renders_cap,
        active_templates=active_cap,
        storage_bytes=storage_cap,
    )
    return _redirect(
        f"/ui/admin/tenants/{tenant_id}/quotas",
        flash="Quotas saved.", flash_kind="success",
    )


# ---------------------------------------------------------------------------
# Admin: tenant pipeline models + triage routing (super_admin-only)
# ---------------------------------------------------------------------------

@router.get("/admin/tenants/{tenant_id}/models", response_class=HTMLResponse)
async def admin_tenant_models(
    tenant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_super_admin(request)
    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    settings = get_settings()
    catalog = get_cached_catalog(
        base_url=(settings.ai_base_url or "https://api.studio.nebius.com/v1"),
        api_key=(settings.ai_api_key or ""),
    )
    platform_defaults = {
        "extract": settings.pdf_vision_ocr_model,
        "triage": settings.triage_model,
        "reason": settings.ai_verdict_model,
    }
    return templates.TemplateResponse(
        request,
        "admin_tenant_models.html",
        await _base_ctx(
            request, session, active="admin",
            tenant=tenant,
            platform_defaults=platform_defaults,
            vision_models=catalog.vision_models(),
            reason_models=catalog.chat_models(),
        ),
    )


@router.post("/admin/tenants/{tenant_id}/models", response_model=None)
async def admin_save_tenant_models(
    tenant_id: uuid.UUID,
    request: Request,
    extract_model: str = Form(""),
    triage_model: str = Form(""),
    retrieve_model: str = Form(""),
    reason_model: str = Form(""),
    verifier_model: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.store.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    # Blank string ("— use platform default —") → NULL so the fallback chain
    # kicks in and env-var rotation propagates through automatically.
    tenant.extract_model = extract_model.strip() or None
    tenant.triage_model = triage_model.strip() or None
    tenant.retrieve_model = retrieve_model.strip() or None
    tenant.reason_model = reason_model.strip() or None
    tenant.verifier_model = verifier_model.strip() or None

    await session.commit()
    return _redirect(
        f"/ui/admin/tenants/{tenant_id}/models",
        flash="Model defaults saved.", flash_kind="success",
    )


@router.get("/admin/tenants/{tenant_id}/triage-routing", response_class=HTMLResponse)
async def admin_tenant_triage_routing(
    tenant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_super_admin(request)
    from docgen.store.models import Tenant
    from pap_pipeline.config_resolver import resolve_pipeline_models

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    settings = get_settings()
    # Effective routing at the tenant level = platform defaults merged
    # with any tenant overrides. Program overrides don't apply here.
    resolved = resolve_pipeline_models(program=None, tenant=tenant, settings=settings)
    all_routes = ["decide", "pharmacy_direct", "crm_appeals", "crm_outreach", "reject"]

    return templates.TemplateResponse(
        request,
        "admin_tenant_triage_routing.html",
        await _base_ctx(
            request, session, active="admin",
            tenant=tenant,
            effective_routing=resolved.triage_routing,
            all_routes=all_routes,
        ),
    )


@router.post("/admin/tenants/{tenant_id}/triage-routing", response_model=None)
async def admin_save_tenant_triage_routing(
    tenant_id: uuid.UUID,
    request: Request,
    routing_json: str = Form("[]"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    import json as _json
    from docgen.store.models import Tenant
    from pap_pipeline.stages.triage import _DEFAULT_ROUTING

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    try:
        rows = _json.loads(routing_json)
        if not isinstance(rows, list):
            rows = []
    except (ValueError, TypeError):
        rows = []

    submitted: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ct = str(row.get("case_type", "")).strip()
        route = str(row.get("route", "")).strip()
        if not ct or not route:
            continue
        submitted[ct] = {
            "route": route,
            "decision_needed": bool(row.get("decision_needed", True)),
        }

    # Only store rows that differ from the platform default — keeps the
    # JSONB minimal and audit-visible ("tenant overrides these rows").
    overrides: dict[str, dict[str, Any]] = {}
    for ct, cfg in submitted.items():
        default = _DEFAULT_ROUTING.get(ct)
        if default is None or default != cfg:
            overrides[ct] = cfg
    tenant.triage_routing = overrides or None

    await session.commit()
    return _redirect(
        f"/ui/admin/tenants/{tenant_id}/triage-routing",
        flash="Triage routing saved.", flash_kind="success",
    )


# ---------------------------------------------------------------------------
# Admin: portal users (super_admin-only)
#
# Adds/deactivates admin_users rows so tenants can be given their own UI login
# instead of everyone sharing the env-var admin. Every route is guarded by
# _require_super_admin — tenant_admins can't see or edit this list.
# ---------------------------------------------------------------------------

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    _require_super_admin(request)
    from docgen.store.models import AdminUser, Tenant
    users_res = await session.execute(
        select(AdminUser, Tenant.name.label("tenant_name"))
        .join(Tenant, Tenant.id == AdminUser.tenant_id, isouter=True)
        .order_by(AdminUser.created_at.desc())
    )
    users = [
        {"user": row[0], "tenant_name": row[1]} for row in users_res.all()
    ]
    tenants_res = await session.execute(select(Tenant).order_by(Tenant.name.asc()))
    all_tenants = tenants_res.scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        await _base_ctx(
            request, session,
            active="admin",
            users=users,
            all_tenants=all_tenants,
            flash=flash,
            flash_kind=flash_kind,
        ),
    )


@router.post("/admin/users")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    tenant_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.auth import hash_password
    from docgen.store.models import AdminUser
    username_clean = username.strip()
    if not username_clean:
        return _redirect("/ui/admin/users", flash="Username is required.", flash_kind="error")
    if len(password) < 8:
        return _redirect(
            "/ui/admin/users",
            flash="Password must be at least 8 characters.",
            flash_kind="error",
        )
    if role not in ("super_admin", "tenant_admin"):
        return _redirect("/ui/admin/users", flash="Role must be super_admin or tenant_admin.", flash_kind="error")  # noqa: E501
    parsed_tenant: uuid.UUID | None = None
    if role == "tenant_admin":
        if not tenant_id:
            return _redirect(
                "/ui/admin/users",
                flash="tenant_admin requires a tenant.", flash_kind="error",
            )
        try:
            parsed_tenant = uuid.UUID(tenant_id)
        except ValueError:
            return _redirect(
                "/ui/admin/users",
                flash="Invalid tenant id.", flash_kind="error",
            )
    session.add(
        AdminUser(
            tenant_id=parsed_tenant,
            username=username_clean,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
    )
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        return _redirect(
            "/ui/admin/users",
            flash=f"Create failed (username may already exist): {e}",
            flash_kind="error",
        )
    log.info(
        "admin_user_created",
        username=username_clean, role=role,
        tenant_id=str(parsed_tenant) if parsed_tenant else None,
    )
    return _redirect(
        "/ui/admin/users",
        flash=f"Created admin user '{username_clean}' ({role}).",
        flash_kind="success",
    )


@router.post("/admin/users/{user_id}/activation")
async def admin_toggle_user(
    user_id: uuid.UUID,
    request: Request,
    active: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.store.models import AdminUser
    user = await session.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    want_active = active.lower() == "true"
    if user.is_active != want_active:
        user.is_active = want_active
        await session.commit()
        log.info(
            "admin_user_toggled",
            username=user.username, is_active=want_active,
        )
    verb = "reactivated" if want_active else "deactivated"
    return _redirect(
        "/ui/admin/users",
        flash=f"User '{user.username}' {verb}.",
        flash_kind="success",
    )


@router.post("/admin/users/{user_id}/password")
async def admin_reset_password(
    user_id: uuid.UUID,
    request: Request,
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.auth import hash_password
    from docgen.store.models import AdminUser
    if len(password) < 8:
        return _redirect(
            "/ui/admin/users",
            flash="Password must be at least 8 characters.",
            flash_kind="error",
        )
    user = await session.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.password_hash = hash_password(password)
    await session.commit()
    log.info("admin_user_password_reset", username=user.username)
    return _redirect(
        "/ui/admin/users",
        flash=f"Password reset for '{user.username}'.",
        flash_kind="success",
    )


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(
    user_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_super_admin(request)
    from docgen.store.models import AdminUser
    user = await session.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    # Guard against self-deletion — a super_admin locking themselves out is a
    # recoverable-but-annoying support ticket. Env-var break-glass still works
    # but that's not obvious to a panicked operator.
    ident = _identity(request)
    if ident is not None and ident.user_id == user_id:
        return _redirect(
            "/ui/admin/users",
            flash="Refusing to delete the account you're logged in as.",
            flash_kind="error",
        )
    username = user.username
    await session.delete(user)
    await session.commit()
    log.info("admin_user_deleted", username=username)
    return _redirect(
        "/ui/admin/users",
        flash=f"Deleted admin user '{username}'.",
        flash_kind="success",
    )


# ---------------------------------------------------------------------------
# Caller profiles: mapping wizard
# ---------------------------------------------------------------------------
#
# A caller profile stores a translation from a caller's native JSON shape into
# a template's frozen schema. Author it once here; the caller then POSTs their
# raw JSON to /render/mapped with the profile_id, and the server does the
# translation. Callers never have to shape their payload per template.

_TRANSFORMS = ["", "number", "date", "join", "split", "upper", "lower", "default"]


@router.get("/templates/{template_id}/profiles", response_class=HTMLResponse)
async def list_caller_profiles(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    result = await session.execute(
        select(CallerProfile)
        .where(CallerProfile.template_id == template_id)
        .order_by(CallerProfile.caller.asc(), CallerProfile.name.asc())
    )
    profiles = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "caller_profiles.html",
        await _base_ctx(
            request, session, template=template, profiles=profiles,
            current_version=template.current_version,
        ),
    )


@router.get("/templates/{template_id}/profiles/new", response_class=HTMLResponse)
async def new_profile_form(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    if template.current_version is None:
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="Publish a version before creating a caller profile.",
            flash_kind="error",
        )
    return templates.TemplateResponse(
        request,
        "profile_new.html",
        await _base_ctx(
            request, session, template=template,
            current_version=template.current_version,
        ),
    )


@router.post("/templates/{template_id}/profiles", response_model=None)
async def create_profile(
    template_id: uuid.UUID,
    request: Request,
    caller: str = Form(...),
    name: str = Form(...),
    sample: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    template = await session.get(Template, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)
    if template.current_version is None:
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="Publish a version before creating a caller profile.",
            flash_kind="error",
        )
    caller_clean = caller.strip()
    name_clean = name.strip()
    if not caller_clean or not name_clean:
        return _redirect(
            f"/ui/templates/{template_id}/profiles/new",
            flash="Caller and profile name are required.", flash_kind="error",
        )
    try:
        sample_json = json.loads(sample)
        if not isinstance(sample_json, dict):
            raise ValueError("sample must be a JSON object at the top level")
    except (json.JSONDecodeError, ValueError) as e:
        return _redirect(
            f"/ui/templates/{template_id}/profiles/new",
            flash=f"Invalid sample JSON: {e}", flash_kind="error",
        )

    # Resolve the frozen schema so we can auto-suggest against it. Uses the
    # exact published (version, subversion) so profile suggestions never
    # drift onto an unpublished sub-version.
    tv = await _resolve_default_frozen(
        session,
        template_id,
        template.current_version,
        template.current_subversion,
    )
    if tv is None:
        return _redirect(
            f"/ui/templates/{template_id}",
            flash="No frozen default variant for the current version.",
            flash_kind="error",
        )
    ts = TemplateSchema.model_validate(tv.schema)
    field_dicts = [{"name": f.name, "type": f.type} for f in ts.fields]
    suggested = suggest_mapping(field_dicts, sample_json)

    profile = CallerProfile(
        tenant_id=template.tenant_id,
        template_id=template_id,
        version=tv.version,
        caller=caller_clean,
        name=name_clean,
        mapping=suggested,
        sample_payload=sample_json,
    )
    session.add(profile)
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        return _redirect(
            f"/ui/templates/{template_id}/profiles/new",
            flash=f"Create failed (name may already exist for this caller): {e}",
            flash_kind="error",
        )
    await session.refresh(profile)
    log.info(
        "caller_profile_created", profile_id=str(profile.id),
        template_id=str(template_id), caller=caller_clean, name=name_clean,
        suggested_count=len(suggested),
    )
    return _redirect(
        f"/ui/profiles/{profile.id}",
        flash=f"Created profile — auto-matched {len(suggested)}/{len(field_dicts)} fields.",
        flash_kind="success",
    )


@router.get("/profiles/{profile_id}", response_class=HTMLResponse)
async def profile_wizard(
    profile_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    profile = await session.get(CallerProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    template = await session.get(Template, profile.template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    _enforce_scope(request, template.tenant_id)

    tv = await _resolve_default_frozen(session, profile.template_id, profile.version)
    if tv is None:
        return _redirect(
            f"/ui/templates/{profile.template_id}/profiles",
            flash=f"v{profile.version} is no longer available.", flash_kind="error",
        )
    ts = TemplateSchema.model_validate(tv.schema)

    # Version-drift detection: if the template moved on, offer an upgrade.
    drift = None
    if template.current_version and template.current_version != profile.version:
        latest_tv = await _resolve_default_frozen(
            session, profile.template_id, template.current_version
        )
        if latest_tv is not None:
            latest_ts = TemplateSchema.model_validate(latest_tv.schema)
            old_names = {f.name for f in ts.fields}
            new_names = {f.name for f in latest_ts.fields}
            drift = {
                "latest_version": template.current_version,
                "added": sorted(new_names - old_names),
                "removed": sorted(old_names - new_names),
            }

    leaves = list(walk_leaves(profile.sample_payload))
    return templates.TemplateResponse(
        request,
        "profile_wizard.html",
        await _base_ctx(
            request, session,
            template=template, profile=profile, version=tv,
            schema_fields=ts.fields,
            leaves=leaves,
            transforms=_TRANSFORMS,
            sample_json=json.dumps(profile.sample_payload, indent=2),
            drift=drift,
            flash=flash, flash_kind=flash_kind,
        ),
    )


@router.post("/profiles/{profile_id}")
async def save_profile(
    profile_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    profile = await session.get(CallerProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    _enforce_scope(request, profile.tenant_id)
    tv = await _resolve_default_frozen(session, profile.template_id, profile.version)
    if tv is None:
        return _redirect(
            f"/ui/templates/{profile.template_id}/profiles",
            flash="Frozen version missing.", flash_kind="error",
        )
    ts = TemplateSchema.model_validate(tv.schema)

    form = await request.form()
    mapping: dict[str, dict[str, Any]] = {}
    for f in ts.fields:
        mode = str(form.get(f"mode_{f.name}", "source"))
        entry: dict[str, Any] = {}
        if mode == "constant":
            entry["constant"] = str(form.get(f"constant_{f.name}", ""))
        elif mode == "source":
            src = str(form.get(f"source_{f.name}", "")).strip()
            if not src:
                continue  # skip unmapped
            entry["source"] = src
        else:
            continue  # "none" — skip
        transform = str(form.get(f"transform_{f.name}", "")).strip()
        if transform:
            arg = str(form.get(f"transform_arg_{f.name}", "")).strip()
            entry["transform"] = f"{transform}:{arg}" if arg else transform
        mapping[f.name] = entry

    profile.mapping = mapping
    await session.commit()
    log.info(
        "caller_profile_saved", profile_id=str(profile_id),
        target_count=len(mapping),
    )
    return _redirect(
        f"/ui/profiles/{profile_id}",
        flash=f"Saved mapping for {len(mapping)} field(s).", flash_kind="success",
    )


@router.post("/profiles/{profile_id}/test", response_model=None)
async def test_profile_render(
    profile_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | StreamingResponse:
    """Run the caller-native sample through mapping + render and return the docx.

    On mapping or render failure, redirect back to the wizard with a flash. Uses
    the same pipeline (mapping -> validate -> merge) as /render/mapped, but
    inlined here so a broken profile never touches RenderJob/Provenance.

    An optional `payload` form field lets the wizard test with an edited JSON
    payload without persisting it — falls back to the saved sample when absent.
    """
    profile = await session.get(CallerProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    _enforce_scope(request, profile.tenant_id)
    tv = await _resolve_default_frozen(session, profile.template_id, profile.version)
    if tv is None:
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash="Frozen version missing.", flash_kind="error",
        )
    form = await request.form()
    payload_raw = str(form.get("payload", "")).strip()
    if payload_raw:
        try:
            sample_data = json.loads(payload_raw)
        except json.JSONDecodeError as e:
            return _redirect(
                f"/ui/profiles/{profile_id}",
                flash=f"Invalid JSON payload: {e}", flash_kind="error",
            )
        if not isinstance(sample_data, dict):
            return _redirect(
                f"/ui/profiles/{profile_id}",
                flash="Payload must be a JSON object.", flash_kind="error",
            )
    else:
        sample_data = profile.sample_payload
    ts = TemplateSchema.model_validate(tv.schema)
    required = {f.name for f in ts.fields if f.required}
    try:
        mapped = apply_mapping(profile.mapping, sample_data, required=required)
    except MappingError as e:
        summary = "; ".join(
            f"{err['target']}: {err['error']}" for err in e.errors[:3]
        )
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash=f"Mapping failed: {summary}", flash_kind="error",
        )
    ok, errors = validate_payload(tv.schema, mapped)
    if not ok:
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash=f"Mapped payload failed schema validation: {'; '.join(errors[:3])}",
            flash_kind="error",
        )
    # PDF templates can only emit PDF; DOCX templates emit DOCX. Pick the
    # format from the template kind so a PDF-based profile doesn't blow up
    # with "PDF templates can only produce PDF output, not 'docx'".
    output_format = "pdf" if tv.pdf_flavor is not None else "docx"
    ext, media_type = FORMAT_META[output_format]
    try:
        output = execute_merge(tv, mapped, output_format, settings=get_settings())
    except Exception as e:
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash=f"Render failed: {e}", flash_kind="error",
        )
    return StreamingResponse(
        io.BytesIO(output),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="test-{profile_id}.{ext}"'
        },
    )


@router.post("/profiles/{profile_id}/delete")
async def delete_profile(
    profile_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    profile = await session.get(CallerProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    _enforce_scope(request, profile.tenant_id)
    template_id = profile.template_id
    await session.delete(profile)
    await session.commit()
    log.info("caller_profile_deleted", profile_id=str(profile_id))
    return _redirect(
        f"/ui/templates/{template_id}/profiles",
        flash="Profile deleted.", flash_kind="success",
    )


@router.post("/profiles/{profile_id}/upgrade", response_model=None)
async def upgrade_profile(
    profile_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Clone this profile against the template's current version.

    The old profile is kept (some callers may still be pinned to the old
    version until they cut over); the new one becomes the wizard target so the
    admin can adjust any fields the new schema added.
    """
    profile = await session.get(CallerProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    _enforce_scope(request, profile.tenant_id)
    template = await session.get(Template, profile.template_id)
    if template is None or template.current_version is None:
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash="No newer version to upgrade to.", flash_kind="error",
        )
    if template.current_version == profile.version:
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash="Profile is already on the current version.", flash_kind="info",
        )
    new_tv = await _resolve_default_frozen(
        session,
        profile.template_id,
        template.current_version,
        template.current_subversion,
    )
    if new_tv is None:
        return _redirect(
            f"/ui/profiles/{profile_id}",
            flash="Current frozen version not found.", flash_kind="error",
        )
    new_ts = TemplateSchema.model_validate(new_tv.schema)
    new_field_names = {f.name for f in new_ts.fields}
    # Keep only entries whose target still exists in the new schema.
    carried = {k: v for k, v in profile.mapping.items() if k in new_field_names}

    new_profile = CallerProfile(
        tenant_id=profile.tenant_id,
        template_id=profile.template_id,
        version=template.current_version,
        caller=profile.caller,
        name=profile.name,
        mapping=carried,
        sample_payload=profile.sample_payload,
    )
    session.add(new_profile)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        # Name collision: the caller already upgraded once. Point them at it.
        result = await session.execute(
            select(CallerProfile).where(
                CallerProfile.tenant_id == profile.tenant_id,
                CallerProfile.template_id == profile.template_id,
                CallerProfile.version == template.current_version,
                CallerProfile.caller == profile.caller,
                CallerProfile.name == profile.name,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return _redirect(
                f"/ui/profiles/{existing.id}",
                flash="A profile for the current version already exists.",
                flash_kind="info",
            )
        raise
    await session.refresh(new_profile)
    log.info(
        "caller_profile_upgraded", from_profile_id=str(profile_id),
        to_profile_id=str(new_profile.id),
        from_version=profile.version, to_version=template.current_version,
        carried=len(carried), dropped=len(profile.mapping) - len(carried),
    )
    return _redirect(
        f"/ui/profiles/{new_profile.id}",
        flash=(
            f"Upgraded to v{template.current_version}. "
            f"Carried {len(carried)} mapping(s); review new/removed fields."
        ),
        flash_kind="success",
    )


async def _resolve_default_frozen(
    session: AsyncSession,
    template_id: uuid.UUID,
    version: int,
    subversion: int | None = None,
) -> TemplateVersion | None:
    """Frozen default-locale row for (version, subversion).

    When `subversion` is None, returns the highest frozen sub for the
    version — the natural "latest of v1" resolution. Callers that need a
    specific historical sub (rollback, diff) pass it explicitly.
    """
    q = select(TemplateVersion).where(
        TemplateVersion.template_id == template_id,
        TemplateVersion.version == version,
        TemplateVersion.locale == "default",
        TemplateVersion.is_frozen.is_(True),
    )
    if subversion is not None:
        q = q.where(TemplateVersion.subversion == subversion)
    else:
        q = q.order_by(TemplateVersion.subversion.desc())
    q = q.limit(1)
    result = await session.execute(q)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Inbox agent — human-in-the-loop review of email-ingested templates
# ---------------------------------------------------------------------------

_INBOX_REVIEW_FILTERS = ("pending_review", "auto_approved", "approved", "rejected")
_INBOX_STATUS_FILTERS = ("pending", "processing", "succeeded", "failed", "skipped")


_INBOX_PAGE_SIZE = 20

_INBOX_SORT_COLUMNS = {
    "case": InboxEvent.case_number,
    "received": InboxEvent.received_at,
    "from": InboxEvent.from_addr,
    "subject": InboxEvent.subject,
    "review": InboxEvent.review_state,
}


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tenant: str | None = None,
    review: str | None = None,
    status: str | None = None,
    page: int = 1,
    sort: str | None = None,
    dir: str | None = None,
) -> HTMLResponse:
    """Every email the agent has received, newest first.

    Default view is unfiltered so operators can see the full flow — ingested,
    skipped (bad sender, no attachment), replies to prior notifications, and
    everything else. Two independent filters:
      - `review` = review-state (pending_review / auto_approved / approved / rejected)
      - `status` = ingest status (pending / processing / succeeded / failed / skipped)
    """
    from docgen.store.models import Tenant

    tenants_stmt = select(Tenant).order_by(Tenant.name.asc())
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenants_stmt = tenants_stmt.where(Tenant.id == ident.tenant_id)
    tenants_res = await session.execute(tenants_stmt)
    all_tenants = tenants_res.scalars().all()

    tenant_uuid = _resolve_list_tenant_filter(request, tenant)
    review_filter = review if review in _INBOX_REVIEW_FILTERS else None
    status_filter = status if status in _INBOX_STATUS_FILTERS else None

    filters: list[Any] = []
    if tenant_uuid is not None:
        filters.append(InboxEvent.tenant_id == tenant_uuid)
    if review_filter is not None:
        filters.append(InboxEvent.review_state == review_filter)
    if status_filter is not None:
        filters.append(InboxEvent.status == status_filter)

    count_stmt = select(func.count()).select_from(InboxEvent)
    for f in filters:
        count_stmt = count_stmt.where(f)
    total_count = int((await session.execute(count_stmt)).scalar_one() or 0)

    total_pages = max(1, math.ceil(total_count / _INBOX_PAGE_SIZE))
    current_page = max(1, min(int(page or 1), total_pages))
    offset = (current_page - 1) * _INBOX_PAGE_SIZE

    sort_key = sort if sort in _INBOX_SORT_COLUMNS else "received"
    sort_dir = "asc" if (dir or "").lower() == "asc" else "desc"
    sort_col = _INBOX_SORT_COLUMNS[sort_key]
    order_primary = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    q = (
        select(
            InboxEvent.id,
            InboxEvent.case_number,
            InboxEvent.from_addr,
            InboxEvent.subject,
            InboxEvent.received_at,
            InboxEvent.status,
            InboxEvent.review_state,
            InboxEvent.score,
            InboxEvent.error,
            InboxEvent.template_id,
            InboxEvent.template_version_id,
            InboxEvent.attachment_name,
            InboxEvent.reply_message_id,
            InboxEvent.tenant_id,
            InboxEvent.callback_url,
            InboxEvent.callback_status,
            InboxEvent.callback_attempts,
            InboxEvent.callback_delivered_at,
            InboxEvent.callback_last_error,
            Template.name.label("template_name"),
            Tenant.name.label("tenant_name"),
        )
        .join(Tenant, Tenant.id == InboxEvent.tenant_id, isouter=True)
        .join(Template, Template.id == InboxEvent.template_id, isouter=True)
        .order_by(order_primary, InboxEvent.id.desc())
        .limit(_INBOX_PAGE_SIZE)
        .offset(offset)
    )
    for f in filters:
        q = q.where(f)

    rows = [dict(r._mapping) for r in (await session.execute(q)).all()]

    return templates.TemplateResponse(
        request,
        "inbox_list.html",
        await _base_ctx(
            request,
            session,
            active="inbox",
            all_tenants=all_tenants,
            selected_tenant=tenant_uuid,
            events=rows,
            review_filter=review_filter,
            status_filter=status_filter,
            current_page=current_page,
            total_pages=total_pages,
            total_count=total_count,
            page_size=_INBOX_PAGE_SIZE,
            sort_key=sort_key,
            sort_dir=sort_dir,
        ),
    )


@router.get("/inbox/humanloop", response_class=HTMLResponse)
async def inbox_humanloop(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tenant: str | None = None,
) -> HTMLResponse:
    """HumanLoop — the focused worklist of inbox events waiting for a human decision.

    Every pipeline outcome routes here (auto-approval was removed so no reply
    ever leaves the system without a reviewer clicking Approve). Legacy
    template-flow items with `review_state == 'pending_review'` also appear.
    Ordered oldest-first so aging cases surface at the top.
    """
    from docgen.store.models import Tenant

    tenants_stmt = select(Tenant).order_by(Tenant.name.asc())
    ident = _identity(request)
    if ident is not None and ident.role == "tenant_admin" and ident.tenant_id is not None:
        tenants_stmt = tenants_stmt.where(Tenant.id == ident.tenant_id)
    tenants_res = await session.execute(tenants_stmt)
    all_tenants = tenants_res.scalars().all()

    tenant_uuid = _resolve_list_tenant_filter(request, tenant)

    q = (
        select(
            InboxEvent.id,
            InboxEvent.case_number,
            InboxEvent.from_addr,
            InboxEvent.subject,
            InboxEvent.received_at,
            InboxEvent.status,
            InboxEvent.review_state,
            InboxEvent.score,
            InboxEvent.template_id,
            InboxEvent.attachment_name,
            InboxEvent.pap_decision,
            InboxEvent.reply_message_id,
            InboxEvent.tenant_id,
            Template.name.label("template_name"),
            Tenant.name.label("tenant_name"),
        )
        .join(Tenant, Tenant.id == InboxEvent.tenant_id, isouter=True)
        .join(Template, Template.id == InboxEvent.template_id, isouter=True)
        .where(
            InboxEvent.review_state == "pending_review",
            InboxEvent.reply_message_id.is_(None),
        )
        .order_by(InboxEvent.received_at.asc(), InboxEvent.id.asc())
        .limit(200)
    )
    if tenant_uuid is not None:
        q = q.where(InboxEvent.tenant_id == tenant_uuid)

    rows = [dict(r._mapping) for r in (await session.execute(q)).all()]

    return templates.TemplateResponse(
        request,
        "inbox_humanloop.html",
        await _base_ctx(
            request,
            session,
            active="humanloop",
            all_tenants=all_tenants,
            selected_tenant=tenant_uuid,
            events=rows,
        ),
    )


async def _load_inbox_event(
    session: AsyncSession, event_id: uuid.UUID
) -> InboxEvent:
    ev = await session.get(InboxEvent, event_id)
    if ev is None:
        raise HTTPException(404, "Not found")
    return ev


async def _latest_assembly_for_event(session: AsyncSession, event_id: uuid.UUID):
    """Return the most recent doc-package assembly for this inbox event, if
    any. Used by the pipeline detail page + verdict JSON download so the
    per-item ledger is visible to reviewers and downstream systems."""
    from docgen.store.models import DocPackageAssembly

    stmt = (
        select(DocPackageAssembly)
        .where(DocPackageAssembly.inbox_event_id == event_id)
        .order_by(DocPackageAssembly.assembled_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _append_review_history(
    event: InboxEvent, *, action: str, actor: str, note: str
) -> None:
    """Append an audit entry to the event's review_history JSONB list.

    JSONB list mutation needs a fresh list assignment to trip the ORM's dirty
    check — .append() on the loaded list is invisible to SQLAlchemy without
    a MutableList wrapper.
    """
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "action": action,
        "actor": actor,
        "note": note,
    }
    event.review_history = [*(event.review_history or []), entry]


async def _fire_verdict_webhook(
    event_id: uuid.UUID, *, reviewer_action: str, reviewer_actor: str
) -> str | None:
    """Deliver the reviewer's final verdict to the caller's callback_url.

    Runs sync in a thread pool because the sender uses ``sync_session()``
    + ``httpx.Client``. Returns a short flash fragment describing the
    delivery outcome so the reviewer sees whether the caller got the
    webhook — empty string when the case has no callback_url configured.
    """
    from fastapi.concurrency import run_in_threadpool

    from pap_pipeline.webhooks import deliver_verdict_webhook

    settings = get_settings()

    def _run() -> tuple[bool, str | None]:
        return deliver_verdict_webhook(
            event_id,
            settings,
            reviewer_action=reviewer_action,
            reviewer_actor=reviewer_actor,
        )

    try:
        delivered, error = await run_in_threadpool(_run)
    except Exception as e:  # pragma: no cover — defensive; sender catches its own
        return f"Webhook delivery crashed: {type(e).__name__}: {e}"

    if delivered:
        return "Verdict webhook delivered to caller."
    if error:
        return f"Webhook delivery failed: {error}"
    return None


@router.get("/inbox/{event_id}", response_class=HTMLResponse)
async def inbox_detail(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Common ingest-event view.

    Renders message metadata (from/to/cc/subject/body) and links out to the
    typed detail page — template or pipeline — depending on which flow
    processed this event. Keeps the ingest object generic so reviewers see
    the same shell regardless of which processor handled the mail.
    """
    ev = await _load_inbox_event(session, event_id)
    # Ingest events with no tenant (unresolved sender) are visible to
    # super_admin only — enforce_scope on a None tenant_id is a no-op,
    # so gate here.
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    tenant = None
    if ev.tenant_id is not None:
        from docgen.store.models import Tenant
        tenant = await session.get(Tenant, ev.tenant_id)

    return templates.TemplateResponse(
        request,
        "inbox_detail.html",
        await _base_ctx(
            request,
            session,
            active="inbox",
            event=ev,
            tenant=tenant,
            cc_addrs=list(ev.cc_addrs or []),
        ),
    )


@router.get("/inbox/{event_id}/template", response_class=HTMLResponse)
async def inbox_template_detail(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Template-flow detail page — trials, models, publish actions.

    Split out of the common ingest page so the two ingest types (template
    freeze + PAP pipeline) share the same drill-down shape: common event →
    typed detail link. Approve/reject buttons only appear here because
    publishing a template version is template-flow-specific.
    """
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    if ev.template_id is None and ev.template_version_id is None:
        raise HTTPException(404, "No template artifacts for this event")

    template = None
    tv = None
    if ev.template_id is not None:
        template = await session.get(Template, ev.template_id)
    if ev.template_version_id is not None:
        tv = await session.get(TemplateVersion, ev.template_version_id)

    tenant = None
    if ev.tenant_id is not None:
        from docgen.store.models import Tenant
        tenant = await session.get(Tenant, ev.tenant_id)

    from docgen.auth import sign_inbox_test_token

    trials_result = await session.execute(
        select(InboxTestResult)
        .where(InboxTestResult.inbox_event_id == ev.id)
        .order_by(InboxTestResult.ordinal.asc())
    )
    trials = list(trials_result.scalars().all())
    trial_download_tokens = {
        str(t.id): sign_inbox_test_token(str(t.id)) for t in trials
    }

    models_used = _resolve_inbox_models(tenant=tenant, tv=tv)

    return templates.TemplateResponse(
        request,
        "inbox_template_detail.html",
        await _base_ctx(
            request,
            session,
            active="inbox",
            event=ev,
            template=template,
            template_version=tv,
            tenant=tenant,
            trials=trials,
            trial_download_tokens=trial_download_tokens,
            models_used=models_used,
        ),
    )


def _resolve_inbox_models(*, tenant, tv) -> list[dict[str, str]]:
    """Return the stages that ran for this inbox event, in pipeline order.

    Reflects the same precedence resolve_tenant / test_planner / test_verdict
    use at runtime: tenant.ai_provider_config wins, then deployment settings,
    then the provider's built-in defaults. PDF-specific rows only appear for
    PDF templates. AcroForm labeling is deterministic (widget rects + page
    text-span geometry — no model call), so its row shows provider="local"
    with no model rather than a misleading vision-model tag.
    """
    settings = get_settings()
    cfg = (tenant.ai_provider_config if tenant else None) or {}
    provider = cfg.get("provider") or settings.ai_provider or "stub"

    from docgen.providers.nebius import DEFAULT_MODEL as _NEBIUS_DEFAULT

    default_design_model = (
        _NEBIUS_DEFAULT if provider == "nebius" else (settings.ai_model or "—")
    )
    design_model = cfg.get("model") or settings.ai_model or default_design_model
    verdict_model = (
        cfg.get("verdict_model")
        or settings.ai_verdict_model
        or design_model
    )

    is_pdf = tv is not None and tv.pdf_flavor is not None
    verdict_row = {
        "stage": "Test verdict scorer",
        "model": verdict_model,
        "provider": provider,
        "purpose": "Reviews each rendered trial for missing values and slot swaps.",
    }

    rows: list[dict[str, str]] = [
        {
            "stage": "Mapping proposal / test planner",
            "model": design_model,
            "provider": provider,
            "purpose": "Extracts field candidates and generates the 5 test payloads.",
        },
        verdict_row,
    ]

    if is_pdf:
        if tv.pdf_flavor == "acroform":
            rows.insert(0, {
                "stage": "PDF field labeler",
                "model": "(deterministic geometry — no model call)",
                "provider": "local",
                "purpose": "Matches each AcroForm widget to its printed caption using widget rects + page text-span bboxes.",  # noqa: E501
            })
        else:
            rows.insert(0, {
                "stage": "PDF field labeler",
                "model": settings.pdf_labeler_model,
                "provider": provider,
                "purpose": "Captions each detected placement using pdfplumber-extracted OCR text and rect coordinates — no page image sent to the model.",  # noqa: E501
            })
            rows.insert(0, {
                "stage": "PDF field detector (AI Draw)",
                "model": "(deterministic geometry — no model call)",
                "provider": "local",
                "purpose": "Locates form-field regions on flat PDFs by scanning pdfplumber drawing primitives (lines, rects, curves).",  # noqa: E501
            })

    return rows


@router.get("/inbox/{event_id}/pipeline", response_class=HTMLResponse)
async def inbox_pipeline_detail(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Pipeline-flow detail page. Renders extract/retrieve/reason evidence.

    The template-flow detail page (`/inbox/{event_id}`) shows message metadata
    + template + review-lifecycle. This page shows the pipeline's own
    artifacts — extracted Intake, matched rules, missing fields, rationale —
    which the template-flow page has no slot for.
    """
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    if ev.pap_decision is None:
        raise HTTPException(404, "No pipeline artifacts for this event")

    tenant = None
    if ev.tenant_id is not None:
        from docgen.store.models import Tenant
        tenant = await session.get(Tenant, ev.tenant_id)

    # Surface the MDM connector used for this case (ADR-0004). Today we
    # derive it from the tenant's current mdm_config — that's honest as
    # long as the config hasn't been retuned since the case was
    # processed. Persisting the exact slice per-event is a follow-up so
    # audit still reflects config-at-decision-time.
    mdm_provider = ""
    mdm_base_url = ""
    if tenant is not None:
        mdm_cfg = dict(tenant.mdm_config or {})
        mdm_provider = str(mdm_cfg.get("provider") or "").strip().lower() or "simulated"
        mdm_base_url = str(mdm_cfg.get("base_url") or "")
    mdm_provider_class = _MDM_PROVIDER_CLASS.get(mdm_provider, "http")
    mdm_admin_url = (
        f"/ui/admin/tenants/{tenant.id}/mdm" if tenant is not None else None
    )

    # Hydrate rule text so citations can be shown as rule statements, not
    # opaque IDs. The loader reads YAML on every call — cheap at demo scale
    # and keeps the page in sync with any rule edits without a restart.
    from docgen.inbox.agent import _pap_program_from_subject
    from pap_pipeline.rules import load_rules
    from pap_pipeline.schema_groups import bucket_slots
    from pap_pipeline.verdict_json import build_verdict

    rules_by_id = {r.id: r for r in load_rules()}

    extraction = ev.pap_extraction or ev.pap_intake or {}
    # Group slot names into reviewer-facing sections at render time.
    # Slice 2 will consult persisted groups on the Program row when present.
    extraction_groups = bucket_slots(extraction.keys())

    # Re-derive program from subject at read time — the pipeline doesn't
    # persist the resolved slug on the event row, but the subject tag is
    # the canonical source it was resolved from.
    _, program_slug = _pap_program_from_subject(ev.subject)
    assembly = await _latest_assembly_for_event(session, ev.id)
    verdict = build_verdict(ev, tenant=tenant, program=program_slug, assembly=assembly)
    import json as _json
    verdict_pretty = _json.dumps(verdict, indent=2, default=str)

    return templates.TemplateResponse(
        request,
        "inbox_pipeline_detail.html",
        await _base_ctx(
            request,
            session,
            active="inbox",
            event=ev,
            tenant=tenant,
            intake=ev.pap_intake or {},
            # Prefer the raw extraction dict when the pipeline persisted it —
            # includes program-specific slots that aren't on Intake. Falls
            # back to pap_intake for pre-migration events.
            extraction=extraction,
            extraction_groups=extraction_groups,
            decision=ev.pap_decision or {},
            rules_by_id=rules_by_id,
            verdict_json=verdict_pretty,
            verdict_download_url=f"/ui/inbox/{ev.id}/verdict.json",
            mdm_provider=mdm_provider,
            mdm_provider_class=mdm_provider_class,
            mdm_base_url=mdm_base_url,
            mdm_admin_url=mdm_admin_url,
        ),
    )


@router.get("/inbox/{event_id}/verdict.json", response_class=None)
async def inbox_verdict_json(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Return the downstream JSON verdict for this event.

    Same envelope the pipeline detail page renders — assembled on demand
    from the persisted decision/intake/extraction so audit and downstream
    consumers see the same object. Admin-session gated like the rest of
    `/ui/*`; downstream systems that need programmatic access should hit
    this behind their own API-key proxy or be given a signed variant.
    """
    from docgen.inbox.agent import _pap_program_from_subject
    from pap_pipeline.verdict_json import build_verdict

    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    if ev.pap_decision is None:
        raise HTTPException(404, "No pipeline verdict for this event")

    tenant = None
    if ev.tenant_id is not None:
        from docgen.store.models import Tenant
        tenant = await session.get(Tenant, ev.tenant_id)

    _, program_slug = _pap_program_from_subject(ev.subject)
    assembly = await _latest_assembly_for_event(session, ev.id)
    verdict = build_verdict(ev, tenant=tenant, program=program_slug, assembly=assembly)
    filename = f"verdict-{ev.case_id}.json"
    return JSONResponse(
        content=verdict,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/inbox/{event_id}/audit", response_class=HTMLResponse)
async def inbox_audit(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Full audit trail for a case: every action taken by AI and humans.

    Reads the `review_history` JSONB list on InboxEvent — entries are appended
    at pipeline stages (AI actors) and at review decisions (human actors), so
    this is the one place a reviewer can see the case's timeline end-to-end.
    """
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    tenant = None
    if ev.tenant_id is not None:
        from docgen.store.models import Tenant
        tenant = await session.get(Tenant, ev.tenant_id)

    return templates.TemplateResponse(
        request,
        "inbox_audit.html",
        await _base_ctx(
            request,
            session,
            active="inbox",
            event=ev,
            tenant=tenant,
            history=list(ev.review_history or []),
        ),
    )


@router.get("/inbox/{event_id}/reply-attachment", response_class=None)
async def inbox_reply_attachment(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Stream the exact PDF the pipeline attached to the outbound reply.

    Behind the admin session (like the rest of `/ui/*`); no signed token
    needed because reviewers are already authenticated. Returns 404 if the
    event has no attachment key (template flow, pre-migration event, or a
    render/upload failure logged upstream).
    """
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    if not ev.reply_attachment_blob_key:
        raise HTTPException(404, "No reply attachment recorded for this event")

    settings = get_settings()
    from docgen.store.blob import get_object

    try:
        body = get_object(settings.s3_bucket_documents, ev.reply_attachment_blob_key)
    except Exception as e:
        raise HTTPException(500, f"attachment fetch failed: {type(e).__name__}") from e

    # `outcome` is a Literal at pipeline time, but JSONB is a raw round-trip —
    # any manual DB edit or future schema drift could put an arbitrary string
    # here. Whitelist before interpolating into Content-Disposition so a
    # newline / quote in the value can't split the header.
    raw_outcome = (ev.pap_decision or {}).get("outcome")
    outcome = raw_outcome if raw_outcome in {"approve", "deny", "missing_info"} else "reply"
    filename = f"alderwood_{outcome}_{str(ev.id).replace('-', '')[:8].upper()}.pdf"
    from fastapi.responses import Response

    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


# Content types the poller can produce (see InboundAttachment.kind). Anything
# outside the allowlist is served as octet-stream so a manifest row with a
# manually-edited content_type can't inject a hostile MIME the browser will
# render (SVG script, XHTML, etc.).
_INBOUND_ALLOWED_CT = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.get("/inbox/{event_id}/inbound-attachment/{index}", response_class=None)
async def inbox_inbound_attachment(
    event_id: uuid.UUID,
    index: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Stream one raw inbound attachment for reviewer preview.

    Index is the position in the manifest (0-based). Admin-session gated;
    no signed URL because the reviewer is already authenticated. PDFs get
    Content-Disposition: inline so the browser previews them in a tab;
    docx falls back to attachment (browsers can't preview it) so the file
    downloads instead of triggering an unhelpful download-with-random-name.
    """
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)

    manifest = ev.inbound_attachments or []
    if index < 0 or index >= len(manifest):
        raise HTTPException(404, "No inbound attachment at that index")

    entry = manifest[index]
    blob_key = entry.get("blob_key")
    if not blob_key:
        raise HTTPException(404, "Attachment upload did not succeed for this file")

    settings = get_settings()
    try:
        body = get_object(settings.s3_bucket_documents, blob_key)
    except Exception as e:
        raise HTTPException(500, f"attachment fetch failed: {type(e).__name__}") from e

    raw_ct = entry.get("content_type") or "application/octet-stream"
    content_type = raw_ct if raw_ct in _INBOUND_ALLOWED_CT else "application/octet-stream"

    # Guard the filename against header-splitting the same way the reply
    # attachment route does — any newline/quote in the stored value would
    # split Content-Disposition. Strip to a safe ASCII slug.
    raw_name = entry.get("filename") or f"attachment-{index}"
    safe_name = "".join(c for c in raw_name if c.isalnum() or c in "._- ").strip() or f"attachment-{index}"

    disposition = "inline" if content_type == "application/pdf" else "attachment"

    from fastapi.responses import Response

    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/inbox/{event_id}/approve", response_model=None)
async def inbox_approve(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)
    detail_url = f"/ui/inbox/{event_id}/template"
    if ev.review_state in ("approved", "auto_approved"):
        return _redirect(detail_url, flash="Already approved.", flash_kind="info")
    if ev.template_id is None or ev.template_version_id is None:
        return _redirect(
            detail_url,
            flash="Cannot approve — ingest never produced a template version.",
            flash_kind="error",
        )
    tv = await session.get(TemplateVersion, ev.template_version_id)
    if tv is None:
        return _redirect(
            detail_url,
            flash="Template version missing — cannot publish.",
            flash_kind="error",
        )
    actor = getattr(request.state, "admin_user", None) or "inbox-review"
    try:
        await publish_version(
            session,
            template_id=ev.template_id,
            tenant_id=ev.tenant_id,
            version=tv.version,
            subversion=tv.subversion,
            actor=actor,
        )
    except ValueError as e:
        return _redirect(
            detail_url,
            flash=f"Publish failed: {e}",
            flash_kind="error",
        )
    ev.review_state = "approved"
    _append_review_history(
        ev,
        action="approved",
        actor=str(actor),
        note=f"Published template v{tv.version}.{tv.subversion}.",
    )
    await session.commit()
    return _redirect(
        detail_url,
        flash=f"Approved and published v{tv.version}.{tv.subversion}.",
        flash_kind="success",
    )


@router.post("/inbox/{event_id}/reject", response_model=None)
async def inbox_reject(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)
    detail_url = f"/ui/inbox/{event_id}/template"
    if ev.review_state == "rejected":
        return _redirect(detail_url, flash="Already rejected.", flash_kind="info")
    if ev.review_state in ("approved", "auto_approved"):
        return _redirect(
            detail_url,
            flash="Cannot reject — already published. Deactivate the template instead.",
            flash_kind="error",
        )
    ev.review_state = "rejected"
    actor = getattr(request.state, "admin_user", None) or "inbox-review"
    _append_review_history(
        ev,
        action="rejected",
        actor=str(actor),
        note="Template version left as draft.",
    )
    await session.commit()
    return _redirect(
        detail_url,
        flash="Marked as rejected. The template version remains as a draft.",
        flash_kind="success",
    )


@router.post("/inbox/{event_id}/pipeline/approve", response_model=None)
async def inbox_pipeline_approve(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Human-in-the-loop send. Pipeline outcomes never leave the building
    until a reviewer OKs them here — the PAP handler stops at pending_review
    with the reply PDF pre-rendered, and this endpoint performs the actual
    SMTP send + records the reply Message-ID.
    """
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)
    detail_url = f"/ui/inbox/{event_id}/pipeline"
    if ev.pap_decision is None:
        return _redirect(
            detail_url,
            flash="No pipeline decision to approve on this event.",
            flash_kind="error",
        )
    if ev.review_state in ("approved", "auto_approved"):
        return _redirect(
            detail_url, flash="Already approved and sent.", flash_kind="info"
        )
    if ev.review_state == "rejected":
        return _redirect(
            detail_url,
            flash="Already rejected — reopen by manual DB action if needed.",
            flash_kind="error",
        )
    # Legacy pipeline events (pre human-review gate) may still carry
    # review_state='pending_review' because that was the missing-info verdict
    # slot, but the auto-send code already fired their reply. reply_message_id
    # is the ground truth for "did an email go out"; refuse the send so the
    # applicant doesn't get a duplicate.
    if ev.reply_message_id:
        return _redirect(
            detail_url,
            flash="Reply already sent (legacy auto-send) — refusing to duplicate.",
            flash_kind="error",
        )

    # REST and MCP intakes already returned the verdict inline to the caller
    # — there is no downstream applicant email to send. The approve action
    # for these channels just records the reviewer's OK and closes the case.
    from_addr = ev.from_addr or ""
    is_m2m_channel = from_addr.startswith(("rest://", "mcp://"))

    if is_m2m_channel:
        actor = str(getattr(request.state, "admin_user", None) or "inbox-review")
        ev.review_state = "approved"
        _append_review_history(
            ev,
            action="approved",
            actor=actor,
            note=(
                "Reviewer approved the pipeline verdict. No email sent — "
                "verdict was returned inline to the API/MCP caller at intake time."
            ),
        )
        await session.commit()
        webhook_flash = await _fire_verdict_webhook(
            ev.id, reviewer_action="approved", reviewer_actor=actor
        )
        flash_msg = "Approved. No email sent — verdict was returned to the API caller at intake."
        if webhook_flash:
            flash_msg = flash_msg + " " + webhook_flash
        return _redirect(detail_url, flash=flash_msg, flash_kind="success")

    from docgen.inbox.agent import get_pap_reply_sender
    from docgen.inbox.mailer import MailerError

    sender = get_pap_reply_sender()
    if sender is None:
        return _redirect(
            detail_url,
            flash="PAP plugin not loaded — cannot send reply. Check DOCGEN_PLUGINS.",
            flash_kind="error",
        )

    # The sender is sync (smtplib + boto3 blob fetch). Sync-session bridge
    # mirrors the pattern used elsewhere in this file — we grab a sync
    # Session bound to the same event row so the sender can commit reply
    # state without contending with the async request session.
    from fastapi.concurrency import run_in_threadpool

    from docgen.store.db import sync_session

    settings = get_settings()
    actor = str(getattr(request.state, "admin_user", None) or "inbox-review")

    def _do_send() -> str | None:
        with sync_session() as s:
            fresh = s.get(InboxEvent, event_id)
            if fresh is None:
                raise MailerError("event vanished between load and send")
            # Record the human decision BEFORE calling the sender so the audit
            # timeline reads chronologically: reviewer approves → system sends.
            # The sender appends its own `reply_sent` entry on success.
            _append_review_history(
                fresh,
                action="approved",
                actor=actor,
                note="Reviewer approved the pipeline verdict — releasing reply to applicant.",
            )
            s.commit()
            reply_id = sender(s, fresh, settings)
            if reply_id:
                fresh.reply_message_id = reply_id
                fresh.review_state = "approved"
                s.commit()
            return reply_id

    try:
        reply_id = await run_in_threadpool(_do_send)
    except MailerError as e:
        return _redirect(detail_url, flash=f"Send failed: {e}", flash_kind="error")
    except Exception as e:
        return _redirect(
            detail_url,
            flash=f"Send failed: {type(e).__name__}: {e}",
            flash_kind="error",
        )

    if not reply_id:
        return _redirect(
            detail_url,
            flash="Sender returned no Message-ID — check SMTP logs.",
            flash_kind="error",
        )
    return _redirect(
        detail_url,
        flash="Approved and reply sent to applicant.",
        flash_kind="success",
    )


@router.post("/inbox/{event_id}/pipeline/reject", response_model=None)
async def inbox_pipeline_reject(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Reviewer discards the pipeline verdict — no reply is sent."""
    ev = await _load_inbox_event(session, event_id)
    if ev.tenant_id is None:
        _require_super_admin(request)
    else:
        _enforce_scope(request, ev.tenant_id)
    detail_url = f"/ui/inbox/{event_id}/pipeline"
    if ev.pap_decision is None:
        return _redirect(
            detail_url,
            flash="No pipeline decision to reject on this event.",
            flash_kind="error",
        )
    if ev.review_state == "rejected":
        return _redirect(detail_url, flash="Already rejected.", flash_kind="info")
    if ev.review_state in ("approved", "auto_approved"):
        return _redirect(
            detail_url,
            flash="Cannot reject — reply already sent to the applicant.",
            flash_kind="error",
        )
    ev.review_state = "rejected"
    actor = str(getattr(request.state, "admin_user", None) or "inbox-review")
    _append_review_history(
        ev,
        action="rejected",
        actor=actor,
        note="Pipeline verdict discarded — no reply sent.",
    )
    await session.commit()

    # M2M reject: if the caller registered a callback_url the reviewer's
    # denial is the authoritative verdict for the case, and the caller
    # needs to hear about it. Fire the webhook so downstream systems flip
    # from "queued/draft-approve" to the final rejected state.
    from_addr = ev.from_addr or ""
    flash_msg = "Rejected. No reply was sent."
    if from_addr.startswith(("rest://", "mcp://")):
        webhook_flash = await _fire_verdict_webhook(
            ev.id, reviewer_action="rejected", reviewer_actor=actor
        )
        if webhook_flash:
            flash_msg = flash_msg + " " + webhook_flash
    return _redirect(detail_url, flash=flash_msg, flash_kind="success")


# ─── Programs (PAP pipeline schema per tenant) ────────────────────────────────
# A Program row is (tenant, slug) + an `extraction_schema` JSONB list of slot
# dicts. The Program's schema replaces the hardcoded PAP Intake shape at
# extract time; admins bind an enrollment template as the schema seed so the
# vision model's slot list matches the form the applicant is filling in.

_SLOT_TYPES = ("string", "date", "integer", "number", "boolean", "enum")


def _tv_fields_to_slots(schema_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Convert a TemplateVersion.schema field manifest into program slots.

    The template's field list is the closest thing we have to a first-class
    schema for what the enrollment form captures — copying it as the initial
    slot set gets the admin from empty state to editing text descriptions,
    rather than typing every field name from scratch. Nested (list/object)
    fields are flattened away for MVP; the extract stage can't populate them
    without a dedicated prompt shape.
    """
    fields = (schema_json or {}).get("fields") or []
    slots: list[dict[str, Any]] = []
    for f in fields:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        t = f.get("type") or "string"
        # Collapse container types — the VLM extraction prompt only knows how
        # to fill scalar slots. Admin can add nested slots by hand later.
        if t in ("list", "object"):
            continue
        slots.append({
            "name": name,
            "type": t if t in _SLOT_TYPES else "string",
            "description": (f.get("description") or "").strip(),
            "source_field": name,
            "required": bool(f.get("required")),
            "enum_values": None,
        })
    return slots


def _parse_program_schema_form(form: Any) -> list[dict[str, Any]]:
    """Deserialize the slot table posted by the program-detail form.

    The form ships one row per slot with parallel `slot_name[]` /
    `slot_type[]` / etc. arrays — the classic tabular-form pattern. Missing
    or empty names are dropped so the "delete a slot" UX is just "clear the
    name field and save."
    """
    names = form.getlist("slot_name")
    types = form.getlist("slot_type")
    descriptions = form.getlist("slot_description")
    source_fields = form.getlist("slot_source_field")
    required_flags = set(form.getlist("slot_required"))
    enum_values_raw = form.getlist("slot_enum_values")

    slots: list[dict[str, Any]] = []
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        t = (types[i] if i < len(types) else "string").strip()
        if t not in _SLOT_TYPES:
            t = "string"
        desc = (descriptions[i] if i < len(descriptions) else "").strip()
        source = (source_fields[i] if i < len(source_fields) else "").strip() or None
        # Row index (as string) is present in the required set when that
        # row's checkbox was ticked — the checkbox `value` is the row index.
        required = str(i) in required_flags
        enum_values: list[str] | None = None
        if t == "enum":
            raw_ev = enum_values_raw[i] if i < len(enum_values_raw) else ""
            enum_values = [v.strip() for v in (raw_ev or "").split(",") if v.strip()] or None
        slots.append({
            "name": name,
            "type": t,
            "description": desc,
            "source_field": source,
            "required": required,
            "enum_values": enum_values,
        })
    return slots


async def _load_program(session: AsyncSession, program_id: uuid.UUID) -> Program:
    row = await session.get(Program, program_id)
    if row is None:
        raise HTTPException(404, "Program not found")
    return row


@router.get("/programs", response_class=HTMLResponse)
async def programs_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    """Programs list — scoped by the sidebar workspace picker.

    Super-admin with no workspace selected sees every tenant's programs;
    tenant-admin and picker-scoped super-admin see only the current
    tenant's programs. Same rule as the Templates/Renders lists.
    """
    scope = _forced_tenant_scope(request)
    stmt = (
        select(Program, Tenant.name)
        .join(Tenant, Tenant.id == Program.tenant_id)
        .order_by(Tenant.name.asc(), Program.slug.asc())
    )
    if scope is not None:
        stmt = stmt.where(Program.tenant_id == scope)
    rows = (await session.execute(stmt)).all()
    programs = [
        {
            "id": p.id,
            "tenant_id": p.tenant_id,
            "tenant_name": tenant_name,
            "slug": p.slug,
            "name": p.name,
            "is_active": p.is_active,
            "slot_count": len(p.extraction_schema or []),
            "enrollment_template_version_id": p.enrollment_template_version_id,
            "updated_at": p.updated_at,
        }
        for (p, tenant_name) in rows
    ]
    tenants_res = await session.execute(select(Tenant).order_by(Tenant.name.asc()))
    tenants_all = tenants_res.scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_programs.html",
        await _base_ctx(
            request, session, active="programs",
            programs=programs, tenants_all=tenants_all,
            flash=flash, flash_kind=flash_kind,
        ),
    )


@router.post("/programs")
async def programs_create(
    request: Request,
    tenant_id: uuid.UUID = Form(...),
    slug: str = Form(...),
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create a Program row — starts with an empty extraction schema. The
    admin binds a template and copies slots on the detail page."""
    _enforce_scope(request, tenant_id)
    slug_clean = slug.strip().lower()
    name_clean = name.strip()
    if not slug_clean or not name_clean:
        return _redirect("/ui/programs", flash="Slug and name are required.", flash_kind="error")
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    # Create with NULL model columns so the 3-level resolver
    # (program → tenant → settings) picks the effective model at runtime.
    # Hard-coding here would freeze the defaults into the row and defeat
    # env-var or tenant-level rotation.
    prog = Program(
        tenant_id=tenant_id, slug=slug_clean, name=name_clean,
        extraction_schema=[], is_active=True,
    )
    session.add(prog)
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        return _redirect("/ui/programs", flash=f"Create failed: {e}", flash_kind="error")
    log.info("program_created", tenant_id=str(tenant_id), slug=slug_clean)
    return _redirect(f"/ui/programs/{prog.id}", flash=f"Created program '{name_clean}'.", flash_kind="success")  # noqa: E501


@router.get("/programs/{program_id}", response_class=HTMLResponse)
async def program_detail(
    program_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    """Program detail — edit slots, pick an enrollment template."""
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    tenant = await session.get(Tenant, prog.tenant_id)

    # Candidate enrollment templates: tenant's frozen versions. Kept small
    # deliberately — a Program's schema is bound to one blank form, so the
    # picker doesn't need every draft in the tenant.
    tv_stmt = (
        select(TemplateVersion, Template.name)
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(Template.tenant_id == prog.tenant_id, TemplateVersion.is_frozen.is_(True))
        .order_by(Template.name.asc(), TemplateVersion.version.desc(), TemplateVersion.subversion.desc())
    )
    template_versions = [
        {
            "id": tv.id,
            "template_id": tv.template_id,
            "template_name": name,
            "version": tv.version,
            "subversion": tv.subversion,
            "locale": tv.locale,
            "field_count": len((tv.schema or {}).get("fields") or []),
        }
        for (tv, name) in (await session.execute(tv_stmt)).all()
    ]

    # Pull the currently-bound template's field manifest so the UI can show
    # the "source form fields" list next to the editable slot table.
    bound_fields: list[dict[str, Any]] = []
    if prog.enrollment_template_version_id:
        bound_tv = await session.get(TemplateVersion, prog.enrollment_template_version_id)
        if bound_tv is not None:
            bound_fields = list((bound_tv.schema or {}).get("fields") or [])

    # Attached rules: join to the tenant's rule library through the join
    # table. The reviewer sees per-program position + is_active from the
    # link and the library's rule text/kind/fields. Rules the retriever
    # actually receives are the ones where both the link and the library
    # row are active.
    linked_stmt = (
        select(Rule, ProgramRuleLink)
        .join(ProgramRuleLink, ProgramRuleLink.rule_id == Rule.id)
        .where(ProgramRuleLink.program_id == prog.id)
        .order_by(ProgramRuleLink.position.asc(), Rule.rule_id.asc())
    )
    linked_rows = (await session.execute(linked_stmt)).all()
    attached_rule_ids: set[uuid.UUID] = {r.id for (r, _l) in linked_rows}
    attached = [
        {
            "link_id": link.id,
            "rule_id": rule.id,
            "public_id": rule.rule_id,
            "kind": rule.kind,
            "title": rule.title,
            "criterion": rule.criterion,
            "exception": rule.exception or "",
            "fields": list(rule.fields or []),
            "position": link.position,
            "link_active": link.is_active,
            "rule_active": rule.is_active,
        }
        for (rule, link) in linked_rows
    ]

    # Library rules the program hasn't attached yet — offered in the "Attach
    # a rule" picker. Skip retired rules (rule.is_active=False) so the
    # picker doesn't surface things ops has already decided to stop using.
    available_stmt = (
        select(Rule)
        .where(
            Rule.tenant_id == prog.tenant_id,
            Rule.is_active.is_(True),
        )
        .order_by(Rule.rule_id.asc())
    )
    available_all = list((await session.execute(available_stmt)).scalars())
    available = [r for r in available_all if r.id not in attached_rule_ids]

    # Doc packages available in this tenant for outcome bindings.
    pkg_stmt = (
        select(DocPackage)
        .where(DocPackage.tenant_id == prog.tenant_id, DocPackage.is_active.is_(True))
        .order_by(DocPackage.name.asc())
    )
    tenant_packages = [
        {"id": str(p.id), "name": p.name, "item_count": len(p.items or [])}
        for p in (await session.execute(pkg_stmt)).scalars()
    ]
    pkg_name_by_id = {p["id"]: p["name"] for p in tenant_packages}

    declared_outcomes = list(prog.outcomes or [])
    bindings_raw = dict(prog.package_bindings or {})
    outcome_rows = [
        {
            "outcome": o,
            "package_id": bindings_raw.get(o, ""),
            "package_name": pkg_name_by_id.get(bindings_raw.get(o, ""), ""),
            "bound": bool(bindings_raw.get(o)),
        }
        for o in declared_outcomes
    ]

    # Live model catalog for the Extract / Reason dropdowns. Cached
    # process-wide; refresh happens on first call after boot. Empty when the
    # tenant hasn't configured DOCGEN_AI_API_KEY — the UI degrades to
    # "no models discovered, using system defaults."
    settings = get_settings()
    catalog = get_cached_catalog(
        base_url=(settings.ai_base_url or "https://api.studio.nebius.com/v1"),
        api_key=(settings.ai_api_key or ""),
    )
    vision_models = catalog.vision_models()
    reason_models = catalog.chat_models()

    # Resolved 3-level chain for the "effective chain" panel + the triage
    # routing table's initial values. See pap_pipeline.config_resolver.
    from pap_pipeline.config_resolver import resolve_pipeline_models
    resolved = resolve_pipeline_models(program=prog, tenant=tenant, settings=settings)
    all_routes = ["decide", "pharmacy_direct", "crm_appeals", "crm_outreach", "reject"]

    return templates.TemplateResponse(
        request,
        "admin_program_detail.html",
        await _base_ctx(
            request, session, active="programs",
            program=prog, tenant=tenant,
            slots=list(prog.extraction_schema or []),
            slot_types=_SLOT_TYPES,
            template_versions=template_versions,
            bound_fields=bound_fields,
            attached_rules=attached,
            available_rules=available,
            tenant_packages=tenant_packages,
            outcome_rows=outcome_rows,
            declared_outcomes=declared_outcomes,
            vision_models=vision_models,
            reason_models=reason_models,
            resolved=resolved,
            all_routes=all_routes,
            flash=flash, flash_kind=flash_kind,
        ),
    )


# Kept as a tuple so the template's <select> and the write-path validator
# read from one source. Must stay in sync with the ck_rules_kind check
# constraint and RuleKind Literal in pap_pipeline.schemas.rule.
_RULE_KINDS = ("financial", "insurance", "residency", "clinical", "history")


def _parse_rule_fields(raw: str) -> list[str]:
    """Comma or newline separated intake field names → clean list."""
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


async def _snapshot_rule_version(
    session: AsyncSession,
    rule: Rule,
    request: Request,
    change_note: str,
) -> RuleVersion:
    """Append a rule_versions row capturing rule's current field values.

    Called from rules_create / rule_update after the rule row is flushed. The
    caller commits — this helper only adds to the session so the version + the
    parent rule mutation land in one transaction. version numbers are
    monotonic per rule; the query below is safe here because writes to a
    single rule are serialized by the admin UI (no concurrent editors).
    """
    next_version = (
        (
            await session.execute(
                select(func.coalesce(func.max(RuleVersion.version), 0) + 1).where(
                    RuleVersion.rule_id == rule.id
                )
            )
        ).scalar_one()
    )
    ident = _identity(request)
    version = RuleVersion(
        rule_id=rule.id,
        version=int(next_version),
        public_rule_id=rule.rule_id,
        kind=rule.kind,
        title=rule.title,
        criterion=rule.criterion,
        exception=rule.exception or "",
        fields=list(rule.fields or []),
        references_slots=(list(rule.references_slots) if rule.references_slots else None),
        is_active=rule.is_active,
        actor_user_id=(ident.user_id if ident else None),
        actor_username=(ident.username if ident else "system"),
        change_note=(change_note or "")[:200],
    )
    session.add(version)
    return version


@router.post("/programs/{program_id}/rule-links")
async def program_rule_attach(
    program_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Attach a library rule to this program.

    The picker submits `rule_id` (the library row's UUID). We verify the
    rule belongs to the same tenant, then append it at the end of the
    program's rule ordering. Silently no-ops on duplicate attach so a
    double-submit doesn't 500 the reviewer.
    """
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    form = await request.form()
    raw_rule_id = (form.get("rule_id") or "").strip()
    try:
        rule_uuid = uuid.UUID(raw_rule_id)
    except ValueError:
        return _redirect(
            f"/ui/programs/{program_id}",
            flash="Pick a rule from the library.", flash_kind="error",
        )
    rule = await session.get(Rule, rule_uuid)
    if rule is None or rule.tenant_id != prog.tenant_id:
        raise HTTPException(404, "Rule not found")

    max_pos_stmt = (
        select(func.coalesce(func.max(ProgramRuleLink.position), -1))
        .where(ProgramRuleLink.program_id == prog.id)
    )
    next_pos = int((await session.execute(max_pos_stmt)).scalar_one()) + 1

    session.add(
        ProgramRuleLink(
            program_id=prog.id,
            rule_id=rule.id,
            position=next_pos,
            is_active=True,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _redirect(
            f"/ui/programs/{program_id}",
            flash=f"Rule {rule.rule_id} is already attached.", flash_kind="info",
        )
    log.info(
        "program_rule_attached",
        program_id=str(prog.id), rule_id=str(rule.id), public_id=rule.rule_id,
    )
    return _redirect(
        f"/ui/programs/{program_id}",
        flash=f"Attached rule {rule.rule_id}.", flash_kind="success",
    )


@router.post("/programs/{program_id}/rule-links/{link_id}/toggle")
async def program_rule_toggle(
    program_id: uuid.UUID,
    link_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Flip the link's is_active — a per-program pause without detaching."""
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    link = await session.get(ProgramRuleLink, link_id)
    if link is None or link.program_id != prog.id:
        raise HTTPException(404, "Link not found")
    link.is_active = not link.is_active
    await session.commit()
    state = "enabled" if link.is_active else "paused"
    log.info(
        "program_rule_link_toggled",
        program_id=str(prog.id), link_id=str(link.id), is_active=link.is_active,
    )
    return _redirect(
        f"/ui/programs/{program_id}",
        flash=f"Link {state}.", flash_kind="success",
    )


@router.post("/programs/{program_id}/rule-links/{link_id}/delete")
async def program_rule_detach(
    program_id: uuid.UUID,
    link_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Detach a rule from this program. The library row is untouched."""
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    link = await session.get(ProgramRuleLink, link_id)
    if link is None or link.program_id != prog.id:
        raise HTTPException(404, "Link not found")
    await session.delete(link)
    await session.commit()
    log.info(
        "program_rule_detached",
        program_id=str(prog.id), link_id=str(link_id),
    )
    return _redirect(
        f"/ui/programs/{program_id}",
        flash="Rule detached.", flash_kind="success",
    )


async def _publish_gate_slot_refs(
    session: AsyncSession, prog: Program
) -> str | None:
    """Publish-gate check for slot references.

    Every slot name referenced by an active rule linked to ``prog`` must be
    declared in ``prog.history_slots``. Returns a human-readable error
    message when the gate refuses activation, or None when the pack is
    consistent.

    The gate lives here (rather than in the DB) because slot defs and rule
    references land through two separate write paths — the program editor
    and the rule editor. A cross-editor invariant belongs at the seam that
    turns editing into production, which is the ``is_active`` flip.
    """
    try:
        declared = {SlotDef.model_validate(d).name for d in (prog.history_slots or [])}
    except SlotCompileError as e:
        return f"Malformed slot definition: {e}."
    except Exception as e:  # pydantic ValidationError caught broadly
        return f"Malformed slot definition: {e}."

    stmt = (
        select(Rule.rule_id, Rule.references_slots)
        .join(ProgramRuleLink, ProgramRuleLink.rule_id == Rule.id)
        .where(
            ProgramRuleLink.program_id == prog.id,
            ProgramRuleLink.is_active.is_(True),
            Rule.is_active.is_(True),
        )
    )
    referenced: dict[str, list[str]] = {}
    for public_rule_id, refs in (await session.execute(stmt)).all():
        for name in (refs or []):
            referenced.setdefault(name, []).append(public_rule_id)

    missing = {n: rs for n, rs in referenced.items() if n not in declared}
    if not missing:
        return None
    parts = [f"{name!r} (used by {', '.join(rules)})" for name, rules in sorted(missing.items())]
    return (
        "Can't activate: rule(s) reference undeclared history slot(s): "
        + "; ".join(parts)
        + ". Declare them under Program → history_slots first."
    )


@router.post("/programs/{program_id}")
async def program_save(
    program_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Save program-level fields + the edited slot table."""
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    form = await request.form()

    prog.name = (form.get("name") or prog.name).strip() or prog.name
    wants_active = form.get("is_active") == "on"
    if wants_active:
        error = await _publish_gate_slot_refs(session, prog)
        if error is not None:
            return _redirect(
                f"/ui/programs/{program_id}",
                flash=error, flash_kind="error",
            )
    prog.is_active = wants_active
    prog.requires_review = form.get("requires_review") == "on"
    raw_tv = (form.get("enrollment_template_version_id") or "").strip()
    if raw_tv:
        try:
            prog.enrollment_template_version_id = uuid.UUID(raw_tv)
        except ValueError:
            return _redirect(
                f"/ui/programs/{program_id}",
                flash="Invalid template-version id.", flash_kind="error",
            )
    else:
        prog.enrollment_template_version_id = None

    # JSONB list mutation needs a fresh assignment for ORM dirty tracking.
    prog.extraction_schema = _parse_program_schema_form(form)
    await session.commit()
    log.info(
        "program_saved", program_id=str(prog.id),
        slots=len(prog.extraction_schema or []),
    )
    return _redirect(
        f"/ui/programs/{program_id}",
        flash="Program saved.", flash_kind="success",
    )


@router.post("/programs/{program_id}/models")
async def program_models_save(
    program_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Save the per-program Extract + Reason model choices.

    Isolated from :func:`program_save` because the Models tab has its own
    form and its own Save button. Blank/whitespace clears the override so
    the pipeline falls back to the settings-wide defaults.
    """
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    form = await request.form()

    # Each model field pairs with an `inherit_<field>` checkbox. When checked,
    # the operator wants the tenant/platform default — store NULL regardless
    # of what the (disabled) select submitted. When unchecked, store the
    # submitted value (blank still clears).
    def _resolve(field: str) -> str | None:
        if form.get(f"inherit_{field}"):
            return None
        return (form.get(field) or "").strip() or None

    prog.extract_model = _resolve("extract_model")
    prog.reason_model = _resolve("reason_model")
    prog.verifier_extract_model = _resolve("verifier_extract_model")
    prog.triage_model = _resolve("triage_model")

    # Triage routing overrides: read the effective (resolver-merged) row set,
    # keep only rows the operator changed vs the currently-effective route.
    # Stores minimal JSONB so the UI clearly shows "operator-changed" rows.
    from pap_pipeline.config_resolver import resolve_pipeline_models
    settings = get_settings()
    tenant = await session.get(Tenant, prog.tenant_id)
    tenant_resolved = resolve_pipeline_models(program=None, tenant=tenant, settings=settings)
    routing_overrides: dict[str, str] = {}
    for case_type, tenant_route in tenant_resolved.triage_routing.items():
        submitted = (form.get(f"route_{case_type}") or "").strip()
        if submitted and submitted != tenant_route:
            routing_overrides[case_type] = submitted
    prog.triage_routing = routing_overrides or None

    await session.commit()
    log.info(
        "program_models_saved",
        program_id=str(prog.id),
        extract_model=prog.extract_model,
        reason_model=prog.reason_model,
        verifier_extract_model=prog.verifier_extract_model,
        triage_model=prog.triage_model,
        triage_routing_overrides=prog.triage_routing,
    )
    return _redirect(
        f"/ui/programs/{program_id}#models",
        flash="Model choices saved.", flash_kind="success",
    )


@router.post("/programs/{program_id}/triage-routing")
async def program_triage_routing_save(
    program_id: uuid.UUID,
    request: Request,
    routing_json: str = Form("[]"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Save the per-program Triage routing table (dynamic rows).

    The form serializes the whole table into a single JSON payload
    (avoids HTML's unchecked-checkbox alignment trap). Rows whose values
    match the currently-effective tenant/platform default are dropped so
    the stored JSONB stays minimal and clearly represents "program
    overrides."
    """
    import json as _json
    from pap_pipeline.config_resolver import resolve_pipeline_models
    from docgen.store.models import Tenant

    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)

    # "Inherit routing from tenant" toggle: clear the program's overrides
    # entirely so the resolver falls back to the tenant/platform routing.
    form = await request.form()
    if form.get("inherit_routing"):
        prog.triage_routing = None
        await session.commit()
        log.info(
            "program_triage_routing_saved",
            program_id=str(prog.id),
            inherited_from_tenant=True,
        )
        return _redirect(
            f"/ui/programs/{program_id}#triage",
            flash="Triage routing now inherits from tenant.",
            flash_kind="success",
        )

    try:
        rows = _json.loads(routing_json)
        if not isinstance(rows, list):
            rows = []
    except (ValueError, TypeError):
        rows = []

    submitted: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ct = str(row.get("case_type", "")).strip()
        route = str(row.get("route", "")).strip()
        if not ct or not route:
            continue
        submitted[ct] = {
            "route": route,
            "decision_needed": bool(row.get("decision_needed", True)),
        }

    # Resolve the tenant baseline (without any program override) so we can
    # drop rows that match the tenant/platform default — keeps JSONB minimal
    # and makes program overrides audit-visible.
    settings = get_settings()
    tenant = await session.get(Tenant, prog.tenant_id)
    baseline = resolve_pipeline_models(
        program=None, tenant=tenant, settings=settings
    ).triage_routing

    overrides: dict[str, dict[str, Any]] = {}
    for ct, cfg in submitted.items():
        default = baseline.get(ct)
        if default is None or default != cfg:
            overrides[ct] = cfg

    prog.triage_routing = overrides or None
    await session.commit()
    log.info(
        "program_triage_routing_saved",
        program_id=str(prog.id),
        rows_submitted=len(submitted),
        overrides_stored=len(overrides),
    )
    return _redirect(
        f"/ui/programs/{program_id}#triage",
        flash="Triage routing saved.", flash_kind="success",
    )


@router.post("/programs/{program_id}/copy-from-template")
async def program_copy_from_template(
    program_id: uuid.UUID,
    request: Request,
    template_version_id: uuid.UUID = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Seed the program's slot list from an enrollment template's fields.

    Existing slots are replaced — this is the "start from the form" flow;
    admins that want to append should edit the table by hand. Sets the
    program's `enrollment_template_version_id` so the source is recorded.
    """
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    tv = await session.get(TemplateVersion, template_version_id)
    if tv is None:
        raise HTTPException(404, "Template version not found")
    tpl = await session.get(Template, tv.template_id)
    if tpl is None or tpl.tenant_id != prog.tenant_id:
        raise HTTPException(404, "Template version not found")
    new_slots = _tv_fields_to_slots(tv.schema)
    prog.extraction_schema = new_slots
    prog.enrollment_template_version_id = tv.id
    await session.commit()
    log.info(
        "program_slots_copied_from_template",
        program_id=str(prog.id), template_version_id=str(tv.id),
        slots=len(new_slots),
    )
    return _redirect(
        f"/ui/programs/{program_id}",
        flash=f"Copied {len(new_slots)} slot(s) from the enrollment template.",
        flash_kind="success",
    )


@router.post("/programs/{program_id}/delete")
async def program_delete(
    program_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    prog = await _load_program(session, program_id)
    _enforce_scope(request, prog.tenant_id)
    await session.delete(prog)
    await session.commit()
    log.info("program_deleted", program_id=str(program_id))
    return _redirect("/ui/programs", flash=f"Deleted program '{prog.name}'.", flash_kind="success")


# ---------------------------------------------------------------------------
# Rules library — tenant-scoped master rule set. Programs attach rules from
# here via ProgramRuleLink; editing a rule here propagates to every program
# that has it attached, which is the point of the library.
# ---------------------------------------------------------------------------


@router.get("/rules", response_class=HTMLResponse)
async def rules_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    """Master rule library — one row per tenant rule, with an attached-programs
    hint per row so ops can see reach at a glance.
    """
    scope = _forced_tenant_scope(request)
    stmt = (
        select(Rule, Tenant.name)
        .join(Tenant, Tenant.id == Rule.tenant_id)
        .order_by(Tenant.name.asc(), Rule.rule_id.asc())
    )
    if scope is not None:
        stmt = stmt.where(Rule.tenant_id == scope)
    rows = (await session.execute(stmt)).all()

    # Count attached programs per rule so the list gives ops a sense of
    # "how many programs will an edit to this rule ripple out to".
    counts_stmt = (
        select(ProgramRuleLink.rule_id, func.count(ProgramRuleLink.id))
        .group_by(ProgramRuleLink.rule_id)
    )
    counts = dict((await session.execute(counts_stmt)).all())

    library = [
        {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "tenant_name": tenant_name,
            "rule_id": r.rule_id,
            "kind": r.kind,
            "title": r.title,
            "criterion": r.criterion,
            "exception": r.exception or "",
            "fields": list(r.fields or []),
            "is_active": r.is_active,
            "attached_programs": counts.get(r.id, 0),
            "updated_at": r.updated_at,
        }
        for (r, tenant_name) in rows
    ]
    tenants_res = await session.execute(select(Tenant).order_by(Tenant.name.asc()))
    tenants_all = tenants_res.scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_rules.html",
        await _base_ctx(
            request, session, active="rules",
            rules=library,
            tenants_all=tenants_all,
            rule_kinds=_RULE_KINDS,
            flash=flash, flash_kind=flash_kind,
        ),
    )


@router.post("/rules")
async def rules_create(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create a new library rule under a tenant.

    Super-admin with no workspace picker must supply `tenant_id` in the
    form; tenant-admin (and picker-scoped super-admin) always creates under
    their effective tenant.
    """
    form = await request.form()
    scope = _forced_tenant_scope(request)
    if scope is not None:
        tenant_id = scope
    else:
        raw_tid = (form.get("tenant_id") or "").strip()
        try:
            tenant_id = uuid.UUID(raw_tid)
        except ValueError:
            return _redirect(
                "/ui/rules",
                flash="Pick a tenant before creating a rule.", flash_kind="error",
            )
    _enforce_scope(request, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")

    rule_id = (form.get("rule_id") or "").strip()
    kind = (form.get("kind") or "").strip()
    title_val = (form.get("title") or "").strip()
    criterion_val = (form.get("criterion") or "").strip()
    exception_val = (form.get("exception") or "").strip()
    fields = _parse_rule_fields(form.get("fields") or "")

    if not rule_id or not title_val or not criterion_val:
        return _redirect(
            "/ui/rules",
            flash="Rule ID, title, and criterion are required.", flash_kind="error",
        )
    if kind not in _RULE_KINDS:
        return _redirect(
            "/ui/rules",
            flash=f"Kind must be one of {', '.join(_RULE_KINDS)}.", flash_kind="error",
        )

    rule = Rule(
        tenant_id=tenant_id,
        rule_id=rule_id,
        kind=kind,
        title=title_val,
        criterion=criterion_val,
        exception=exception_val,
        fields=fields,
        is_active=True,
    )
    session.add(rule)
    try:
        await session.flush()
        await _snapshot_rule_version(session, rule, request, "Created")
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _redirect(
            "/ui/rules",
            flash=f"Rule ID '{rule_id}' already exists for this tenant.",
            flash_kind="error",
        )
    log.info("rule_created", tenant_id=str(tenant_id), rule_id=rule_id)
    return _redirect(
        f"/ui/rules/{rule.id}",
        flash=f"Created rule {rule_id}.", flash_kind="success",
    )


@router.get("/rules/{rule_row_id}", response_class=HTMLResponse)
async def rule_detail(
    rule_row_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    flash: str | None = None,
    flash_kind: str | None = None,
) -> HTMLResponse:
    """Edit a library rule + show the programs it's attached to."""
    rule = await session.get(Rule, rule_row_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    _enforce_scope(request, rule.tenant_id)
    tenant = await session.get(Tenant, rule.tenant_id)

    prog_stmt = (
        select(Program, ProgramRuleLink)
        .join(ProgramRuleLink, ProgramRuleLink.program_id == Program.id)
        .where(ProgramRuleLink.rule_id == rule.id)
        .order_by(Program.slug.asc())
    )
    attached = [
        {
            "program_id": p.id,
            "program_name": p.name,
            "program_slug": p.slug,
            "link_active": link.is_active,
        }
        for (p, link) in (await session.execute(prog_stmt)).all()
    ]
    versions_stmt = (
        select(RuleVersion)
        .where(RuleVersion.rule_id == rule.id)
        .order_by(RuleVersion.version.desc())
    )
    versions = [
        {
            "id": v.id,
            "version": v.version,
            "actor_username": v.actor_username,
            "change_note": v.change_note,
            "created_at": v.created_at,
        }
        for v in (await session.execute(versions_stmt)).scalars()
    ]
    return templates.TemplateResponse(
        request,
        "admin_rule_detail.html",
        await _base_ctx(
            request, session, active="rules",
            rule=rule,
            tenant=tenant,
            attached_programs=attached,
            versions=versions,
            rule_kinds=_RULE_KINDS,
            flash=flash, flash_kind=flash_kind,
        ),
    )


@router.post("/rules/{rule_row_id}")
async def rule_update(
    rule_row_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Update rule text / kind / fields / active state.

    Edits here ripple to every program that has the rule attached — that's
    the point of the library. `rule_id` (the public citation ID) is
    immutable so decision letters already cite a stable identifier.
    """
    rule = await session.get(Rule, rule_row_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    _enforce_scope(request, rule.tenant_id)
    form = await request.form()

    kind = (form.get("kind") or rule.kind).strip()
    if kind not in _RULE_KINDS:
        return _redirect(
            f"/ui/rules/{rule_row_id}",
            flash=f"Kind must be one of {', '.join(_RULE_KINDS)}.", flash_kind="error",
        )
    title_val = (form.get("title") or "").strip()
    criterion_val = (form.get("criterion") or "").strip()
    exception_val = (form.get("exception") or "").strip()
    if not title_val or not criterion_val:
        return _redirect(
            f"/ui/rules/{rule_row_id}",
            flash="Title and criterion cannot be empty.", flash_kind="error",
        )
    rule.kind = kind
    rule.title = title_val
    rule.criterion = criterion_val
    rule.exception = exception_val
    rule.fields = _parse_rule_fields(form.get("fields") or "")
    rule.is_active = form.get("is_active") == "on"
    await session.flush()
    await _snapshot_rule_version(session, rule, request, "Edited")
    await session.commit()
    log.info(
        "rule_updated",
        rule_row_id=str(rule.id), rule_id=rule.rule_id, is_active=rule.is_active,
    )
    return _redirect(
        f"/ui/rules/{rule_row_id}",
        flash=f"Saved rule {rule.rule_id}.", flash_kind="success",
    )


@router.post("/rules/{rule_row_id}/delete")
async def rule_delete(
    rule_row_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Hard-delete a library rule. Cascades through program_rule_links, so
    every program that had this rule attached loses the link too. Prefer
    the is_active toggle when the rule is retired but you want the history
    (attached-programs count, past letters citing the rule) legible.
    """
    rule = await session.get(Rule, rule_row_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    _enforce_scope(request, rule.tenant_id)
    rule_id = rule.rule_id
    await session.delete(rule)
    await session.commit()
    log.info("rule_deleted", rule_row_id=str(rule_row_id), rule_id=rule_id)
    return _redirect(
        "/ui/rules",
        flash=f"Deleted rule {rule_id}.", flash_kind="success",
    )


@router.get("/rules/{rule_row_id}/versions/{version}", response_class=HTMLResponse)
async def rule_version_view(
    rule_row_id: uuid.UUID,
    version: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Read-only snapshot of a rule at a specific version.

    Renders the same read-only card the current-rule view uses, but with the
    snapshot's field values instead of the live row's. No edit path.
    """
    rule = await session.get(Rule, rule_row_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    _enforce_scope(request, rule.tenant_id)
    tenant = await session.get(Tenant, rule.tenant_id)

    snapshot = (
        await session.execute(
            select(RuleVersion).where(
                RuleVersion.rule_id == rule.id, RuleVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(404, "Version not found")

    latest_version = (
        await session.execute(
            select(func.max(RuleVersion.version)).where(RuleVersion.rule_id == rule.id)
        )
    ).scalar_one() or version
    is_current = int(latest_version) == snapshot.version

    return templates.TemplateResponse(
        request,
        "admin_rule_version.html",
        await _base_ctx(
            request, session, active="rules",
            rule=rule,
            tenant=tenant,
            snapshot=snapshot,
            is_current=is_current,
            latest_version=int(latest_version),
        ),
    )



# ------------------------------------------------------------------
# Doc packages routes (static docs library + ordered composition +
# program outcome bindings). Split into its own module because this
# feature landed as a self-contained slice — see ADR-0003.
# ------------------------------------------------------------------
from docgen.ui.routes_doc_packages import register as _register_doc_packages  # noqa: E402

_register_doc_packages(
    router,
    templates,
    _base_ctx,
    _forced_tenant_scope,
    _enforce_scope,
    _redirect,
)
