"""Dramatiq actor: deliver webhook callback to `job.callback_url` on terminal state.

Runs in the worker. Retries with exponential backoff (Dramatiq) up to
`settings.webhook_max_attempts - 1` extra tries. On each attempt, bumps
`webhook_attempts`. On terminal failure, marks `webhook_state=failed`;
on 2xx, marks `webhook_state=delivered`.

# tenant-audit: file-allow (worker actor — job_id comes from a trusted queue
# message enqueued by the render actor for a specific tenant's job; all
# updates target that same row by primary key).
"""
from __future__ import annotations

import uuid

import dramatiq
import httpx
import structlog

from docgen.config import get_settings
from docgen.jobs.broker import _broker  # noqa: F401 — ensure broker registered
from docgen.net_safety import UrlNotAllowed, validate_public_url
from docgen.store.db import sync_session
from docgen.store.models import RenderJob, TemplateVersion
from docgen.webhooks import build_body, event_for_state, sign_body

log = structlog.get_logger(__name__)

# Captured once at import so the decorator value and the runtime check can
# never disagree. Dramatiq bakes `max_retries` into the actor at decoration
# time, so re-reading settings later would silently drift.
_MAX_RETRIES = max(0, get_settings().webhook_max_attempts - 1)


@dramatiq.actor(max_retries=_MAX_RETRIES, min_backoff=1000, time_limit=60 * 1000)
def deliver_webhook(job_id: str) -> None:
    settings = get_settings()
    jid = uuid.UUID(job_id)

    with sync_session() as session:
        job = session.get(RenderJob, jid)
        if job is None or not job.callback_url:
            return
        tv = session.get(TemplateVersion, job.template_version_id)
        template_id = None
        version = None
        if tv is not None:
            template_id = str(tv.template_id)
            version = tv.version
        body = build_body(
            job_id=str(job.id),
            state=job.state,
            result_bytes=job.result_bytes,
            error=job.error,
            output_format=job.output_format,
            template_id=template_id,
            version=version,
        )
        signature = sign_body(body, secret=settings.webhook_signing_secret)
        event = event_for_state(job.state)
        delivery_id = uuid.uuid4().hex
        callback_url = job.callback_url

        job.webhook_attempts = (job.webhook_attempts or 0) + 1
        job.webhook_state = "delivering"
        session.commit()

    headers = {
        "Content-Type": "application/json",
        "X-DocGen-Signature": signature,
        "X-DocGen-Delivery": delivery_id,
        "X-DocGen-Event": event,
    }
    log.info(
        "webhook_attempt",
        job_id=job_id,
        callback_url=callback_url,
        event_name=event,
        delivery_id=delivery_id,
    )

    def _is_last_attempt() -> bool:
        try:
            msg = dramatiq.middleware.CurrentMessage.get_current_message()
            if msg is None:
                return False
            retries = int(msg.options.get("retries", 0))
            # Prefer the message's own ceiling (what dramatiq actually enforces)
            # and fall back to the actor's compile-time constant.
            ceiling = int(msg.options.get("max_retries", _MAX_RETRIES))
            return retries >= ceiling
        except Exception:
            return False

    # Re-validate at delivery time: guards against DNS rebinding between
    # enqueue and delivery, and catches URLs inserted through paths that
    # bypass the API boundary (e.g., direct DB writes).
    try:
        validate_public_url(callback_url)
    except UrlNotAllowed as e:
        _record_failure(jid, f"url_rejected: {e}", terminal=True)
        log.warning(
            "webhook_url_rejected",
            job_id=job_id,
            callback_url=callback_url,
            reason=str(e),
        )
        return

    try:
        with httpx.Client(
            timeout=settings.webhook_timeout_seconds, follow_redirects=False
        ) as client:
            resp = client.post(callback_url, content=body, headers=headers)
    except Exception as e:  # network / timeout
        _record_failure(jid, f"{type(e).__name__}: {e}", terminal=_is_last_attempt())
        raise

    if 200 <= resp.status_code < 300:
        with sync_session() as session:
            job = session.get(RenderJob, jid)
            if job is not None:
                job.webhook_state = "delivered"
                job.webhook_last_error = None
                session.commit()
        log.info(
            "webhook_delivered",
            job_id=job_id,
            status=resp.status_code,
            delivery_id=delivery_id,
        )
        return

    err = f"HTTP {resp.status_code}"
    _record_failure(jid, err, terminal=_is_last_attempt())
    log.warning("webhook_non_2xx", job_id=job_id, status=resp.status_code)
    raise RuntimeError(f"webhook non-2xx: {resp.status_code}")


def _record_failure(job_id: uuid.UUID, err: str, *, terminal: bool) -> None:
    with sync_session() as session:
        job = session.get(RenderJob, job_id)
        if job is None:
            return
        job.webhook_last_error = err
        if terminal:
            job.webhook_state = "failed"
        else:
            job.webhook_state = "retrying"
        session.commit()
