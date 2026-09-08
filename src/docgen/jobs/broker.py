"""Dramatiq broker — Redis. Imported by anything that enqueues or consumes actors."""
from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from docgen.config import get_settings
from docgen.inbox.bootstrap import InboxPollerBootstrapMiddleware
from docgen.jobs.watchdog import StaleJobReaperMiddleware

_broker = RedisBroker(url=get_settings().redis_url)
# Run stale-job reaper on worker boot so a restart clears jobs left in
# 'running' by a previous crashed worker.
_broker.add_middleware(StaleJobReaperMiddleware())
# Kick off the inbox agent's self-scheduling poller when the worker
# boots. Middleware is no-op unless DOCGEN_INBOX_ENABLED + credentials
# are set, so this is safe to leave installed globally.
_broker.add_middleware(InboxPollerBootstrapMiddleware())
dramatiq.set_broker(_broker)
