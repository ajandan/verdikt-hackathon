"""Sync ↔ async bridge for the pipeline's sync-entry-point callers.

Both :mod:`pap_pipeline.inbox_hook` (email intake) and
:mod:`pap_pipeline.api_hook` (JSON intake) run the async pipeline
from a synchronous caller — the FastAPI handlers wrap the sync
function in ``run_in_threadpool``, and the IMAP-poller actor is
plain sync. Both used to call :func:`asyncio.run` directly, which
raises ``RuntimeError: asyncio.run() cannot be called from a running
event loop`` if the caller ever happens to have an ambient loop
(future ``anyio`` port, in-process test harness, background task
scheduled inside a request handler).

This helper creates a fresh event loop, runs the coroutine to
completion, and closes the loop, without asserting that no ambient
loop exists on the caller's thread. Same behavior as ``asyncio.run``
in the current deployment (threadpool-executor threads have no
ambient loop), but explicit about intent — the constraint is
"caller must not itself be running inside a loop on this thread,"
not "no loop exists anywhere in the process."

The loop is closed on exit even when the coroutine raises so we
don't leak per-call event loops on the pipeline's failure paths.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


def run_pipeline_sync(coro: Awaitable[_T]) -> _T:
    """Run an async pipeline coroutine to completion from sync code.

    Creates a dedicated event loop for this call, uses it to drive
    the coroutine, and closes it in a ``finally`` block. Compatible
    with FastAPI's ``run_in_threadpool`` (the thread has no ambient
    loop) and the sync IMAP-poller actor (same).

    Not compatible with running inside an already-active event loop
    on the same thread — that's the same limitation as
    ``asyncio.run`` and is a deliberate choice: if a caller is
    already inside a loop, it should ``await`` the underlying
    coroutine directly rather than bridge through here.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


__all__ = ["run_pipeline_sync"]
