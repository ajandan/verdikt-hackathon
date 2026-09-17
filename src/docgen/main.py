"""FastAPI entrypoint. Mounts REST and MCP (streamable-http) on one process."""
from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from docgen.api.pap import router as pap_router
from docgen.api.routes import router as rest_router
from docgen.auth import (
    SESSION_COOKIE,
    AdminSessionMiddleware,
    ApiKeyAuthMiddleware,
    generate_api_key,
    verify_session,
)
from docgen.config import get_settings
from docgen.downloads import router as downloads_router
from docgen.mcp_server import build_mcp_app, mcp
from docgen.metrics import render_metrics, sample_queue_depth_from_session
from docgen.observability import RequestLoggingMiddleware, configure_logging
from docgen.ratelimit import RateLimitMiddleware
from docgen.security_headers import SecurityHeadersMiddleware
from docgen.store.db import AsyncSessionLocal, sync_session
from docgen.store.models import AdminUser, ApiKey, Tenant
from docgen.ui.routes import router as ui_router
from docgen.ui.routes import templates as ui_templates

log = structlog.get_logger(__name__)

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
_REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


async def _startup_checks() -> None:
    settings = get_settings()
    if not settings.admin_password_hash:
        if settings.env == "production":
            raise RuntimeError(
                "Refusing to start in production with empty DOCGEN_ADMIN_PASSWORD_HASH. "
                "/ui/login would return 503. Generate a hash with: "
                "python -m docgen.auth hash-password <plaintext>"
            )
        log.warning(
            "admin_bootstrap_disabled",
            hint=(
                "DOCGEN_ADMIN_PASSWORD_HASH is empty. /ui/login will return 503. "
                "Generate a hash with: python -m docgen.auth hash-password <plaintext>"
            ),
        )

    if settings.env == "production" and settings.admin_user == "admin":
        # Not fatal — the password hash still gates access — but shipping the
        # default username in prod widens the credential-stuffing surface for
        # no real gain.
        log.warning(
            "admin_user_is_default",
            hint=(
                "DOCGEN_ADMIN_USER is still 'admin' in production. Change it to "
                "a per-deploy value to reduce credential-stuffing surface."
            ),
        )

    if settings.download_token_secret is None:
        # Falls back to session_secret. Fine for solo deploys but worth flagging
        # so operators know the two rotation domains are coupled.
        log.info(
            "download_token_secret_fallback",
            hint=(
                "DOCGEN_DOWNLOAD_TOKEN_SECRET unset — signed download URLs "
                "use DOCGEN_SESSION_SECRET. Rotating the session secret will "
                "also invalidate outstanding download URLs. Set an independent "
                "secret to decouple."
            ),
        )

    # Seed the first super_admin row from the env-var credentials if the
    # admin_users table is empty. Once anyone exists in the table we stop
    # seeding — subsequent env-var rotations don't clone new rows. The
    # env-var identity is also accepted at login as a break-glass path
    # even when the table has other users (see docgen.auth).
    if settings.admin_password_hash and settings.admin_user:
        try:
            async with AsyncSessionLocal() as session:
                # tenant-audit: allow (startup seed check — table-empty probe
                # for the first-boot super_admin seed; cross-tenant by design).
                res = await session.execute(select(AdminUser).limit(1))
                if res.scalars().first() is None:
                    session.add(
                        AdminUser(
                            tenant_id=None,
                            username=settings.admin_user,
                            password_hash=settings.admin_password_hash,
                            role="super_admin",
                            is_active=True,
                        )
                    )
                    await session.commit()
                    log.info(
                        "admin_users_seeded",
                        username=settings.admin_user,
                        role="super_admin",
                    )
        except Exception:
            # Table may not exist yet on a first boot before migrations run.
            # We don't want to crash startup — the env-var break-glass path
            # still works. Log for visibility.
            log.warning("admin_users_seed_skipped", exc_info=True)

    if settings.bootstrap_key:
        # Auto-issued API keys are a dev-only convenience. Refuse in production
        # even if the flag is set. Raw key goes to stderr, never structlog.
        if settings.env != "development":
            log.error(
                "bootstrap_refused",
                reason="DOCGEN_BOOTSTRAP_KEY=true is only allowed when DOCGEN_ENV=development",
            )
        else:
            try:
                async with AsyncSessionLocal() as session:
                    tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
                    if tenant is None:
                        tenant = Tenant(id=DEFAULT_TENANT_ID, name="default")
                        session.add(tenant)
                        await session.commit()
                    # Only create a key if there are no active keys for this tenant.
                    res = await session.execute(
                        select(ApiKey).where(ApiKey.tenant_id == DEFAULT_TENANT_ID)
                    )
                    if not res.scalars().first():
                        raw, prefix, digest = generate_api_key()
                        session.add(
                            ApiKey(
                                tenant_id=DEFAULT_TENANT_ID,
                                name="bootstrap",
                                key_prefix=prefix,
                                key_hash=digest,
                            )
                        )
                        await session.commit()
                        # Structured log carries only the prefix; the raw key
                        # goes to stderr for a human to capture once.
                        log.warning(
                            "bootstrap_api_key_created",
                            tenant_id=str(DEFAULT_TENANT_ID),
                            key_prefix=prefix,
                            note="Raw key printed to stderr once. Disable bootstrap after capture.",
                        )
                        print(
                            f"\n[docgen bootstrap] tenant={DEFAULT_TENANT_ID} api_key={raw}\n"
                            "[docgen bootstrap] Store this now — it will not be shown again.\n",
                            file=sys.stderr,
                            flush=True,
                        )
            except Exception:
                log.exception("bootstrap_failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _startup_checks()
    from docgen.plugins import load_plugins

    load_plugins()
    # FastMCP's streamable-http transport needs its session-manager task group
    # started before the first request; sub-app lifespans don't fire under
    # Starlette mount, so we drive it from the parent app.
    async with mcp.session_manager.run():
        yield


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    app = FastAPI(
        title="DocGen",
        version="0.1.0",
        description="Template-based document generation. Design-time AI, deterministic render.",
        lifespan=lifespan,
    )
    # Order matters: outer → inner. SecurityHeaders wraps everything so even
    # 401/403/500 responses get CSP + HSTS. Request log sits just inside it so
    # request_id is stamped before headers are applied. Auth then rate limit
    # so the rate limiter sees `request.state.tenant_id`.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(AdminSessionMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(rest_router)
    app.include_router(pap_router)
    app.include_router(ui_router)
    app.include_router(downloads_router)

    if not (_STATIC_DIR / "app.css").exists():
        log.warning(
            "static_css_missing",
            hint=(
                "src/docgen/ui/static/app.css is not present. Compile it with "
                "`scripts/build-css.ps1` (Windows) or `scripts/build-css.sh` "
                "(Linux/macOS). Docker builds produce it automatically."
            ),
        )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Engineering dashboard — static HTML hub (index, features, tests, code,
    # security) generated into reports/. Public because reviewers land here
    # from the hackathon block on the marketing page. Bypasses api-key auth
    # via the `/engineering` prefix in docgen.auth._BYPASS_PREFIXES.
    if _REPORTS_DIR.exists():
        # Land on About instead of the static-mount default (index.html).
        @app.get("/engineering/", include_in_schema=False)
        @app.get("/engineering", include_in_schema=False)
        async def _engineering_landing() -> RedirectResponse:
            return RedirectResponse(url="/engineering/about.html", status_code=307)

        app.mount(
            "/engineering",
            StaticFiles(directory=str(_REPORTS_DIR), html=True),
            name="engineering",
        )
    else:
        log.warning("engineering_reports_missing", path=str(_REPORTS_DIR))

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        """Prometheus exposition endpoint.

        Refreshes the queue-depth gauges from the DB on every scrape so the
        gauge stays consistent even if a worker crash left a job stuck in
        "running" (the counters would keep incrementing forever otherwise).
        Bypasses API-key auth (see `docgen.auth._BYPASS_EXACT`) — Prometheus
        scrapers don't carry bearer tokens; restrict access at the network
        edge (allowlist the scraper IP or expose only on a private port).
        """
        try:
            with sync_session() as session:
                sample_queue_depth_from_session(session)
        except Exception:
            log.exception("metrics_queue_depth_sample_failed")
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    @app.get("/", include_in_schema=False)
    async def _root(request: Request) -> Response:
        # Always serve the public marketing landing at the root so admins
        # can still show it during demos or after signing in. When a valid
        # session cookie is present the template swaps the "Sign in" CTAs
        # for "Open dashboard" so returning admins get one click back in.
        cookie = request.cookies.get(SESSION_COOKIE)
        is_authenticated = bool(cookie and verify_session(cookie) is not None)
        return ui_templates.TemplateResponse(
            request, "landing.html", {"is_authenticated": is_authenticated}
        )

    mcp_app = build_mcp_app()
    app.mount("/mcp", mcp_app)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("docgen.main:app", host=s.host, port=s.port, log_level=s.log_level.lower())
