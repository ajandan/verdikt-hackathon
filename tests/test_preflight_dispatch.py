"""Cross-check that inline and subprocess backends return equivalent output.

The subprocess backend mirrors the Nebius Serverless Job image contract,
so if the two paths agree here they'll agree once the Job runs remotely
too. Stub provider is pinned so this stays deterministic in CI.
"""
from __future__ import annotations

import io

import pytest
from docx import Document

from docgen.designtime.preflight_dispatch import (
    PreflightBackendError,
    dispatch_preflight_and_label,
)


def _docx_with(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_inline_returns_expected_mapping() -> None:
    docx = _docx_with(["Dear [MEMBER_NAME], claim [CLAIM_ID] for [AMOUNT]."])
    proposal, result = await dispatch_preflight_and_label(
        template_bytes=docx,
        provider_config={"provider": "stub"},
        backend="inline",
    )
    names = {s.field.name for s in proposal.suggestions}
    assert names == {"member_name", "claim_id", "amount"}
    assert result["provider"] == "stub"
    assert result["preflight"]["outcome"] == "accept"


@pytest.mark.asyncio
async def test_subprocess_matches_inline() -> None:
    docx = _docx_with([
        "Dear [MEMBER_NAME],",
        "Your claim [CLAIM_ID] filed on <<claim_date>> for [AMOUNT] was denied.",
    ])
    cfg = {"provider": "stub"}

    inline_proposal, inline_result = await dispatch_preflight_and_label(
        template_bytes=docx, provider_config=cfg, backend="inline",
    )
    sub_proposal, sub_result = await dispatch_preflight_and_label(
        template_bytes=docx, provider_config=cfg, backend="subprocess",
    )

    # Field detection must be identical — same code path, different process boundary.
    assert (
        {s.field.name for s in inline_proposal.suggestions}
        == {s.field.name for s in sub_proposal.suggestions}
    )
    # Preflight outcome + provider identity must agree.
    assert inline_result["preflight"]["outcome"] == sub_result["preflight"]["outcome"]
    assert inline_result["provider"] == sub_result["provider"] == "stub"
    # Template hash is deterministic — proves both backends see the same bytes.
    assert inline_result["template_hash"] == sub_result["template_hash"]


@pytest.mark.asyncio
async def test_nebius_job_backend_not_yet_implemented() -> None:
    with pytest.raises(PreflightBackendError, match="not yet implemented"):
        await dispatch_preflight_and_label(
            template_bytes=b"unused",
            provider_config={"provider": "stub"},
            backend="nebius_job",
        )
