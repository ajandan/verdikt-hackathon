"""Verdict webhook delivery for the hybrid intake contract.

Fires when a reviewer approves or rejects a case that arrived over the
REST/MCP channel with a ``callback_url``. Payload is the final Decision
dict plus enough case-metadata for the caller to correlate against the
original ``queued`` response.

Signing follows the same pattern as the render-job webhook
(``docgen.webhooks.sign_body``): HMAC-SHA256 hex of the exact request
body, keyed with ``settings.webhook_signing_secret``, sent as
``X-Verdikt-Signature: sha256=<hex>``.

Delivery is a single blocking attempt with a short timeout. The demo
doesn't need dramatiq-backed retries — the important thing is that
``callback_status`` on the case row records the outcome so the reviewer
can see whether the caller got the verdict. Retryable failures land in
``callback_last_error`` and support can re-fire manually.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from docgen.config import Settings
from docgen.store.db import sync_session
from docgen.store.models import InboxEvent
from docgen.webhooks import sign_body

log = structlog.get_logger(__name__)


def deliver_verdict_webhook(
    event_id: uuid.UUID,
    settings: Settings,
    *,
    reviewer_action: str,
    reviewer_actor: str,
) -> tuple[bool, str | None]:
    """POST the final verdict to the case's ``callback_url``.

    Uses its own sync session (mirrors the render webhook actor) so the
    caller's request session isn't held open across the HTTP round-trip.
    Loads the event row fresh, bumps ``callback_attempts``, records the
    outcome on the row, and returns ``(delivered, error_message)`` for
    the caller to surface in the UI flash.

    ``reviewer_action`` is ``"approved"`` or ``"rejected"``. That's what
    the caller uses to decide whether ``decision.outcome`` is the final
    answer or should be flipped to a denial regardless.

    No-ops when the event has no ``callback_url`` or when it's already in
    a terminal callback state — safe to call unconditionally from the
    approve/reject handlers.
    """
    with sync_session() as session:
        ev = session.get(InboxEvent, event_id)
        if ev is None:
            return False, "event not found"
        if not ev.callback_url:
            return False, None  # no webhook configured; not an error
        if ev.callback_status in ("delivered", "failed"):
            return False, None  # already terminal; caller shouldn't refire

        body = _build_body(ev, reviewer_action=reviewer_action, reviewer_actor=reviewer_actor)
        signature = sign_body(body, secret=settings.webhook_signing_secret)
        callback_url = ev.callback_url

        ev.callback_attempts = (ev.callback_attempts or 0) + 1
        session.commit()

    headers = {
        "Content-Type": "application/json",
        "X-Verdikt-Signature": signature,
        "X-Verdikt-Event": f"verdict.{reviewer_action}",
        "X-Verdikt-Delivery": uuid.uuid4().hex,
    }

    log.info(
        "verdict_webhook_attempt",
        event_id=str(event_id),
        callback_url=callback_url,
        reviewer_action=reviewer_action,
    )

    delivered = False
    error: str | None = None
    try:
        with httpx.Client(
            timeout=settings.webhook_timeout_seconds, follow_redirects=False
        ) as client:
            resp = client.post(callback_url, content=body, headers=headers)
        if 200 <= resp.status_code < 300:
            delivered = True
        else:
            error = f"HTTP {resp.status_code}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    with sync_session() as session:
        ev = session.get(InboxEvent, event_id)
        if ev is None:
            return delivered, error
        if delivered:
            ev.callback_status = "delivered"
            ev.callback_delivered_at = datetime.now(UTC)
            ev.callback_last_error = None
        else:
            ev.callback_status = "failed"
            ev.callback_last_error = error
        session.commit()

    if delivered:
        log.info("verdict_webhook_delivered", event_id=str(event_id))
    else:
        log.warning(
            "verdict_webhook_failed", event_id=str(event_id), error=error
        )

    return delivered, error


def _build_body(
    ev: InboxEvent,
    *,
    reviewer_action: str,
    reviewer_actor: str,
) -> bytes:
    """JSON body for the verdict webhook.

    Deterministic key order + no whitespace so the signature is stable
    regardless of dict iteration order — mirrors the render-webhook
    convention so receivers can share the verification helper.
    """
    decision: dict[str, Any] = dict(ev.pap_decision or {})
    intake: dict[str, Any] = dict(ev.pap_intake or {})
    payload: dict[str, Any] = {
        "case_id": ev.case_id,
        "event_id": str(ev.id),
        "reviewer_action": reviewer_action,
        "reviewer_actor": reviewer_actor,
        "review_state": ev.review_state,
        "outcome": decision.get("outcome"),
        "decision": decision,
        "intake": intake,
        "delivered_at": datetime.now(UTC).isoformat(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = ["deliver_verdict_webhook"]
