"""MCP render_template auto-derives an idempotency key from the request
fingerprint so retries return the original job (no orphaned PDFs)."""
from __future__ import annotations

import asyncio

from docgen.auth import _tenant_id
from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.mcp_server import render_template
from docgen.providers.base import FieldSpec, MappingProposal, Suggestion
from docgen.store.db import AsyncSessionLocal, async_engine


def _proposal_for_sample() -> MappingProposal:
    return MappingProposal(suggestions=[
        Suggestion(
            placeholder="[MEMBER_NAME]", jinja="{{ member_name }}",
            field=FieldSpec(name="member_name", type="string", required=True),
        ),
        Suggestion(
            placeholder="[CLAIM_ID]", jinja="{{ claim_id }}",
            field=FieldSpec(name="claim_id", type="string", required=True),
        ),
        Suggestion(
            placeholder="[CLAIM_DATE]", jinja="{{ claim_date }}",
            field=FieldSpec(name="claim_date", type="date", required=True),
        ),
        Suggestion(
            placeholder="[AMOUNT]", jinja="{{ amount }}",
            field=FieldSpec(name="amount", type="number", required=True),
        ),
    ])


def _run(coro):
    async def _wrap():
        try:
            return await coro
        finally:
            await async_engine.dispose()
    return asyncio.run(_wrap())


def test_render_template_replays_identical_calls(default_tenant, sample_docx_bytes) -> None:
    async def _go():
        # render_template resolves tenant from the ContextVar the auth
        # middleware normally sets — replicate that here.
        tok = _tenant_id.set(str(default_tenant))
        try:
            async with AsyncSessionLocal() as s:
                _, base, _ = await upload_template(
                    s, name="mcp-idem-a", docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                frozen = await freeze_version(
                    s, version_id=base.id, proposal=_proposal_for_sample(),
                )
                template_id = str(frozen.template_id)

            data = {
                "member_name": "Ada",
                "claim_id": "C-1",
                "claim_date": "2026-01-01",
                "amount": 100,
            }
            # Freeze no longer auto-publishes (see designtime.freeze); pin
            # to version=1 so render_template doesn't need current_version.
            first = await render_template(template_id=template_id, data=data, version=1)
            second = await render_template(template_id=template_id, data=data, version=1)
            return first, second
        finally:
            _tenant_id.reset(tok)

    first, second = _run(_go())
    assert first["job_id"] == second["job_id"]
    assert first.get("replayed") is not True
    assert second.get("replayed") is True
    # Fetch URL is regenerated (new token each call) but points at the same job.
    assert first["job_id"] in second["fetch_url"]


def test_render_template_replays_fresh_when_blob_gone(
    default_tenant, sample_docx_bytes, monkeypatch,
) -> None:
    """If the idempotency row survives but the blob was pruned, the tool
    kicks off a fresh render instead of returning a fetch_url pointing at a
    404. New job_id, `replayed` is not set."""
    async def _go():
        tok = _tenant_id.set(str(default_tenant))
        try:
            async with AsyncSessionLocal() as s:
                _, base, _ = await upload_template(
                    s, name="mcp-idem-c", docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                frozen = await freeze_version(
                    s, version_id=base.id, proposal=_proposal_for_sample(),
                )
                template_id = str(frozen.template_id)

            data = {
                "member_name": "Ada",
                "claim_id": "C-1",
                "claim_date": "2026-01-01",
                "amount": 100,
            }
            first = await render_template(template_id=template_id, data=data, version=1)

            # Simulate the blob having been garbage-collected out from under
            # the idempotency mapping. head_object() returns False → the tool
            # should fall through to a fresh render.
            from docgen import mcp_server as _mcp
            monkeypatch.setattr(_mcp, "head_object", lambda bucket, key: False)

            second = await render_template(template_id=template_id, data=data, version=1)
            return first, second
        finally:
            _tenant_id.reset(tok)

    first, second = _run(_go())
    assert first["job_id"] != second["job_id"]
    assert second.get("replayed") is not True


def test_render_template_different_payload_produces_new_job(
    default_tenant, sample_docx_bytes,
) -> None:
    async def _go():
        tok = _tenant_id.set(str(default_tenant))
        try:
            async with AsyncSessionLocal() as s:
                _, base, _ = await upload_template(
                    s, name="mcp-idem-b", docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                frozen = await freeze_version(
                    s, version_id=base.id, proposal=_proposal_for_sample(),
                )
                template_id = str(frozen.template_id)

            base_data = {
                "member_name": "Ada",
                "claim_id": "C-1",
                "claim_date": "2026-01-01",
                "amount": 100,
            }
            r1 = await render_template(template_id=template_id, data=base_data, version=1)
            # Change a single field — different fingerprint → fresh job.
            r2 = await render_template(
                template_id=template_id,
                data={**base_data, "amount": 200},
                version=1,
            )
            return r1, r2
        finally:
            _tenant_id.reset(tok)

    r1, r2 = _run(_go())
    assert r1["job_id"] != r2["job_id"]
    assert r2.get("replayed") is not True
