"""Smoke tests for the standalone preflight_job entry point.

These tests exercise the same code path a Nebius Serverless Job container
would run — no DB session, no blob storage, no HTTP layer. The provider
is pinned to `stub` so CI stays deterministic and offline-safe.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document

from docgen.designtime.preflight_job import run_preflight_and_label


def _docx_with(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_run_preflight_and_label_returns_expected_shape() -> None:
    docx = _docx_with([
        "Dear [MEMBER_NAME],",
        "Your claim [CLAIM_ID] filed on <<claim_date>> for [AMOUNT] was denied.",
    ])

    result = await run_preflight_and_label(
        template_bytes=docx,
        kind="docx",
        provider_config={"provider": "stub"},
    )

    assert set(result) >= {
        "kind",
        "template_hash",
        "elapsed_seconds",
        "provider",
        "preflight",
        "mapping",
    }
    assert result["kind"] == "docx"
    assert result["provider"] == "stub"
    assert len(result["template_hash"]) == 64  # sha256 hex
    assert result["preflight"]["outcome"] == "accept"

    names = {s["field"]["name"] for s in result["mapping"]["suggestions"]}
    assert names == {"member_name", "claim_id", "claim_date", "amount"}


@pytest.mark.asyncio
async def test_pdf_kind_not_yet_supported() -> None:
    with pytest.raises(NotImplementedError):
        await run_preflight_and_label(
            template_bytes=b"%PDF-",
            kind="pdf",
            provider_config={"provider": "stub"},
        )


def test_cli_writes_result_json(tmp_path: Path) -> None:
    """End-to-end: shell out to the module CLI just like a Serverless Job would."""
    docx_path = tmp_path / "sample.docx"
    docx_path.write_bytes(_docx_with(["Hello [FIRST_NAME]!"]))

    cfg_path = tmp_path / "provider.json"
    cfg_path.write_text(json.dumps({"provider": "stub"}), encoding="utf-8")

    out_path = tmp_path / "result.json"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "docgen.designtime.preflight_job",
            "--input",
            str(docx_path),
            "--out",
            str(out_path),
            "--provider-config",
            str(cfg_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["provider"] == "stub"
    assert result["preflight"]["outcome"] == "accept"
    names = {s["field"]["name"] for s in result["mapping"]["suggestions"]}
    assert names == {"first_name"}
