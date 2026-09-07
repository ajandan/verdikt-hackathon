"""Render-stack fingerprint — §2 Phase 2: deterministic-output audit.

Records the exact versions of tools that participated in producing a document so
that any historical render can be reproduced (or its drift identified) from the
Provenance row alone.

`render_stack_info()` returns the raw dict; `render_stack_hash()` returns a
16-hex-char sha256 prefix suitable for indexing / equality comparisons.

Both are lru_cached: the stack cannot change during a process's lifetime.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from functools import lru_cache


def _pkg_version(name: str) -> str:
    try:
        import importlib.metadata as im

        return im.version(name)
    except Exception:
        return "unknown"


def _libreoffice_version() -> str:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return "unavailable"
    try:
        proc = subprocess.run(
            [soffice, "--version"], capture_output=True, timeout=10, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable"
    if proc.returncode != 0:
        return "unavailable"
    return proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[0]


@lru_cache
def render_stack_info() -> dict:
    return {
        "libreoffice": _libreoffice_version(),
        "python": platform.python_version(),
        "packages": {
            "docxtpl": _pkg_version("docxtpl"),
            "jinja2": _pkg_version("jinja2"),
            "python-docx": _pkg_version("python-docx"),
            "lxml": _pkg_version("lxml"),
        },
        "image_tag": os.environ.get("DOCGEN_IMAGE_TAG"),
    }


@lru_cache
def render_stack_hash() -> str:
    payload = json.dumps(render_stack_info(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
