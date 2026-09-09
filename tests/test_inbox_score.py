"""Score model for the inbox agent: label half (50) + trial half (50).

The formula lives in `docgen.inbox.agent._score`; behavior:
  - freeze failure → 0 regardless of coverage or trials
  - otherwise: 50 * (labeled_ai / total) + min(50, 10 * trials_passed)
  - a trial passes when its verdict LLM returned passed=True. render_error
    counts as a failing trial (verdict is set to False in that case).
"""
from __future__ import annotations

from docgen.core.schema_infer import FieldSpec
from docgen.inbox.agent import _score
from docgen.inbox.test_executor import ExecutedTrial
from docgen.providers.base import MappingProposal, Suggestion


def _proposal(ai: int, orphan: int) -> MappingProposal:
    def _one(name: str, source: str) -> Suggestion:
        return Suggestion(
            placeholder=name,
            jinja="{{ " + name + " }}",
            field=FieldSpec(name=name, type="string"),
            source=source,
        )

    suggestions = [_one(f"a{i}", "ai") for i in range(ai)] + [
        _one(f"o{i}", "orphan_backfill") for i in range(orphan)
    ]
    return MappingProposal(suggestions=suggestions)


def _trials(pass_count: int, fail_count: int = 0) -> list[ExecutedTrial]:
    def _one(i: int, passed: bool) -> ExecutedTrial:
        return ExecutedTrial(
            ordinal=i,
            name=f"trial-{i}",
            description="",
            payload={},
            rendered=None,
            ext="pdf",
            render_error=None if passed else "boom",
            passed=passed,
            verdict="",
            blob_key=None,
        )

    trials = [_one(i, True) for i in range(pass_count)]
    trials += [_one(pass_count + i, False) for i in range(fail_count)]
    return trials


def test_full_labeling_and_all_trials_pass_scores_100():
    p = _proposal(ai=5, orphan=0)
    assert _score(p, freeze_ok=True, executed_trials=_trials(5)) == 100.0


def test_full_labeling_no_trials_scores_50():
    p = _proposal(ai=5, orphan=0)
    assert _score(p, freeze_ok=True, executed_trials=[]) == 50.0


def test_full_labeling_three_trials_pass_scores_80():
    p = _proposal(ai=5, orphan=0)
    assert _score(p, freeze_ok=True, executed_trials=_trials(3, 2)) == 80.0


def test_partial_labeling_all_trials_pass_scores_87_5():
    # 3/4 labeled → 37.5, all 5 trials pass → 50, total 87.5
    p = _proposal(ai=3, orphan=1)
    assert _score(p, freeze_ok=True, executed_trials=_trials(5)) == 87.5


def test_freeze_failure_returns_zero():
    p = _proposal(ai=5, orphan=0)
    assert _score(p, freeze_ok=False, executed_trials=_trials(5)) == 0.0


def test_empty_proposal_no_trials_is_zero():
    p = _proposal(ai=0, orphan=0)
    assert _score(p, freeze_ok=True, executed_trials=[]) == 0.0


def test_trial_score_caps_at_fifty_with_more_than_five_passes():
    # Belt-and-suspenders: if the matrix ever produces more than 5 trials,
    # trial credit still caps at 50 so total can't exceed 100.
    p = _proposal(ai=5, orphan=0)
    assert _score(p, freeze_ok=True, executed_trials=_trials(7)) == 100.0
