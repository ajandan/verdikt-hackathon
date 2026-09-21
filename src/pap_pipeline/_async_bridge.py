"""Sync → async bridge for the pipeline's sync-entry-point callers.

Same behavior as :func:`asyncio.run` under threadpool-executor
callers, but creates the loop explicitly so the "no ambient loop"
constraint is documented rather than assumed. Compatible with
FastAPI's ``run_in_threadpool`` and the IMAP-poller actor; not
compatible with running inside an already-active event loop on the
same thread (callers in that position should await directly).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


def run_pipeline_sync(coro: Awaitable[_T]) -> _T:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


__all__ = ["run_pipeline_sync"]
