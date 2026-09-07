"""Per-tenant token-bucket rate limiter backed by Redis (fixed windows).

Key format: `ratelimit:{tenant_id}:{action}:{window_start_epoch}`.

Only rate-limits routes that have known limits (render, upload). All other routes
pass through untouched. Requires `request.state.tenant_id` to have been set by
`ApiKeyAuthMiddleware`; if absent (e.g. bypassed route), pass through.
"""
from __future__ import annotations

import ipaddress
import secrets
import time
from functools import lru_cache

import redis.asyncio as aioredis
import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from docgen.config import get_settings

log = structlog.get_logger(__name__)

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            # The client may be bound to a closed loop (common in tests that
            # spin up multiple TestClients). Dropping the reference is enough —
            # the underlying socket goes with the dead loop.
            pass
        finally:
            _client = None


def _limits() -> dict[tuple[str, str], tuple[str, int, int]]:
    """Return {(method, path): (action, limit, window_seconds)}.

    All render entry points share the "render" bucket so a caller can't dodge
    the limit by switching endpoints. Upload includes /templates only (that's
    the write path; GET /templates is a listing).
    """
    s = get_settings()
    r = s.rate_limit_renders_per_minute
    u = s.rate_limit_uploads_per_hour
    return {
        ("POST", "/render"): ("render", r, 60),
        ("POST", "/render/mapped"): ("render", r, 60),
        ("POST", "/templates"): ("upload", u, 3600),
    }


# Actions where failing open on a Redis outage would be worse than a brief
# outage of the endpoint (unbounded render volume can wedge the worker pool
# and rack up cost). Everything else falls back to pass-through.
_FAIL_CLOSED_ACTIONS = frozenset({"render"})


def _bucket_key(tenant_id: str, action: str, window: int) -> str:
    return f"ratelimit:{tenant_id}:{action}:{window}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies fixed-window limits per (tenant, action)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rule = _limits().get((request.method, request.url.path))
        if rule is None:
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is None:
            # No auth applied (or bypassed) — nothing to key on.
            return await call_next(request)

        action, limit, window_seconds = rule
        now = int(time.time())
        window_start = now - (now % window_seconds)
        key = _bucket_key(str(tenant_id), action, window_start)

        r = get_redis()
        try:
            pipe = r.pipeline()
            pipe.incr(key, 1)
            pipe.expire(key, window_seconds)
            results = await pipe.execute()
            count = int(results[0])
        except Exception:
            log.exception("ratelimit_backend_error", action=action, tenant_id=str(tenant_id))
            if action in _FAIL_CLOSED_ACTIONS:
                return JSONResponse(
                    {"detail": "rate limiter unavailable", "action": action},
                    status_code=503,
                    headers={"Retry-After": "5"},
                )
            return await call_next(request)

        remaining = max(0, limit - count)
        reset_at = window_start + window_seconds

        if count > limit:
            retry_after = max(1, reset_at - now)
            log.info(
                "ratelimit_exceeded",
                action=action,
                tenant_id=str(tenant_id),
                count=count,
                limit=limit,
            )
            headers = {
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_at),
            }
            return JSONResponse(
                {"detail": "rate limit exceeded", "action": action, "retry_after": retry_after},
                status_code=429,
                headers=headers,
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_at)
        return response


# ---------------------------------------------------------------------------
# /ui/login rate limiter (§4)
#
# Sliding window in a Redis sorted set: each attempt is a member scored by its
# arrival timestamp. Before each attempt we prune scores older than the window
# and count what's left. Fail-open on Redis errors so an outage doesn't lock
# the admin out of the console.
#
# Keyed on (ip, username) because that's what the roadmap calls for; if the
# same IP burns 10 usernames in a short window we won't catch it here, but the
# audit log makes such patterns obvious to a human reviewer.
# ---------------------------------------------------------------------------

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 10


def _login_bucket_key(ip: str, username: str) -> str:
    # Usernames are user input; hash-free but colon-safe by replacing the sep.
    safe_user = username.replace(":", "_")[:64]
    return f"login_rl:{ip}:{safe_user}"


@lru_cache(maxsize=1)
def _trusted_proxy_nets() -> tuple[ipaddress._BaseNetwork, ...]:
    raw = get_settings().trusted_proxies or ""
    nets: list[ipaddress._BaseNetwork] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            log.warning("trusted_proxy_invalid", value=token)
    return tuple(nets)


def _peer_is_trusted(peer: str | None) -> bool:
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(ip in net for net in _trusted_proxy_nets())


def client_ip(request: Request) -> str:
    """Best-effort caller IP.

    Only trusts `X-Forwarded-For` when the direct TCP peer is in
    `DOCGEN_TRUSTED_PROXIES`. Without this gate, any caller can forge their
    source IP for rate limiting and login-lockout evasion.
    """
    client = request.client
    peer = client.host if client else None
    if _peer_is_trusted(peer):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    return peer or "unknown"


async def check_login_attempt(ip: str, username: str) -> tuple[bool, int]:
    """Sliding-window check for /ui/login.

    Returns `(allowed, retry_after_seconds)`. On reject, the returned
    retry_after is how many seconds until the oldest windowed attempt ages
    out. Fail-open on backend errors — logs the exception.
    """
    now = time.time()
    window_start = now - LOGIN_WINDOW_SECONDS
    key = _login_bucket_key(ip, username)
    r = get_redis()

    try:
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        results = await pipe.execute()
        count = int(results[1])
    except Exception:
        log.exception("login_ratelimit_backend_error", ip=ip, username=username)
        return True, 0

    if count >= LOGIN_MAX_ATTEMPTS:
        try:
            oldest = await r.zrange(key, 0, 0, withscores=True)
        except Exception:
            oldest = []
        if oldest:
            _, oldest_score = oldest[0]
            retry_after = max(1, int(oldest_score + LOGIN_WINDOW_SECONDS - now))
        else:
            retry_after = LOGIN_WINDOW_SECONDS
        return False, retry_after

    # Record this attempt. Use a unique member (score + random nonce) so
    # simultaneous attempts don't collapse into a single ZSET member.
    member = f"{now}:{secrets.token_hex(4)}"
    try:
        pipe = r.pipeline()
        pipe.zadd(key, {member: now})
        pipe.expire(key, LOGIN_WINDOW_SECONDS)
        await pipe.execute()
    except Exception:
        log.exception("login_ratelimit_record_failed", ip=ip, username=username)
    return True, 0


async def reset_login_attempts(ip: str, username: str) -> None:
    """Wipe the sliding window after a successful login so the same user
    isn't locked out later by their earlier typos."""
    try:
        await get_redis().delete(_login_bucket_key(ip, username))
    except Exception:
        log.exception("login_ratelimit_reset_failed", ip=ip, username=username)
