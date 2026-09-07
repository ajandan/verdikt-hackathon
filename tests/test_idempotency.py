"""POST /render honors Idempotency-Key per (tenant, key).

- Same key + same payload → same job_id (no duplicate render).
- Same key + different payload → 409.
- No key → normal behavior (each call creates a new job).
- Key longer than 255 chars → 400.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.main import app
from docgen.providers.base import MappingProposal
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import IdempotencyKey, Provenance, RenderJob


def _seed_frozen_template(default_tenant, sample_docx_bytes) -> uuid.UUID:
    async def _prep() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                template, version, _report = await upload_template(
                    s,
                    name=f"idem-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                tv = await freeze_version(
                    s, version_id=version.id, proposal=MappingProposal(suggestions=[])
                )
                template.current_version = tv.version
                await s.commit()
                return template.id
        finally:
            await async_engine.dispose()

    return asyncio.run(_prep())


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


def test_same_key_same_payload_returns_same_job(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    tid = _seed_frozen_template(default_tenant, sample_docx_bytes)
    key = f"test-{uuid.uuid4().hex}"
    body = _render_payload(tid)

    with TestClient(app) as client:
        r1 = client.post("/render", json=body, headers={**auth_headers, "Idempotency-Key": key})
        r2 = client.post("/render", json=body, headers={**auth_headers, "Idempotency-Key": key})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["job_id"] == r2.json()["job_id"]

    # Exactly one row in idempotency_keys for this key.
    with sync_session() as s:
        rows = s.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key)
        ).scalars().all()
        assert len(rows) == 1


def test_same_key_different_payload_returns_409(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    tid = _seed_frozen_template(default_tenant, sample_docx_bytes)
    key = f"test-{uuid.uuid4().hex}"
    body1 = _render_payload(tid)
    body2 = _render_payload(tid)
    body2["data"]["MEMBER_NAME"] = "Somebody Else"

    with TestClient(app) as client:
        r1 = client.post("/render", json=body1, headers={**auth_headers, "Idempotency-Key": key})
        r2 = client.post("/render", json=body2, headers={**auth_headers, "Idempotency-Key": key})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 409, r2.text
    assert "different payload" in r2.json()["detail"].lower()


def test_no_key_creates_independent_jobs(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    tid = _seed_frozen_template(default_tenant, sample_docx_bytes)
    body = _render_payload(tid)

    with TestClient(app) as client:
        r1 = client.post("/render", json=body, headers=auth_headers)
        r2 = client.post("/render", json=body, headers=auth_headers)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["job_id"] != r2.json()["job_id"]


def test_oversized_key_rejected_as_400(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    tid = _seed_frozen_template(default_tenant, sample_docx_bytes)
    body = _render_payload(tid)
    bad_key = "x" * 300

    with TestClient(app) as client:
        r = client.post("/render", json=body, headers={**auth_headers, "Idempotency-Key": bad_key})
    assert r.status_code == 400
    assert "255" in r.json()["detail"]


def test_key_scoped_per_tenant(auth_headers, default_tenant, sample_docx_bytes) -> None:
    """Two tenants using the same key string must not collide."""
    from docgen.auth import generate_api_key, hash_api_key
    from docgen.store.models import ApiKey, Tenant

    tid = _seed_frozen_template(default_tenant, sample_docx_bytes)
    body = _render_payload(tid)
    key = f"shared-{uuid.uuid4().hex[:8]}"

    # Second tenant with its own template.
    other_tenant = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=other_tenant, name=f"idem-other-{other_tenant.hex[:6]}"))
        s.flush()
        raw, prefix, digest = generate_api_key()
        s.add(ApiKey(tenant_id=other_tenant, name="other", key_prefix=prefix, key_hash=digest))
        s.commit()
    other_headers = {"Authorization": f"Bearer {raw}"}
    other_tid = _seed_frozen_template(other_tenant, sample_docx_bytes)
    other_body = _render_payload(other_tid)

    try:
        with TestClient(app) as client:
            r1 = client.post(
                "/render", json=body, headers={**auth_headers, "Idempotency-Key": key}
            )
            r2 = client.post(
                "/render",
                json=other_body,
                headers={**other_headers, "Idempotency-Key": key},
            )
        # Both succeed with distinct jobs — the key namespace is per-tenant.
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["job_id"] != r2.json()["job_id"]
    finally:
        # Template.tenant_id has no ondelete=CASCADE, so we have to drop
        # templates + jobs first, then the api key + tenant.
        from sqlalchemy import delete

        from docgen.store.models import Template as _T
        with sync_session() as s:
            # Provenance and IdempotencyKey FKs to render_jobs are ondelete=RESTRICT,
            # so drop dependent rows for this tenant's jobs before the jobs themselves.
            # Templates cascade their versions.
            other_job_ids = s.execute(
                select(RenderJob.id).where(RenderJob.tenant_id == other_tenant)
            ).scalars().all()
            if other_job_ids:
                s.execute(delete(Provenance).where(Provenance.render_job_id.in_(other_job_ids)))
            s.execute(delete(IdempotencyKey).where(IdempotencyKey.tenant_id == other_tenant))
            s.execute(delete(RenderJob).where(RenderJob.tenant_id == other_tenant))
            s.execute(delete(_T).where(_T.tenant_id == other_tenant))
            k = s.execute(
                select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw))
            ).scalar_one_or_none()
            if k:
                s.delete(k)
            t = s.get(Tenant, other_tenant)
            if t:
                s.delete(t)
            s.commit()
