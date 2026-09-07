"""Dramatiq worker entrypoint — runs the render actor pool."""
from __future__ import annotations

import os
import sys


def run() -> None:
    # dramatiq CLI is the supported way to launch workers; exec it with our modules.
    argv = ["dramatiq", "docgen.jobs.broker", "docgen.jobs.render", *sys.argv[1:]]
    os.execvp("dramatiq", argv)
