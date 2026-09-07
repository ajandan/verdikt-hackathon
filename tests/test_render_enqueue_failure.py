"""When the render broker is unreachable, /render must mark the job failed
and return 503 — not leave an orphan `queued` row.

Covers the enqueue-failure branch of `_execute_render`, which is easy to miss
under normal test conditions because Redis is always up.
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
from docgen.store.models import RenderJob


def test_enqueue_failure_marks_job_failed_and_returns_503(
    monkeypatch, auth_headers, default_tenant, sample_docx_bytes
) -> None:
    async def _prep() -> tuple[uuid.UUID, uuid.UUID]:
        try:
            async with AsyncSessionLocal() as s:
                template, version, _report = await upload_template(
                    s,
                    name=f"enqfail-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                tv = await freeze_version(
                    s, version_id=version.id, proposal=MappingProposal(suggestions=[])
                )
                # Mark it as the current version so /render can find it.
                template.current_version = tv.version
                await s.commit()
                return template.id, tv.id
        finally:
            await async_engine.dispose()

    template_id, _tv_id = asyncio.run(_prep())

    # Force async path AND make send() raise.
    from docgen.jobs import render as render_module

    def _boom(_msg: str) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(render_module.render_job, "send", _boom)

    with TestClient(app) as client:
        r = client.post(
            "/render",
            json={
                "template_id": str(template_id),
                "data": {
                    "MEMBER_NAME": "Alice",
                    "CLAIM_ID": "C1",
                    "CLAIM_DATE": "2026-01-01",
                    "AMOUNT": 10,
                    "agent_name": "Bob",
                },
                "async": True,
            },
            headers=auth_headers,
        )

    assert r.status_code == 503, r.text
    assert "broker" in r.json()["detail"].lower()

    # The row must exist and be marked failed with a helpful error prefix.
    with sync_session() as s:
        rows = s.execute(
            select(RenderJob)
            .where(RenderJob.template_version_id == _tv_id)
            .order_by(RenderJob.submitted_at.desc())
        ).scalars().all()
        assert rows, "expected a RenderJob row from the failed enqueue"
        job = rows[0]
        assert job.state == "failed"
        assert job.error and job.error.startswith("enqueue_failed:")
        assert job.completed_at is not None
