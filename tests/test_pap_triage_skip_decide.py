"""Orchestrator conditional skip-Decide — when Triage flips decision_needed
off, the pipeline shouldn't call Retrieve/Reason and should emit a stub
Decision that names the target route on ``decision.triage.route``.
"""
from __future__ import annotations

import pytest

from pap_pipeline.orchestrator import PipelineConfig, run_with_intake
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig
from pap_pipeline.stages.triage import TriageConfig


def _config(*, triage: TriageConfig | None) -> PipelineConfig:
    return PipelineConfig(
        extract=ExtractClientConfig(base_url="http://x", api_key="k", vision_model="m"),
        retrieve=RetrieveClientConfig(base_url="http://x", api_key="k", embed_model="e"),
        reason=ReasonClientConfig(base_url="http://x", api_key="k", model="m"),
        triage=triage,
    )


@pytest.mark.asyncio
async def test_decision_needed_false_skips_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    called_run_from_intake = [False]

    async def _fake_extract(attachments, *, config, slots=None):
        return {"patient_name": "Devon", "family_name": "Okafor"}

    async def _fake_run_from_intake(intake, **kwargs):
        called_run_from_intake[0] = True
        from pap_pipeline.schemas.decision import Decision
        return Decision(outcome="approve", rationale="should not be called")

    from pap_pipeline import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "extract", _fake_extract)
    monkeypatch.setattr(orch_mod, "run_from_intake", _fake_run_from_intake)

    triage_cfg = TriageConfig(enabled=True)
    _, _, decision = await run_with_intake(
        [b""], config=_config(triage=triage_cfg), subject="Refill request",
    )

    assert called_run_from_intake[0] is False
    assert decision.outcome == "missing_info"
    assert "Decide skipped" in decision.rationale
    assert decision.triage is not None
    assert decision.triage.case_type == "refill"
    assert decision.triage.route == "pharmacy_direct"


@pytest.mark.asyncio
async def test_decision_needed_true_runs_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    called_run_from_intake = [False]

    async def _fake_extract(attachments, *, config, slots=None):
        return {"patient_name": "Devon", "family_name": "Okafor",
                "prescribed_drug": "Cardiovita"}

    async def _fake_run_from_intake(intake, **kwargs):
        called_run_from_intake[0] = True
        from pap_pipeline.schemas.decision import Decision
        return Decision(outcome="approve", rationale="real verdict")

    from pap_pipeline import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "extract", _fake_extract)
    monkeypatch.setattr(orch_mod, "run_from_intake", _fake_run_from_intake)

    triage_cfg = TriageConfig(enabled=True)
    _, _, decision = await run_with_intake(
        [b""], config=_config(triage=triage_cfg),
    )

    assert called_run_from_intake[0] is True
    assert decision.outcome == "approve"
    assert decision.rationale == "real verdict"
    assert decision.triage is not None
    assert decision.triage.decision_needed is True


@pytest.mark.asyncio
async def test_triage_none_preserves_pre_triage_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_run_from_intake = [False]

    async def _fake_extract(attachments, *, config, slots=None):
        return {"patient_name": "Devon"}

    async def _fake_run_from_intake(intake, **kwargs):
        called_run_from_intake[0] = True
        from pap_pipeline.schemas.decision import Decision
        return Decision(outcome="approve", rationale="pre-triage path")

    from pap_pipeline import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "extract", _fake_extract)
    monkeypatch.setattr(orch_mod, "run_from_intake", _fake_run_from_intake)

    _, _, decision = await run_with_intake([b""], config=_config(triage=None))

    assert called_run_from_intake[0] is True
    assert decision.outcome == "approve"
    assert decision.triage is None
