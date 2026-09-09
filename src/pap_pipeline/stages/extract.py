"""Stage 1 — intake extraction.

Calls nv-ingest (or MiniCPM-V fallback per the Phase 1 spike outcome) against
the email's attachments and returns a validated Intake object.

Backend selection reads from settings so the fallback is a config flip, not a
code change.
"""
from __future__ import annotations

from pap_pipeline.schemas.intake import Intake


async def extract(attachments: list[bytes]) -> Intake:
    """Extract a structured Intake from raw attachment bytes."""
    raise NotImplementedError("Phase 2: wire nv-ingest / MiniCPM-V backend.")
