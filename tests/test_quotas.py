"""Per-tenant quota enforcement.

Covers the ``docgen.quotas`` module directly (unit-level) and the /render
API integration (429 body shape + idempotent replay still bypasses the cap).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.main import app
from docgen.providers.base import MappingProposal
from docgen.quotas import (
    QuotaExceeded,
    check_active_template_quota,
    check_render_quota,
)
from docgen.store.db import AsyncSessionLocal, async_engine, async_session, sync_session
from docgen.store.models import (
    ApiKey,
    IdempotencyKey,
    Provenance,
    RenderJob,
    Template,
    Tenant,
    TemplateVersion,
)


def _make_tenant_with_key(
    *,
    quota_renders_per_day: int | None = None,
    quota_active_templates: int | None = None,
) -> tuple[uuid.UUID, str]:
    """Fresh tenant + API key with the requested caps. Isolated from
    ``default_tenant`` so caps set in one test don't leak into others."""
    from docgen.auth import generate_api_key

    tid = uuid.uuid4()
    raw, prefix, digest = generate_api_key()
    with sync_session() as s:
        s.add(
            Tenant(
                id=tid,
                name=f"quota-{tid.hex[:6]}",
                quota_renders_per_day=quota_renders_per_day,
                quota_active_templates=quota_active_templates,
            )
        )
        s.flush()
        s.add(ApiKey(tenant_id=tid, name="q", key_prefix=prefix, key_hash=digest))
        s.commit()
    return tid, raw


def _drop_tenant(tid: uuid.UUID) -> None:
    """Tear down a tenant made by ``_make_tenant_with_key``."""
    with sync_session() as s:
        job_ids = s.execute(
            select(RenderJob.id).where(RenderJob.tenant_id == tid)
        ).scalars().all()
        if job_ids:
            s.execute(delete(Provenance).where(Provenance.render_job_id.in_(job_ids)))
        s.execute(delete(IdempotencyKey).where(IdempotencyKey.tenant_id == tid))
        s.execute(delete(RenderJob).where(RenderJob.tenant_id == tid))
        s.execute(delete(Template).where(Template.tenant_id == tid))
        s.execute(delete(ApiKey).where(ApiKey.tenant_id == tid))
        s.execute(delete(Tenant).where(Tenant.id == tid))
        s.commit()


async def _seed_frozen_template_async(tid: uuid.UUID, docx_bytes: bytes) -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        template, version, _report = await upload_template(
            s,
            name=f"quota-t-{uuid.uuid4().hex[:6]}",
            docx_bytes=docx_bytes,
            tenant_id=tid,
        )
        tv = await freeze_version(
            s, version_id=version.id, proposal=MappingProposal(suggestions=[])
        )
        template.current_version = tv.version
        await s.commit()
        return template.id


def _seed_frozen_template(tid: uuid.UUID, docx_bytes: bytes) -> uuid.UUID:
    """Sync wrapper. Async tests should call ``_seed_frozen_template_async``
    directly — nesting ``asyncio.run`` inside an already-running loop raises."""
    try:
        return asyncio.run(_seed_frozen_template_async(tid, docx_bytes))
    finally:
        asyncio.run(async_engine.dispose())


def _render_payload(template_id: uuid.UUID) -> dict:
    return {
        "template_id": str(template_id),
        "data": {
            "MEMBER_NAME": "Alice",
            "CLAIM_ID": "C1",
            "CLAIM_DATE": "2026-01-01",
            "AMOUNT": 10,
            "agent_name": "Bob",
        },
    }


# ---------------------------------------------------------------------------
# Unit — the module in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_quota_null_cap_is_noop() -> None:
    """NULL cap = no limit → the check must not raise regardless of usage."""
    tid, _ = _make_tenant_with_key(quota_renders_per_day=None)
    try:
        async with async_session() as s:
            await check_render_quota(s, tid)  # must not raise
    finally:
        _drop_tenant(tid)


@pytest.mark.asyncio
async def test_render_quota_at_cap_raises() -> None:
    """Cap reached → QuotaExceeded with the stable ``renders_per_day`` kind."""
    tid, _ = _make_tenant_with_key(quota_renders_per_day=2)
    try:
        # render_jobs.template_version_id is NOT NULL; seed a minimal stub row
        # so we can insert RenderJob rows that satisfy the FK/NOT-NULL.
        tmpl_id = uuid.uuid4()
        tv_id = uuid.uuid4()
        with sync_session() as s:
            s.add(Template(id=tmpl_id, tenant_id=tid, name="q-stub"))
            s.flush()
            s.add(
                TemplateVersion(
                    id=tv_id, template_id=tmpl_id, version=1, locale="default",
                    schema={"fields": []}, blob_key=f"seed/{tmpl_id}/v1.docx",
                    version_hash="h" * 16, status="frozen", is_frozen=True,
                )
            )
            s.commit()

        async with async_session() as s:
            # Two jobs already exist in the last 24h → we're AT the cap. The
            # third would tip us over, so check_render_quota must refuse.
            for _ in range(2):
                s.add(
                    RenderJob(
                        tenant_id=tid, template_version_id=tv_id,
                        state="succeeded", payload={},
                    )
                )
            await s.commit()

            with pytest.raises(QuotaExceeded) as exc_info:
                await check_render_quota(s, tid)
        assert exc_info.value.kind == "renders_per_day"
        assert exc_info.value.limit == 2
        assert exc_info.value.current == 2
    finally:
        _drop_tenant(tid)


@pytest.mark.asyncio
async def test_active_template_quota_null_cap_is_noop(sample_docx_bytes: bytes) -> None:
    tid, _ = _make_tenant_with_key(quota_active_templates=None)
    try:
        await _seed_frozen_template_async(tid, sample_docx_bytes)
        async with async_session() as s:
            await check_active_template_quota(s, tid)
    finally:
        _drop_tenant(tid)


@pytest.mark.asyncio
async def test_active_template_quota_at_cap_raises(sample_docx_bytes: bytes) -> None:
    tid, _ = _make_tenant_with_key(quota_active_templates=1)
    try:
        await _seed_frozen_template_async(tid, sample_docx_bytes)
        async with async_session() as s:
            with pytest.raises(QuotaExceeded) as exc_info:
                await check_active_template_quota(s, tid)
        assert exc_info.value.kind == "active_templates"
        assert exc_info.value.limit == 1
        assert exc_info.value.current == 1
    finally:
        _drop_tenant(tid)


# ---------------------------------------------------------------------------
# Integration — /render returns 429 when quota trips
# ---------------------------------------------------------------------------


def test_render_api_returns_429_when_quota_exceeded(sample_docx_bytes: bytes) -> None:
    tid, raw_key = _make_tenant_with_key(quota_renders_per_day=1)
    try:
        template_id = _seed_frozen_template(tid, sample_docx_bytes)
        headers = {"Authorization": f"Bearer {raw_key}"}
        body = _render_payload(template_id)

        with TestClient(app) as client:
            first = client.post("/render", json=body, headers=headers)
            second = client.post("/render", json=body, headers=headers)

        assert first.status_code == 200, first.text
        assert second.status_code == 429, second.text
        detail = second.json()["detail"]
        # 429 body names WHICH quota tripped and includes usage vs cap so the
        # UI can render a specific error, not a generic "rate-limited".
        assert detail["quota"] == "renders_per_day"
        assert detail["limit"] == 1
        assert detail["current"] == 1
    finally:
        _drop_tenant(tid)


def test_idempotent_replay_bypasses_quota(sample_docx_bytes: bytes) -> None:
    """A retry of an already-recorded Idempotency-Key must return the cached
    response even when the tenant is at cap — otherwise clients that safely
    retry on network failure would trip a 429 that never fired originally."""
    tid, raw_key = _make_tenant_with_key(quota_renders_per_day=1)
    try:
        template_id = _seed_frozen_template(tid, sample_docx_bytes)
        headers = {"Authorization": f"Bearer {raw_key}"}
        body = _render_payload(template_id)
        key = f"quota-replay-{uuid.uuid4().hex[:8]}"

        with TestClient(app) as client:
            first = client.post(
                "/render", json=body, headers={**headers, "Idempotency-Key": key}
            )
            # After the first, tenant is AT cap. The replay uses the same
            # Idempotency-Key so the API returns the cached job without
            # touching the quota check.
            replay = client.post(
                "/render", json=body, headers={**headers, "Idempotency-Key": key}
            )

        assert first.status_code == 200, first.text
        assert replay.status_code == 200, replay.text
        assert first.json()["job_id"] == replay.json()["job_id"]
    finally:
        _drop_tenant(tid)
