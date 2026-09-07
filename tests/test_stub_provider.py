from __future__ import annotations

import pytest

from docgen.providers.stub import StubProvider


@pytest.mark.asyncio
async def test_stub_matches_bracketed_and_angle() -> None:
    text = "Dear [MEMBER_NAME], claim [CLAIM_ID] on <<claim_date>> for [AMOUNT] denied."
    proposal = await StubProvider().propose_mapping(text)
    names = {s.field.name for s in proposal.suggestions}
    assert names == {"member_name", "claim_id", "claim_date", "amount"}
    for s in proposal.suggestions:
        if s.field.name == "amount":
            assert s.field.type == "number"
        if s.field.name == "claim_date":
            assert s.field.type == "date"


@pytest.mark.asyncio
async def test_stub_dedupes_repeated_placeholders() -> None:
    text = "[MEMBER_NAME], hi [MEMBER_NAME], welcome [MEMBER_NAME]."
    proposal = await StubProvider().propose_mapping(text)
    assert len(proposal.suggestions) == 1
    assert proposal.suggestions[0].field.name == "member_name"


@pytest.mark.asyncio
async def test_stub_emits_reasoning_for_every_suggestion() -> None:
    """Reviewers see the deterministic trace even in offline/stub-only demos."""
    text = "Dear [MEMBER_NAME], claim <<claim_date>> for [AMOUNT] on [COUNT]."
    proposal = await StubProvider().propose_mapping(text)
    for s in proposal.suggestions:
        assert s.reasoning, f"missing reasoning for {s.field.name}"
    by_name = {s.field.name: s.reasoning for s in proposal.suggestions}
    assert "date" in by_name["claim_date"].lower()
    assert "currency" in by_name["amount"].lower() or "amount" in by_name["amount"].lower()
    assert "count" in by_name["count"].lower() or "quantity" in by_name["count"].lower()
