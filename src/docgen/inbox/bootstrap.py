"""Lightweight middleware for wiring the inbox poller into worker boot.

Kept in its own module (rather than `inbox/job.py`) so `docgen.jobs.broker`
can register the middleware without eagerly importing the whole agent
graph — the actor + orchestrator are pulled in lazily inside
`after_worker_boot`, which only runs in the worker process.
"""
from __future__ import annotations

import structlog
from dramatiq.middleware import Middleware

log = structlog.get_logger(__name__)


class InboxPollerBootstrapMiddleware(Middleware):
    """Kick off the first `poll_inbox` tick when the worker boots.

    No-op when the inbox config is incomplete so a fresh deploy that
    hasn't wired credentials up yet doesn't crash-loop.
    """

    def after_worker_boot(self, broker, worker) -> None:
        try:
            # Lazy import: this module is loaded by the broker at
            # process boot (web + worker + tests); the actor + agent
            # only need to exist inside the worker process.
            from docgen.inbox.job import bootstrap_inbox_poller

            bootstrap_inbox_poller()
        except Exception:
            log.exception("inbox_bootstrap_failed")
