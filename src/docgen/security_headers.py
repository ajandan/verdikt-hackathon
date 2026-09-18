"""HTTP security headers + effective-secure-cookie helper (roadmap §3).

`SecurityHeadersMiddleware` stamps every response with a hardened header set:
- CSP with `frame-ancestors 'none'` (clickjacking) and `default-src 'self'`.
- HSTS when the request is HTTPS or the operator has forced it on (behind a
  TLS-terminating proxy).
- `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and a minimal
  `Permissions-Policy` locking down camera / microphone / geolocation.

The `secure_cookie_flag(request)` helper is the source of truth for the `Secure`
attribute on session + CSRF cookies. It trusts `X-Forwarded-Proto` when the
proxy sits in front (belt-and-braces: also honours the config toggle).

Docs served by FastAPI (/docs, /redoc) get a slightly relaxed CSP because
Swagger UI + ReDoc load their own CDN assets. Static files, OpenAPI JSON, and
health are covered by the base policy.
"""
from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from docgen.config import get_settings


# Per-request nonces bind <script>/<style> tags to a fresh token so an XSS
# injection that lands raw markup can't run inline JS or drop styles. The
# templates emit `nonce="{{ csp_nonce(request) }}"` on every legitimate block,
# so the browser only executes/applies those.
#
# 'unsafe-inline' stays for now as a legacy fallback: the admin UI still uses
# inline event handlers (onclick=, onchange=), which nonces don't cover. In
# CSP3 browsers the presence of a nonce on script-src *disables* 'unsafe-inline'
# for scripts anyway — so modern browsers already enforce the nonce policy.
# Full retirement waits on migrating inline handlers to addEventListener.
def _base_csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "img-src 'self' data:",
        "font-src 'self' https://rsms.me data:",
        f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline' https://rsms.me",
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-inline'",
        "connect-src 'self'",
    ])

# Public marketing page at `/`. Same tight rules as base, but allows Google
# Fonts (stylesheet + woff2) and YouTube's privacy-enhanced embed for the
# hackathon demo video. No scripts, no XHR, no images beyond self+data.
# The demo card plays a locally-hosted MP4 from /static, hence media-src 'self'.
def landing_csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "img-src 'self' data:",
        "media-src 'self'",
        "font-src 'self' https://fonts.gstatic.com data:",
        f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline' https://fonts.googleapis.com",
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-inline'",
        "connect-src 'self'",
    ])


# Swagger UI + ReDoc load their bundle from jsdelivr / unpkg by default.
_DOCS_CSP = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "img-src 'self' data: https://fastapi.tiangolo.com",
    "font-src 'self' https://cdn.jsdelivr.net data:",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "worker-src 'self' blob:",
    "connect-src 'self'",
])

# In-browser .docx preview loads jszip + docx-preview from unpkg. docx-preview
# extracts embedded images into blob: URLs so <img src="blob:..."> must be
# permitted too. The preview page never has user input — this CSP is scoped
# to a single admin-only route (`/ui/versions/{id}/preview.html`).
def preview_csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        "base-uri 'self'",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        f"style-src 'self' 'nonce-{nonce}'",
        f"script-src 'self' 'nonce-{nonce}' https://unpkg.com",
        "connect-src 'self'",
    ])

_HSTS = "max-age=31536000; includeSubDomains"


# Pre-generated static reports under /engineering can't carry per-request
# nonces on their inline <style>/<script> tags. In CSP3 browsers, the mere
# presence of a nonce on script-src/style-src disables 'unsafe-inline' — so
# without a matching nonce every inline block is blocked and the dashboard
# renders unstyled. Use a nonce-free CSP that relies on 'unsafe-inline'.
# rsms.me is the Inter font CDN the dashboards use.
#
# SECURITY BOUNDARY: /engineering/* is mounted as :class:`fastapi.staticfiles.StaticFiles`
# in `docgen.main` — no template rendering, no query-string interpolation, no
# user-input path lands in these files. Adding ANY dynamic route under the
# /engineering prefix would break that assumption; every future engineering
# route must go through the base or preview CSP (both nonce-locked) instead.
# frame-src / worker-src / connect-src are locked to 'none'/'self' so an
# XSS foothold on a report page can't spin up a worker, embed iframes, or
# exfiltrate via fetch to an off-origin endpoint.
_ENGINEERING_CSP = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "worker-src 'none'",
    "object-src 'none'",
    "img-src 'self' data:",
    "font-src 'self' https://rsms.me https://fonts.gstatic.com data:",
    "style-src 'self' 'unsafe-inline' https://rsms.me https://fonts.googleapis.com",
    "script-src 'self' 'unsafe-inline'",
    "connect-src 'self'",
])


def _is_docs_path(path: str) -> bool:
    return path in {"/docs", "/redoc", "/docs/oauth2-redirect"} or path.startswith("/docs/")


def _is_landing_path(path: str) -> bool:
    return path == "/"


def _is_engineering_path(path: str) -> bool:
    return path == "/engineering" or path.startswith("/engineering/")


def _request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def secure_cookie_flag(request: Request) -> bool:
    """Effective value for the `Secure` cookie attribute.

    True when the incoming request is HTTPS (directly or per X-Forwarded-Proto),
    or when the operator has pinned it on via `DOCGEN_SECURE_COOKIES=true`.
    """
    if _request_is_https(request):
        return True
    return get_settings().secure_cookies


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp CSP + HSTS + friends on every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # 16 bytes → 22 URL-safe base64 chars, ample for CSP3 nonce entropy.
        # Attached before the downstream handler runs so Jinja templates can
        # read it via the `csp_nonce` global.
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        headers = response.headers

        # CSP: relaxed for /docs + /redoc, landing (fonts only) for /, base otherwise.
        path = request.url.path
        if _is_docs_path(path):
            policy = _DOCS_CSP
        elif _is_engineering_path(path):
            policy = _ENGINEERING_CSP
        elif _is_landing_path(path):
            policy = landing_csp(request.state.csp_nonce)
        else:
            policy = _base_csp(request.state.csp_nonce)
        headers.setdefault("content-security-policy", policy)
        headers.setdefault("x-content-type-options", "nosniff")
        # X-Frame-Options is a legacy backstop; frame-ancestors in CSP is the
        # modern equivalent. Cheap to send both.
        headers.setdefault("x-frame-options", "DENY")
        headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "permissions-policy",
            "camera=(), microphone=(), geolocation=(), interest-cohort=()",
        )

        settings = get_settings()
        if _request_is_https(request) or settings.hsts_enabled:
            headers.setdefault("strict-transport-security", _HSTS)

        return response
