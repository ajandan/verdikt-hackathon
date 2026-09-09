"""Composes the three pipeline stages into a single Decision.

Called by docgen.inbox.agent when the pipeline backend is enabled for a
tenant. Returns a canonical Decision (approve / deny / missing_info) that the
inbox agent uses to select which reply template to render.

Phase 2 responsibilities:
  - Accept an InboundMessage (docgen.inbox.imap_poller.InboundMessage)
  - Call stages.extract → stages.retrieve → stages.reason in order
  - Wrap each call in the routing.py instrumentation
  - Return a Decision + evidence trail
"""
from __future__ import annotations

from pap_pipeline.schemas.decision import Decision


async def run(message: object) -> Decision:
    """Run extract → retrieve → reason and return a Decision.

    Signature will firm up in Phase 2 once the InboundMessage → Intake
    boundary is stabilized. Kept `object` here so the module imports cleanly
    before docgen types are pulled in.
    """
    raise NotImplementedError("Phase 2: compose the three stages.")
