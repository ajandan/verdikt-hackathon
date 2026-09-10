"""Dynamic plugin loader — dot-path modules from the DOCGEN_PLUGINS env var.

Kept deliberately dumb so the boundary test (docgen never statically imports
pap_pipeline) still holds. Plugin modules register themselves at import time
against slots exposed by docgen (e.g. `docgen.inbox.agent.set_pap_handler`).

Set `DOCGEN_PLUGINS=pap_pipeline.inbox_hook` at deploy time to activate the
PAP pipeline route; leave unset and the ingest flow is pure docgen.
"""
from __future__ import annotations

import importlib
import os

import structlog

log = structlog.get_logger(__name__)


def load_plugins(env_var: str = "DOCGEN_PLUGINS") -> list[str]:
    """Import each comma-separated module in the env var. Returns names loaded."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    loaded: list[str] = []
    for name in (n.strip() for n in raw.split(",") if n.strip()):
        try:
            importlib.import_module(name)
        except Exception:
            log.exception("plugin_load_failed", plugin=name)
            continue
        log.info("plugin_loaded", plugin=name)
        loaded.append(name)
    return loaded


__all__ = ["load_plugins"]
