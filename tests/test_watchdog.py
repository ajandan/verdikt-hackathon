"""Watchdog reaps jobs left in state='running' by a crashed worker."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.jobs.watchdog import STALE_RUNNING_AFTER_SECONDS, reap_stale_running_jobs
from docgen.providers.base import MappingProposal
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import RenderJob


async def _seed_tv(default_tenant, sample_docx_bytes) -> uuid.UUID:
    try:
        async with AsyncSessionLocal() as s:
            _t, v, _ = await upload_template(
                s, name=f"wd-{uuid.uuid4().hex[:6]}",
                docx_bytes=sample_docx_bytes, tenant_id=default_tenant,
            )
            tv = await freeze_version(
                s, version_id=v.id, proposal=MappingProposal(suggestions=[])
            )
            return tv.id
    finally:
        await async_engine.dispose()


def _make_job(tenant_id, tv_id, *, state: str, age_seconds: int) -> uuid.UUID:
    """Insert a job in the given state with submitted_at aged the given amount."""
    submitted = datetime.now(UTC) - timedelta(seconds=age_seconds)
    with sync_session() as s:
        j = RenderJob(
            tenant_id=tenant_id, template_version_id=tv_id, state=state,
            payload={"MEMBER_NAME": "X"}, output_format="docx", submitted_at=submitted,
        )
        s.add(j)
        s.commit()
        return j.id


def test_reaper_fails_stale_running_jobs(default_tenant, sample_docx_bytes) -> None:
    import asyncio
    tv_id = asyncio.run(_seed_tv(default_tenant, sample_docx_bytes))

    stale = _make_job(default_tenant, tv_id, state="running",
                      age_seconds=STALE_RUNNING_AFTER_SECONDS + 60)
    fresh = _make_job(default_tenant, tv_id, state="running",
                      age_seconds=60)
    queued = _make_job(default_tenant, tv_id, state="queued",
                       age_seconds=STALE_RUNNING_AFTER_SECONDS + 60)

    n = reap_stale_running_jobs()
    assert n >= 1

    with sync_session() as s:
        rows = {j.id: j for j in s.execute(
            select(RenderJob).where(RenderJob.id.in_([stale, fresh, queued]))
        ).scalars().all()}
    assert rows[stale].state == "failed"
    assert rows[stale].error == "worker_timeout"
    assert rows[stale].completed_at is not None
    assert rows[fresh].state == "running"     # too young
    assert rows[queued].state == "queued"     # not "running"
