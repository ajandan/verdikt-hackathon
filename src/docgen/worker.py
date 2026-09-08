"""Dramatiq worker entrypoint — runs the render actor pool."""
from __future__ import annotations

import os
import sys


def run() -> None:
    # dramatiq CLI is the supported way to launch workers; exec it with our modules.
    # Extra modules on the tail are imported by the worker to register their
    # actors — no side effects when the actor's `send()` is never called.
    argv = [
        "dramatiq",
        "docgen.jobs.broker",
        "docgen.jobs.render",
        "docgen.jobs.webhooks",
        "docgen.inbox.job",
        *sys.argv[1:],
    ]
    os.execvp("dramatiq", argv)
