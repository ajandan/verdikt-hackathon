"""Watchdog: mark jobs stuck in state='running' past the actor time-limit as failed.

A worker crash (SIGKILL, OOM, container evicted) leaves the RenderJob row
mid-flight with `state='running'`. Nothing in the actor path recovers this,
so callers see the job stuck indefinitely. This module sweeps such rows on
worker boot (via a Dramatiq middleware) so a restart automatically clears
the graveyard.

Only "running" jobs older than `STALE_RUNNING_AFTER_SECONDS` are reaped —
comfortably longer than the actor's `time_limit` (15 min) so an in-flight
long render never gets clobbered mid-run.

# tenant-audit: file-allow (cross-tenant maintenance sweep — reaps orphaned
# 'running' rows for every tenant on worker boot; scoping by tenant would
# defeat the purpose).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from dramatiq.middleware import Middleware
from sqlalchemy import update

from docgen.store.db import sync_session
from docgen.store.models import RenderJob

log = structlog.get_logger(__name__)

# Actor time_limit is 15 min; add margin so we never race an in-flight retry.
STALE_RUNNING_AFTER_SECONDS = 30 * 60


def reap_stale_running_jobs() -> int:
    """Mark jobs stuck in 'running' past the threshold as 'failed'. Returns count."""
    cutoff = datetime.now(UTC) - timedelta(seconds=STALE_RUNNING_AFTER_SECONDS)
    now = datetime.now(UTC)
    with sync_session() as s:
        result = s.execute(
            update(RenderJob)
            .where(RenderJob.state == "running", RenderJob.submitted_at < cutoff)
            .values(state="failed", error="worker_timeout", completed_at=now)
        )
        s.commit()
    n = result.rowcount or 0
    if n:
        log.warning("reaped_stale_running_jobs", count=n, cutoff_seconds=STALE_RUNNING_AFTER_SECONDS)
    return n


class StaleJobReaperMiddleware(Middleware):
    """Run the reaper once per worker boot."""

    def after_worker_boot(self, broker, worker) -> None:
        try:
            reap_stale_running_jobs()
        except Exception:
            log.exception("reap_stale_running_jobs_failed")
