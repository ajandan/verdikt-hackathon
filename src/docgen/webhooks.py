"""Webhook delivery helpers — signing, event body building.

The Dramatiq actor lives in `docgen.jobs.webhooks` (sync/redis-broker land);
this module holds only pure helpers so the auth/webhook boundary is clean
and easy to reason about from tests.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from docgen.config import get_settings


def sign_body(body: bytes, *, secret: str | None = None) -> str:
    """HMAC-SHA256 hex digest of `body` using the configured signing secret."""
    key = (secret or get_settings().webhook_signing_secret).encode("utf-8")
    mac = hmac.new(key, body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_body(
    *,
    job_id: str,
    state: str,
    result_bytes: int | None,
    error: str | None,
    output_format: str | None,
    template_id: str | None,
    version: int | None,
) -> bytes:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "state": state,
        "result_bytes": result_bytes,
        "error": error,
        "output_format": output_format,
        "template_id": template_id,
        "version": version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def event_for_state(state: str) -> str:
    if state == "succeeded":
        return "render.succeeded"
    if state == "failed":
        return "render.failed"
    return f"render.{state}"
