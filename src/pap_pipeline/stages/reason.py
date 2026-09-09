"""Stage 3 — eligibility reasoning.

Calls Nemotron-Super with the extracted intake and the retrieved rules and
returns a structured Decision with citations back to the rules that fired.

JSON-mode call so the output validates against Decision without a free-text
parse step. Nemotron-Super's non-reasoning mode is preferred here — the
Nano/reasoning variant burns output tokens on the trace and returns empty
content on this task shape (documented in config.ai_verdict_model rationale).
"""
from __future__ import annotations

from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule


async def reason(intake: Intake, rules: list[Rule]) -> Decision:
    """Reason over the intake against retrieved rules; emit a Decision."""
    raise NotImplementedError("Phase 2: wire Nemotron-Super JSON-mode call.")
