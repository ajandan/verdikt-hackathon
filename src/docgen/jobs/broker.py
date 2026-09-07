"""Dramatiq broker — Redis. Imported by anything that enqueues or consumes actors."""
from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from docgen.config import get_settings
from docgen.jobs.watchdog import StaleJobReaperMiddleware

_broker = RedisBroker(url=get_settings().redis_url)
# Run stale-job reaper on worker boot so a restart clears jobs left in
# 'running' by a previous crashed worker.
_broker.add_middleware(StaleJobReaperMiddleware())
dramatiq.set_broker(_broker)
