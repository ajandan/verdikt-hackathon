"""Score model for the inbox agent: ai_coverage × freeze_ok × render_ok.

The formula lives in `docgen.inbox.agent._score`; behavior:
  - freeze failure → 0 regardless of coverage
  - render failure → cap at 50 (freeze succeeded, but downstream broke)
  - otherwise 100 * ai / total
"""
from __future__ import annotations

from docgen.core.schema_infer import FieldSpec
from docgen.inbox.agent import _score
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


def test_full_ai_coverage_max_score():
    p = _proposal(ai=5, orphan=0)
    assert _score(p, render_ok=True, freeze_ok=True) == 100.0


def test_partial_coverage():
    p = _proposal(ai=3, orphan=1)
    assert _score(p, render_ok=True, freeze_ok=True) == 75.0


def test_freeze_failure_returns_zero():
    p = _proposal(ai=5, orphan=0)
    assert _score(p, render_ok=True, freeze_ok=False) == 0.0


def test_render_failure_caps_at_fifty():
    p = _proposal(ai=5, orphan=0)
    # 100% ai coverage but render broke → cap
    assert _score(p, render_ok=False, freeze_ok=True) == 50.0


def test_render_failure_below_cap_stays_below():
    p = _proposal(ai=1, orphan=3)  # 25%
    assert _score(p, render_ok=False, freeze_ok=True) == 25.0


def test_empty_proposal_is_zero():
    p = _proposal(ai=0, orphan=0)
    assert _score(p, render_ok=True, freeze_ok=True) == 0.0
