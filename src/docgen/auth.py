"""Authentication primitives: API-key auth for REST + session auth for /ui admin portal.

Two middlewares share the auth surface:
- `ApiKeyAuthMiddleware` reads `Authorization: Bearer <key>`, sets `request.state.tenant_id`.
- `AdminSessionMiddleware` reads a signed cookie, sets `request.state.admin_user`.

Bypass paths for API-key auth: /, /health, /docs, /openapi.json, /redoc, /ui/*.
(MCP is mounted at /mcp and manages its own auth.)
The UI middleware only fires for /ui/* except /ui/login and /ui/logout.

Follows the pattern in `observability.py`: BaseHTTPMiddleware, structlog, best-effort
DB writes off the request path where feasible.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import sys
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import bcrypt
import structlog
from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from docgen.config import get_settings
from docgen.security_headers import secure_cookie_flag
from docgen.store.db import AsyncSessionLocal
from docgen.store.models import AdminUser, ApiKey

log = structlog.get_logger(__name__)

_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_api_key_id: ContextVar[str | None] = ContextVar("api_key_id", default=None)
_admin_user: ContextVar[str | None] = ContextVar("admin_user", default=None)

# Retain references to fire-and-forget background tasks so they aren't GC'd
# mid-flight (see RUF006 / asyncio.create_task docs).
_bg_tasks: set[asyncio.Task] = set()

# In-process debounce for last_used_at: at most one write per key per window.
# Trades sub-window precision (harmless — this field is for humans, not billing)
# for bounded write pressure under bursty API traffic. Sync check-and-set below
# is atomic w.r.t. other coroutines because there is no `await` between the get
# and set — single-loop asyncio does not preempt sync code. Bounded to
# _LAST_USED_MAX_ENTRIES to prevent unbounded growth when many distinct keys
# are seen (multi-tenant with high key churn).
_LAST_USED_DEBOUNCE_SECONDS = 60.0
_LAST_USED_MAX_ENTRIES = 10_000
_last_used_written_at: dict[str, float] = {}

SESSION_COOKIE = "docgen_session"
SESSION_MAX_AGE_SECONDS = 24 * 60 * 60

# CSRF: double-submit cookie. The token is rendered into every mutating form as
# a hidden `csrf_token` field; the middleware compares cookie == form field.
# HttpOnly is safe because the form is server-rendered — the client never needs
# to read the cookie via JS.
CSRF_COOKIE = "docgen_csrf"
CSRF_TOKEN_BYTES = 32
# POSTs exempt from CSRF: login (no session yet). Logout requires it — CSRF-forced
# logout is a nuisance vector otherwise.
_CSRF_EXEMPT_PATHS = {"/ui/login"}


# ---------------------------------------------------------------------------
# API key generation + hashing
# ---------------------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    """Return (raw, prefix, hash). Raw looks like 'dgk_<32 hex chars>'."""
    body = secrets.token_hex(16)  # 32 hex chars
    raw = f"dgk_{body}"
    prefix = raw[:12]  # e.g. 'dgk_abcdefgh'
    return raw, prefix, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Admin identity resolution (DB-backed with env-var break-glass)
# ---------------------------------------------------------------------------
# The UI login has two sources of truth:
#   1. `admin_users` table (per-tenant DB-backed identities, roles).
#   2. Env-var pair DOCGEN_ADMIN_USER + DOCGEN_ADMIN_PASSWORD_HASH — a
#      break-glass super_admin so an operator can never lock themselves out
#      of a fresh deploy. Accepted at login even when the DB has other rows.
#
# A successful login stashes only the username in the signed session cookie
# (unchanged shape, so existing sessions from before this migration stay
# valid). The middleware resolves username → identity on each request via
# `resolve_admin_identity`, so role/tenant changes take effect immediately.


@dataclass(frozen=True)
class AdminIdentity:
    """Resolved admin identity for the current request.

    `user_id` is None when the identity came from the env-var break-glass
    path (no DB row exists yet). `tenant_id` is None for super_admin.
    """

    username: str
    role: str  # "super_admin" | "tenant_admin"
    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None


def _env_super_admin_identity() -> AdminIdentity | None:
    """Virtual super_admin backed by the env-var credentials. Returned when
    the caller's username matches DOCGEN_ADMIN_USER — used at login for the
    break-glass path and at request-resolve time when a DB row is absent."""
    s = get_settings()
    if not s.admin_user or not s.admin_password_hash:
        return None
    return AdminIdentity(
        username=s.admin_user,
        role="super_admin",
        tenant_id=None,
        user_id=None,
    )


async def authenticate_admin(
    session: AsyncSession, username: str, password: str
) -> AdminIdentity | None:
    """Verify username+password against `admin_users`, falling back to the
    env-var break-glass path. Bumps `last_login_at` on a DB-row match."""
    if not username or not password:
        return None

    # tenant-audit: allow (login lookup — username is globally unique across
    # admin_users; scope by tenant_id would break super_admin sign-in).
    row = (
        await session.execute(
            select(AdminUser).where(AdminUser.username == username)
        )
    ).scalar_one_or_none()
    if row is not None and row.is_active and verify_password(password, row.password_hash):
        row.last_login_at = datetime.now(UTC)
        await session.commit()
        return AdminIdentity(
            username=row.username,
            role=row.role,
            tenant_id=row.tenant_id,
            user_id=row.id,
        )

    # Break-glass: env-var admin. Accepted whenever the credentials match,
    # regardless of what's in the DB — so a wedged admin_users table can't
    # lock the operator out.
    env = _env_super_admin_identity()
    s = get_settings()
    if (
        env is not None
        and username == env.username
        and verify_password(password, s.admin_password_hash)
    ):
        return env
    return None


async def resolve_admin_identity(
    session: AsyncSession, username: str
) -> AdminIdentity | None:
    """Resolve a username (from a valid session cookie) to an AdminIdentity.

    Preference order: DB row wins (so role/tenant updates take effect on the
    next request); falls back to the env-var virtual super_admin. Returns
    None if the username matches nothing or the row is deactivated — the
    caller (middleware) then kicks the session to /ui/login.
    """
    # tenant-audit: allow (session-cookie resolver — username is globally
    # unique; see authenticate_admin above).
    row = (
        await session.execute(
            select(AdminUser).where(AdminUser.username == username)
        )
    ).scalar_one_or_none()
    if row is not None:
        if not row.is_active:
            return None
        return AdminIdentity(
            username=row.username,
            role=row.role,
            tenant_id=row.tenant_id,
            user_id=row.id,
        )
    env = _env_super_admin_identity()
    if env is not None and env.username == username:
        return env
    return None


# ---------------------------------------------------------------------------
# Session cookie signing
# ---------------------------------------------------------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="docgen-admin-session")


def sign_session(user: str) -> str:
    return _serializer().dumps({"u": user})


def verify_session(cookie: str) -> str | None:
    try:
        data = _serializer().loads(cookie, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(data, dict):
        u = data.get("u")
        if isinstance(u, str):
            return u
    return None


# ---------------------------------------------------------------------------
# Signed download tokens (job artifact URLs)
# ---------------------------------------------------------------------------
# MCP tool responses hand back a `fetch_url` for the rendered artifact. When a
# human (or a client without our Bearer key) clicks it, they'd otherwise hit
# 401. A short-lived HMAC token, bound to a specific job_id + tenant, lets the
# URL be clickable while keeping the endpoint auth-protected for everyone else.

DOWNLOAD_TOKEN_MAX_AGE_SECONDS = 15 * 60


def _download_serializer() -> URLSafeTimedSerializer:
    # Prefer the dedicated secret so a session-secret rotation doesn't also
    # invalidate every outstanding download URL. Fall back to session_secret
    # so single-purpose deploys still work with no extra config.
    s = get_settings()
    secret = s.download_token_secret or s.session_secret
    return URLSafeTimedSerializer(secret, salt="docgen-download")


def sign_download_token(job_id: str, tenant_id: str) -> str:
    return _download_serializer().dumps({"j": job_id, "t": tenant_id})


def verify_download_token(token: str, job_id: str) -> str | None:
    """Return tenant_id if token is valid and bound to this job_id, else None."""
    try:
        data = _download_serializer().loads(
            token, max_age=DOWNLOAD_TOKEN_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or data.get("j") != job_id:
        return None
    t = data.get("t")
    return t if isinstance(t, str) else None


# ---------------------------------------------------------------------------
# Signed inbox-test download tokens (email-embedded artifact URLs)
# ---------------------------------------------------------------------------
# The inbox reply table gives reviewers a per-row download link for each
# AI-planned test render. Emails are opened days after they arrive, so the
# 15-minute job-download TTL is too short. A dedicated 30-day serializer
# keeps that concern separated from live job artifacts.

INBOX_TEST_TOKEN_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _inbox_test_serializer() -> URLSafeTimedSerializer:
    s = get_settings()
    secret = s.download_token_secret or s.session_secret
    return URLSafeTimedSerializer(secret, salt="docgen-inbox-test")


def sign_inbox_test_token(test_result_id: str) -> str:
    return _inbox_test_serializer().dumps({"r": test_result_id})


def verify_inbox_test_token(token: str, test_result_id: str) -> bool:
    """True iff the token is valid and bound to this test_result_id."""
    try:
        data = _inbox_test_serializer().loads(
            token, max_age=INBOX_TEST_TOKEN_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and data.get("r") == test_result_id


# ---------------------------------------------------------------------------
# ApiKey middleware
# ---------------------------------------------------------------------------

_BYPASS_EXACT = {"/", "/health", "/docs", "/openapi.json", "/redoc"}
_BYPASS_PREFIXES = ("/ui", "/static", "/engineering")


def _bypass_api_auth(path: str) -> bool:
    if path in _BYPASS_EXACT:
        return True
    for pref in _BYPASS_PREFIXES:
        if path == pref or path.startswith(pref + "/"):
            return True
    return False


_JOB_RESULT_PATH = re.compile(r"^/jobs/([^/]+)/result$")
_INBOX_TEST_DOWNLOAD_PATH = re.compile(
    r"^/downloads/inbox-test/([^/]+)$"
)
_CASE_REPLY_DOWNLOAD_PATH = re.compile(
    r"^/downloads/case-reply/([^/]+)$"
)


def _download_token_tenant(request: Request) -> str | None:
    """If the request is a signed artifact-download URL with a valid token,
    return the tenant_id from the token; otherwise None.

    Covers both job-result URLs (`/jobs/<id>/result`) and case-reply URLs
    (`/downloads/case-reply/<event_id>`) — both share the same short-lived
    serializer, so the caller can treat any successful signature as an
    authenticated request against the payload's tenant scope.
    """
    if request.method != "GET":
        return None
    for pattern in (_JOB_RESULT_PATH, _CASE_REPLY_DOWNLOAD_PATH):
        m = pattern.fullmatch(request.url.path)
        if m is None:
            continue
        token = request.query_params.get("token")
        if not token:
            return None
        return verify_download_token(token, m.group(1))
    return None


def _inbox_test_token_valid(request: Request) -> bool:
    """True iff the request is a signed inbox-test download URL.

    Unlike job artifacts (which carry a tenant scope in the token) these
    URLs are self-contained: the token binds solely to a test-result id
    and the route re-loads the row to authorize the blob fetch.
    """
    if request.method != "GET":
        return False
    m = _INBOX_TEST_DOWNLOAD_PATH.fullmatch(request.url.path)
    if m is None:
        return False
    token = request.query_params.get("token")
    if not token:
        return False
    return verify_inbox_test_token(token, m.group(1))


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Enforces `Authorization: Bearer <api key>` on REST endpoints."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if _bypass_api_auth(path):
            return await call_next(request)

        # Signed inbox-test-download URLs are embedded in outbound reply
        # emails and clicked days later, so they use a self-contained
        # 30-day token that binds to a specific InboxTestResult row. The
        # route itself re-reads the row to authorize the blob fetch, so
        # this middleware only needs to accept a valid signature.
        if _inbox_test_token_valid(request):
            return await call_next(request)

        # Short-lived signed URLs for job artifacts bypass Bearer auth. The
        # token itself carries the tenant scope; the route still checks it
        # against the job's tenant_id when serving bytes.
        token_tenant = _download_token_tenant(request)
        if token_tenant is not None:
            try:
                request.state.tenant_id = uuid.UUID(token_tenant)
            except ValueError:
                # Signature was valid but payload's tenant field isn't a UUID —
                # treat as a forged/malformed token, don't crash into a 500.
                log.info("auth_download_token_bad_tenant", path=path)
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            tenant_token = _tenant_id.set(token_tenant)
            try:
                return await call_next(request)
            finally:
                _tenant_id.reset(tenant_token)

        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if not auth or not auth.lower().startswith("bearer "):
            log.info("auth_missing", path=path)
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        raw_key = auth.split(" ", 1)[1].strip()
        if not raw_key:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        digest = hash_api_key(raw_key)
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))
            api_key: ApiKey | None = res.scalar_one_or_none()

        if api_key is None or api_key.revoked_at is not None:
            log.info("auth_invalid_or_revoked", path=path, has_key=api_key is not None)
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        request.state.tenant_id = api_key.tenant_id
        request.state.api_key_id = api_key.id
        tenant_token = _tenant_id.set(str(api_key.tenant_id))
        key_token = _api_key_id.set(str(api_key.id))

        # Fire-and-forget last_used_at bump, debounced per key so a busy caller
        # doesn't spawn one task + one UPDATE per request.
        if _should_bump_last_used(str(api_key.id)):
            try:
                task = asyncio.create_task(_bump_last_used(api_key.id))
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
            except RuntimeError:
                pass

        try:
            response = await call_next(request)
        finally:
            _tenant_id.reset(tenant_token)
            _api_key_id.reset(key_token)
        return response


def _should_bump_last_used(api_key_id: str) -> bool:
    """Return True at most once per key per debounce window."""
    now = _monotonic()
    last = _last_used_written_at.get(api_key_id, 0.0)
    if now - last < _LAST_USED_DEBOUNCE_SECONDS:
        return False
    # Cap dict size. Under bursty multi-tenant traffic, unique keys could
    # accumulate faster than they expire. Drop entries older than the window
    # first; if that isn't enough (all entries hot), clear everything — the
    # only cost is a duplicate UPDATE for each dropped key on its next hit.
    if len(_last_used_written_at) >= _LAST_USED_MAX_ENTRIES:
        cutoff = now - _LAST_USED_DEBOUNCE_SECONDS
        stale = [k for k, t in _last_used_written_at.items() if t < cutoff]
        for k in stale:
            _last_used_written_at.pop(k, None)
        if len(_last_used_written_at) >= _LAST_USED_MAX_ENTRIES:
            _last_used_written_at.clear()
    _last_used_written_at[api_key_id] = now
    return True


def _monotonic() -> float:
    # Extracted so tests can monkeypatch cheaply.
    import time
    return time.monotonic()


async def _bump_last_used(api_key_id) -> None:
    try:
        async with AsyncSessionLocal() as s:
            # tenant-audit: allow (the caller already resolved this api_key_id
            # from the incoming Bearer token via a hash lookup that IS
            # tenant-agnostic by design — auth happens before tenant is known;
            # this is just a "last used" timestamp bump on that exact row).
            await s.execute(
                update(ApiKey).where(ApiKey.id == api_key_id).values(last_used_at=datetime.now(UTC))
            )
            await s.commit()
    except Exception:
        log.exception("bump_last_used_failed", api_key_id=str(api_key_id))


# ---------------------------------------------------------------------------
# Admin session middleware
# ---------------------------------------------------------------------------

_UI_PUBLIC = {"/ui/login", "/ui/logout"}


class AdminSessionMiddleware(BaseHTTPMiddleware):
    """Guards /ui/* except /ui/login and /ui/logout.

    Also ensures a CSRF cookie exists for any authenticated request. Templates
    read `request.state.csrf_token` to render the hidden form input; the
    `enforce_csrf_if_mutating` dependency verifies it on POST.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not (path == "/ui" or path.startswith("/ui/")):
            return await call_next(request)
        if path in _UI_PUBLIC:
            return await call_next(request)

        cookie = request.cookies.get(SESSION_COOKIE)
        user = verify_session(cookie) if cookie else None
        if user is None:
            # Preserve where the user was headed.
            next_qs = quote(request.url.path + (("?" + request.url.query) if request.url.query else ""))
            return RedirectResponse(f"/ui/login?next={next_qs}", status_code=303)

        # Resolve the cookie's username to a live identity — deactivated or
        # deleted users get kicked to login even if their cookie is unexpired.
        async with AsyncSessionLocal() as db:
            identity = await resolve_admin_identity(db, user)
        if identity is None:
            next_qs = quote(request.url.path + (("?" + request.url.query) if request.url.query else ""))
            resp = RedirectResponse(f"/ui/login?next={next_qs}", status_code=303)
            resp.delete_cookie(SESSION_COOKIE, path="/")
            return resp

        request.state.admin_user = identity.username
        request.state.admin_identity = identity
        csrf_cookie = request.cookies.get(CSRF_COOKIE)
        csrf_new: str | None = None
        if not csrf_cookie:
            csrf_new = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
            csrf_cookie = csrf_new
        request.state.csrf_token = csrf_cookie

        token = _admin_user.set(user)
        try:
            response = await call_next(request)
        finally:
            _admin_user.reset(token)

        if csrf_new is not None:
            response.set_cookie(
                CSRF_COOKIE, csrf_new,
                max_age=SESSION_MAX_AGE_SECONDS,
                httponly=True,
                samesite="lax",
                secure=secure_cookie_flag(request),
                path="/",
            )
        return response


async def enforce_csrf_if_mutating(request: Request) -> None:
    """FastAPI dependency: verify CSRF on mutating /ui/* requests.

    Double-submit pattern: `csrf_token` form field (or `X-CSRF-Token` header)
    must equal the `docgen_csrf` cookie. Safe methods (GET/HEAD/OPTIONS) skip.
    Login is exempt because no session (and no CSRF cookie) exists yet.
    """
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if request.url.path in _CSRF_EXEMPT_PATHS:
        return
    cookie_val = request.cookies.get(CSRF_COOKIE)
    submitted = request.headers.get("X-CSRF-Token")
    if not submitted:
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
            # Starlette caches form() at the Request; the route handler's Form(...)
            # extraction will re-use this parse instead of re-reading the body.
            try:
                form = await request.form()
            except Exception:
                form = None
            if form is not None:
                val = form.get("csrf_token")
                if isinstance(val, str):
                    submitted = val
    if not cookie_val or not submitted or not secrets.compare_digest(cookie_val, submitted):
        log.info(
            "csrf_rejected",
            path=request.url.path,
            has_cookie=bool(cookie_val),
            has_submitted=bool(submitted),
        )
        raise HTTPException(status_code=403, detail="CSRF token invalid or missing")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
# Usage inside the container/host after alembic migrations have run:
#   python -m docgen.auth hash-password <plaintext>
#   python -m docgen.auth create-tenant <name>
#   python -m docgen.auth create-user <username> <password> <role> [<tenant_id>]
#     role: super_admin | tenant_admin
#     tenant_id: required when role=tenant_admin; must be omitted for super_admin


def _cli_create_tenant(name: str) -> int:
    from docgen.store.db import sync_session
    from docgen.store.models import Tenant

    with sync_session() as s:
        t = Tenant(name=name)
        s.add(t)
        s.commit()
        s.refresh(t)
        print(str(t.id))
    return 0


def _cli_create_user(
    username: str, password: str, role: str, tenant_id: str | None
) -> int:
    from docgen.store.db import sync_session
    from docgen.store.models import AdminUser, Tenant

    if role not in ("super_admin", "tenant_admin"):
        print("role must be 'super_admin' or 'tenant_admin'", file=sys.stderr)
        return 2
    parsed_tenant: uuid.UUID | None = None
    if role == "tenant_admin":
        if not tenant_id:
            print("tenant_admin requires a tenant_id", file=sys.stderr)
            return 2
        try:
            parsed_tenant = uuid.UUID(tenant_id)
        except ValueError:
            print(f"invalid tenant_id: {tenant_id}", file=sys.stderr)
            return 2
    else:
        if tenant_id:
            print("super_admin must not have a tenant_id", file=sys.stderr)
            return 2

    with sync_session() as s:
        if parsed_tenant is not None and s.get(Tenant, parsed_tenant) is None:
            print(f"tenant {parsed_tenant} does not exist", file=sys.stderr)
            return 1
        u = AdminUser(
            tenant_id=parsed_tenant,
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        s.add(u)
        try:
            s.commit()
        except Exception as e:
            s.rollback()
            print(f"create failed (username may already exist): {e}", file=sys.stderr)
            return 1
        s.refresh(u)
        print(str(u.id))
    return 0


def _cli() -> int:
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "hash-password":
        print(hash_password(argv[1]))
        return 0
    if len(argv) == 2 and argv[0] == "create-tenant":
        return _cli_create_tenant(argv[1])
    if len(argv) in (4, 5) and argv[0] == "create-user":
        return _cli_create_user(argv[1], argv[2], argv[3], argv[4] if len(argv) == 5 else None)
    print(
        "Usage:\n"
        "  python -m docgen.auth hash-password <plaintext>\n"
        "  python -m docgen.auth create-tenant <name>\n"
        "  python -m docgen.auth create-user <username> <password> <role> [<tenant_id>]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
