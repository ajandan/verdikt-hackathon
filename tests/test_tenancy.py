"""Cross-tenant isolation: templates uploaded by tenant A must be invisible to tenant B."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.auth import generate_api_key
from docgen.designtime.upload import upload_template
from docgen.main import app
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import ApiKey, Tenant


@pytest.fixture
def two_tenants_with_keys():
    """Create tenant A + tenant B, each with one API key. Yield (a_id, a_key, b_id, b_key)."""
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    a_raw, a_prefix, a_hash = generate_api_key()
    b_raw, b_prefix, b_hash = generate_api_key()

    with sync_session() as s:
        s.add(Tenant(id=a_id, name=f"tenant-a-{a_id.hex[:8]}"))
        s.add(Tenant(id=b_id, name=f"tenant-b-{b_id.hex[:8]}"))
        s.flush()
        s.add(ApiKey(tenant_id=a_id, name="a", key_prefix=a_prefix, key_hash=a_hash))
        s.add(ApiKey(tenant_id=b_id, name="b", key_prefix=b_prefix, key_hash=b_hash))
        s.commit()

    yield (a_id, a_raw, b_id, b_raw)

    with sync_session() as s:
        for h in (a_hash, b_hash):
            row = s.execute(select(ApiKey).where(ApiKey.key_hash == h)).scalar_one_or_none()
            if row is not None:
                s.delete(row)
        s.commit()


def _upload_sync(tenant_id: uuid.UUID, docx_bytes: bytes, name: str) -> uuid.UUID:
    """Run the async upload_template in a fresh event loop; dispose the async pool after."""
    async def _run() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                template, _v, _r = await upload_template(
                    s, name=name, docx_bytes=docx_bytes, tenant_id=tenant_id
                )
                return template.id
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())


def test_tenant_b_cannot_see_tenant_a_template(two_tenants_with_keys, sample_docx_bytes) -> None:
    a_id, a_raw, _b_id, b_raw = two_tenants_with_keys
    tid = _upload_sync(a_id, sample_docx_bytes, "A-only template")

    # Both requests share one TestClient — the MCP session manager (bound to
    # the module-level `mcp` singleton mounted on `app`) rejects a second
    # `.run()` on the same instance, so we can't open two client contexts
    # against the same app in one test.
    with TestClient(app) as client:
        r_b = client.get(
            f"/templates/{tid}/provenance", headers={"Authorization": f"Bearer {b_raw}"}
        )
        r_a = client.get(
            f"/templates/{tid}/provenance", headers={"Authorization": f"Bearer {a_raw}"}
        )
    assert r_b.status_code == 404
    assert r_a.status_code == 200
    assert r_a.json() == []


def test_tenant_b_cannot_get_schema_of_tenant_a_template(
    two_tenants_with_keys, sample_docx_bytes
) -> None:
    a_id, _a_raw, _b_id, b_raw = two_tenants_with_keys
    tid = _upload_sync(a_id, sample_docx_bytes, "A schema")
    with TestClient(app) as client:
        r = client.get(
            f"/templates/{tid}/schema", headers={"Authorization": f"Bearer {b_raw}"}
        )
    assert r.status_code == 404
