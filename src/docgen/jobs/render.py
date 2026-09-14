"""Async render actor — the deterministic pipeline off the request thread.

Never imports providers/ or designtime/mapper.py: the render path is provably data-blind
of the AI layer at the module-boundary level.

# tenant-audit: file-allow (worker actor — job_id in the queue message came
# from an already-authenticated /render call that wrote the row with the
# caller's tenant; the actor trusts the queue-side scoping and looks up by
# primary key).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import dramatiq
import structlog
from sqlalchemy.exc import IntegrityError

from docgen.config import get_settings
from docgen.core.merge import RenderError
from docgen.core.render_pipeline import execute_merge, persist_result
from docgen.core.render_stack import render_stack_hash
from docgen.core.schema_infer import validate_payload
from docgen.jobs.broker import _broker  # noqa: F401 — ensure broker registered
from docgen.metrics import observe_render
from docgen.store.db import sync_session
from docgen.store.models import Provenance, RenderJob, TemplateVersion


def _duration_seconds(job: RenderJob) -> float:
    """Wall-clock seconds from submit to now — used to feed the render
    latency histogram. Falls back to 0.0 if `submitted_at` is missing so
    we still record the terminal-state counter tick either way."""
    if job.submitted_at is None:
        return 0.0
    return (datetime.now(UTC) - job.submitted_at).total_seconds()

# States the actor treats as terminal — it must never overwrite them, even on
# retry. Without this guard, a retry of a job that already succeeded would
# flip state back to "running" and lose the completed_at + result_blob_key.
_TERMINAL_STATES = frozenset({"succeeded", "failed"})

log = structlog.get_logger(__name__)


@dramatiq.actor(max_retries=1, time_limit=15 * 60 * 1000)
def render_job(job_id: str) -> None:
    jid = uuid.UUID(job_id)
    settings = get_settings()

    with sync_session() as session:
        job = session.get(RenderJob, jid)
        if job is None:
            return
        # Idempotency guard: never re-process a job that already reached a
        # terminal state. Dramatiq can redeliver on broker hiccups; without
        # this guard the actor would overwrite a completed row's state and
        # duplicate the Provenance write.
        if job.state in _TERMINAL_STATES:
            log.info(
                "render_actor_skip_terminal",
                job_id=str(job.id),
                state=job.state,
            )
            return
        tv = session.get(TemplateVersion, job.template_version_id)
        if tv is None or not tv.is_frozen:
            job.state = "failed"
            job.error = "Template version missing or not frozen"
            job.completed_at = datetime.now(UTC)
            session.commit()
            return

        job.state = "running"
        session.commit()

        try:
            ok, errors = validate_payload(tv.schema, job.payload)
            if not ok:
                raise RenderError("Payload validation failed: " + "; ".join(errors))

            output = execute_merge(tv, job.payload, job.output_format, settings=settings)
            result_key = persist_result(tv, job.id, output, job.output_format, settings=settings)

            job.result_blob_key = result_key
            job.result_bytes = len(output)
            job.state = "succeeded"
            job.completed_at = datetime.now(UTC)

            session.add(
                Provenance(
                    render_job_id=job.id,
                    template_version_id=tv.id,
                    version_hash=tv.version_hash,
                    render_stack_hash=render_stack_hash(),
                    caller=job.caller,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                # A prior attempt already wrote provenance for this job.
                # Discard the duplicate row and keep the succeeded state.
                session.rollback()
                job = session.get(RenderJob, jid)
                if job is not None and job.state != "succeeded":
                    job.result_blob_key = result_key
                    job.result_bytes = len(output)
                    job.state = "succeeded"
                    job.completed_at = datetime.now(UTC)
                    session.commit()
            log.info(
                "render_succeeded",
                job_id=str(job.id),
                template_id=str(tv.template_id),
                tenant_id=str(job.tenant_id) if job.tenant_id else None,
                version=tv.version,
                version_hash=tv.version_hash,
                mode="async",
                format=job.output_format,
                result_bytes=job.result_bytes,
                caller=job.caller,
            )
            observe_render(
                tenant_id=str(job.tenant_id) if job.tenant_id else None,
                format=job.output_format,
                state="succeeded",
                duration_seconds=_duration_seconds(job),
            )
            _maybe_enqueue_webhook(job.id, job.callback_url)
        except Exception as e:
            job.state = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.completed_at = datetime.now(UTC)
            session.commit()
            log.error(
                "render_failed",
                job_id=str(job.id),
                template_id=str(tv.template_id) if tv else None,
                tenant_id=str(job.tenant_id) if job.tenant_id else None,
                version=tv.version if tv else None,
                mode="async",
                format=job.output_format,
                error=job.error,
            )
            observe_render(
                tenant_id=str(job.tenant_id) if job.tenant_id else None,
                format=job.output_format,
                state="failed",
                duration_seconds=_duration_seconds(job),
            )
            _maybe_enqueue_webhook(job.id, job.callback_url)
            raise


def _maybe_enqueue_webhook(job_id, callback_url: str | None) -> None:
    if not callback_url:
        return
    try:
        # Local import: keeps this module's static imports narrow (see tests/test_boundaries).
        from docgen.jobs.webhooks import deliver_webhook

        deliver_webhook.send(str(job_id))
    except Exception:
        log.exception("webhook_enqueue_failed", job_id=str(job_id))
