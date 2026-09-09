"""Stage 2 — rule retrieval.

Calls the NeMo Retriever embed + rerank NIMs against a pgvector table of
eligibility rules. Returns the top-k rules that apply to the given intake,
each with the reranker's relevance score.
"""
from __future__ import annotations

from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule


async def retrieve(intake: Intake, k: int = 4) -> list[Rule]:
    """Retrieve the top-k eligibility rules relevant to this intake."""
    raise NotImplementedError("Phase 2: wire NeMo Retriever NIMs + pgvector.")
