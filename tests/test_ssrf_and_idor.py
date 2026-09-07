"""End-to-end regression tests for the SSRF and IDOR fixes.

Covers:
  * `/render` rejects a `callback_url` pointing at a private/loopback host
    with 422 before the RenderJob row is written.
  * `deliver_webhook` re-validates at delivery time and marks the job
    `webhook_state=failed` without issuing an HTTP request.
  * `/ui/jobs/{id}/result` returns 404 when the sidebar workspace cookie
    scopes to a different tenant than the job's tenant.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.auth import sign_session
from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.main import app
from docgen.providers.base import MappingProposal
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import RenderJob, Tenant

# ---------------------------------------------------------------------------
# SSRF — API boundary
# ---------------------------------------------------------------------------

def _seed_frozen_template(default_tenant, sample_docx_bytes) -> uuid.UUID:
    async def _prep() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                template, version, _report = await upload_template(
                    s,
                    name=f"ssrf-{uuid.uuid4().hex[:6]}",
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


def test_render_rejects_loopback_callback_url(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    template_id = _seed_frozen_template(default_tenant, sample_docx_bytes)

    with TestClient(app) as client:
        r = client.post(
            "/render",
            json={
                "template_id": str(template_id),
                "data": {
                    "MEMBER_NAME": "Alice", "CLAIM_ID": "C1", "CLAIM_DATE": "2026-01-01",
                    "AMOUNT": 10, "agent_name": "Bob",
                },
                "async": True,
                "callback_url": "http://127.0.0.1/hook",
            },
            headers=auth_headers,
        )

    assert r.status_code == 422, r.text
    assert "callback_url" in r.json()["detail"]

    # No RenderJob row should have been created.
    with sync_session() as s:
        rows = s.execute(
            select(RenderJob).where(RenderJob.tenant_id == default_tenant)
        ).scalars().all()
        assert all(row.callback_url != "http://127.0.0.1/hook" for row in rows)


def test_render_rejects_link_local_metadata_url(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    template_id = _seed_frozen_template(default_tenant, sample_docx_bytes)

    with TestClient(app) as client:
        r = client.post(
            "/render",
            json={
                "template_id": str(template_id),
                "data": {
                    "MEMBER_NAME": "Alice", "CLAIM_ID": "C1", "CLAIM_DATE": "2026-01-01",
                    "AMOUNT": 10, "agent_name": "Bob",
                },
                "async": True,
                "callback_url": "http://169.254.169.254/latest/meta-data/",
            },
            headers=auth_headers,
        )

    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# SSRF — worker re-validates at delivery time
# ---------------------------------------------------------------------------

def test_deliver_webhook_marks_failed_on_private_url_without_posting(
    monkeypatch, default_tenant, sample_docx_bytes
) -> None:
    """Belt-and-suspenders: even if a bad URL made it to the DB (direct write,
    schema drift, etc.), the worker must refuse to POST and mark it failed."""
    async def _prep() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                _template, version, _report = await upload_template(
                    s, name=f"wh-ssrf-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes, tenant_id=default_tenant,
                )
                job = RenderJob(
                    tenant_id=default_tenant,
                    template_version_id=version.id,
                    state="succeeded",
                    payload={},
                    output_format="docx",
                    result_bytes=1024,
                    callback_url="http://10.0.0.5/hook",  # RFC1918
                    webhook_state="pending",
                )
                s.add(job)
                await s.commit()
                await s.refresh(job)
                return job.id
        finally:
            await async_engine.dispose()

    job_id = asyncio.run(_prep())

    posts: list[dict] = []

    class _ExplodingClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content, headers):
            posts.append({"url": url})
            raise AssertionError("worker must not POST to a private URL")

    import docgen.jobs.webhooks as wh
    monkeypatch.setattr(wh.httpx, "Client", _ExplodingClient)

    wh.deliver_webhook.fn(str(job_id))

    # No HTTP request should have been attempted.
    assert posts == []

    with sync_session() as s:
        job = s.get(RenderJob, job_id)
        assert job.webhook_state == "failed"
        assert job.webhook_last_error and "url_rejected" in job.webhook_last_error


# ---------------------------------------------------------------------------
# IDOR — UI download honors sidebar workspace scope
# ---------------------------------------------------------------------------

def _admin_client_with_scope(scope_tenant_id: uuid.UUID) -> TestClient:
    client = TestClient(app)
    client.cookies.set("docgen_session", sign_session("admin"))
    client.cookies.set("docgen_ui_tenant", str(scope_tenant_id))
    return client


def test_ui_job_download_rejects_cross_tenant_when_scoped(
    default_tenant, sample_docx_bytes
) -> None:
    """With the sidebar picked to Tenant B, requesting Tenant A's job returns 404."""
    async def _prep() -> tuple[uuid.UUID, uuid.UUID]:
        try:
            async with AsyncSessionLocal() as s:
                # A second tenant that is different from default_tenant.
                other_id = uuid.uuid4()
                s.add(Tenant(id=other_id, name=f"other-{other_id.hex[:6]}", ai_provider_config={}))
                await s.flush()

                _template, version, _report = await upload_template(
                    s, name=f"idor-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes, tenant_id=default_tenant,
                )
                job = RenderJob(
                    tenant_id=default_tenant,  # job belongs to Tenant A
                    template_version_id=version.id,
                    state="succeeded",
                    payload={},
                    output_format="docx",
                    result_bytes=1024,
                    result_blob_key="does/not/matter/for/this/test.docx",
                    webhook_state="not_configured",
                )
                s.add(job)
                await s.commit()
                await s.refresh(job)
                return job.id, other_id
        finally:
            await async_engine.dispose()

    job_id, other_tenant_id = asyncio.run(_prep())

    # Sidebar scope = Tenant B (other). Job belongs to Tenant A (default).
    with _admin_client_with_scope(other_tenant_id) as client:
        r = client.get(f"/ui/jobs/{job_id}/result")
    assert r.status_code == 404, r.text


def test_ui_job_download_allows_matching_scope(
    monkeypatch, default_tenant, sample_docx_bytes
) -> None:
    """When the sidebar scope matches the job's tenant, the scope check passes
    (the actual 200 vs 409 outcome depends on blob availability, not scope)."""
    async def _prep() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                _template, version, _report = await upload_template(
                    s, name=f"scope-match-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes, tenant_id=default_tenant,
                )
                # State = "queued" so we hit the 409 branch after the scope
                # check — proving scope let us through without needing S3.
                job = RenderJob(
                    tenant_id=default_tenant,
                    template_version_id=version.id,
                    state="queued",
                    payload={},
                    output_format="docx",
                    webhook_state="not_configured",
                )
                s.add(job)
                await s.commit()
                await s.refresh(job)
                return job.id
        finally:
            await async_engine.dispose()

    job_id = asyncio.run(_prep())

    with _admin_client_with_scope(default_tenant) as client:
        r = client.get(f"/ui/jobs/{job_id}/result")
    # 409 = scope passed, blob-not-ready branch. Anything but 404 proves scope OK.
    assert r.status_code == 409, r.text
