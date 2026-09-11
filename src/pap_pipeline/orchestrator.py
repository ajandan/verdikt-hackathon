"""Composes the three pipeline stages into a single Decision.

Called by docgen.inbox.agent when the pipeline backend is enabled for a
tenant. Returns a canonical Decision (approve / deny / missing_info) that
the inbox agent uses to select which reply template to render.

Design shape:

  attachments ── extract ──▶ Intake
                              │
                              ├── retrieve ──▶ [RetrievedRule] (rule + score)
                              │
                              └── reason(intake, rules) ──▶ Decision

The retriever's reranker score is stitched back into the Decision citations
after reasoning — the reason stage doesn't see the scores (they're not
inputs to its verdict) but the reply letter uses them to order citations by
confidence.

Stage 1 (extract) is not yet wired; :func:`run_from_intake` is the current
entry point, taking an already-extracted :class:`Intake`. When Stage 1
lands, :func:`run` will add the extract call on top and become the primary
entry point.
"""
from __future__ import annotations

from dataclasses import dataclass

from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages.extract import ExtractClientConfig, extract
from pap_pipeline.stages.reason import ReasonClientConfig, reason
from pap_pipeline.stages.retrieve import RetrieveClientConfig, retrieve


@dataclass(frozen=True)
class PipelineConfig:
    """Aggregate config for the wired stages.

    Grouped so the orchestrator has a single config seam and callers don't
    hand-thread three individual configs through the stack.
    """

    extract: ExtractClientConfig
    retrieve: RetrieveClientConfig
    reason: ReasonClientConfig


async def run_from_intake(
    intake: Intake,
    *,
    config: PipelineConfig,
    program: str | None = None,
    k: int = 4,
) -> Decision:
    """Run retrieve → reason against an already-extracted Intake.

    This is the "middle-slice" entry point used by tests, by the demo
    script that feeds `load_canonical_intake()`, and (until Stage 1 lands)
    by the inbox agent when the extract stage is being stubbed.
    """
    retrieved = await retrieve(
        intake, config=config.retrieve, program=program, k=k
    )
    rules = [rr.rule for rr in retrieved]
    decision = await reason(intake, rules, config=config.reason)
    return _attach_retrieval_scores(decision, retrieved)


def _attach_retrieval_scores(decision: Decision, retrieved) -> Decision:
    """Fold the reranker's per-rule score back onto Decision.citations.

    The reason stage leaves `score=None` because the scores aren't in its
    input; they're a retrieval artifact. The letter renderer orders
    citations by score to show the strongest rules first, so the
    orchestrator stitches them in as the pipeline's final act.
    """
    scores = {rr.rule.id: rr.score for rr in retrieved}
    if not scores:
        return decision
    return decision.model_copy(
        update={
            "citations": [
                (
                    c.model_copy(update={"score": scores[c.rule_id]})
                    if c.rule_id in scores and c.score is None
                    else c
                )
                for c in decision.citations
            ]
        }
    )


async def run(
    attachments: list[bytes],
    *,
    config: PipelineConfig,
    program: str | None = None,
    k: int = 4,
) -> Decision:
    """Full pipeline — attachments in, Decision out.

    Composes extract → retrieve → reason and stitches the reranker's scores
    onto the Decision's citations. The inbox agent calls this once per
    inbound message when `use_pipeline_backend` is enabled for the tenant.
    """
    _, decision = await run_with_intake(
        attachments, config=config, program=program, k=k
    )
    return decision


async def run_with_intake(
    attachments: list[bytes],
    *,
    config: PipelineConfig,
    program: str | None = None,
    k: int = 4,
) -> tuple[Intake, Decision]:
    """Same as :func:`run` but also returns the extracted Intake.

    The reply-letter renderer needs both — Decision picks the template and
    fills the outcome-specific fields; Intake fills the addressee, DOB,
    medication, and other patient-scoped fields that aren't part of the
    Decision but must appear on every letter.
    """
    intake = await extract(attachments, config=config.extract)
    decision = await run_from_intake(intake, config=config, program=program, k=k)
    return intake, decision


__all__ = ["PipelineConfig", "Citation", "run", "run_from_intake", "run_with_intake"]
