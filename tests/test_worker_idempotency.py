"""Render actor is idempotent on redelivery.

Two protections:
1. Terminal-state guard: a redelivered message for a `succeeded` or `failed`
   job returns early without touching the row.
2. Provenance uniqueness: even if the guard is skipped somehow, the DB
   unique(render_job_id) prevents a duplicate Provenance row from being
   written; the actor catches IntegrityError and keeps the job succeeded.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.jobs.render import render_job
from docgen.providers.base import MappingProposal
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import Provenance, RenderJob


def _seed_job(default_tenant, sample_docx_bytes) -> uuid.UUID:
    async def _prep() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                _template, version, _report = await upload_template(
                    s,
                    name=f"widem-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                tv = await freeze_version(
                    s, version_id=version.id, proposal=MappingProposal(suggestions=[])
                )
                job = RenderJob(
                    tenant_id=default_tenant,
                    template_version_id=tv.id,
                    state="queued",
                    payload={
                        "MEMBER_NAME": "Alice",
                        "CLAIM_ID": "C1",
                        "CLAIM_DATE": "2026-01-01",
                        "AMOUNT": 10,
                        "agent_name": "Bob",
                    },
                    output_format="docx",
                )
                s.add(job)
                await s.commit()
                await s.refresh(job)
                return job.id
        finally:
            await async_engine.dispose()

    return asyncio.run(_prep())


def test_redelivery_after_success_is_a_noop(default_tenant, sample_docx_bytes) -> None:
    jid = _seed_job(default_tenant, sample_docx_bytes)

    # First run: real render — succeeds, writes provenance.
    render_job.fn(str(jid))

    with sync_session() as s:
        job = s.get(RenderJob, jid)
        assert job.state == "succeeded"
        completed_at_1 = job.completed_at
        result_key_1 = job.result_blob_key
        n_prov_1 = len(
            s.execute(
                select(Provenance).where(Provenance.render_job_id == jid)
            ).scalars().all()
        )
        assert n_prov_1 == 1

    # Redeliver the same message. Terminal-state guard must skip everything.
    render_job.fn(str(jid))

    with sync_session() as s:
        job = s.get(RenderJob, jid)
        assert job.state == "succeeded"
        assert job.completed_at == completed_at_1  # untouched
        assert job.result_blob_key == result_key_1
        n_prov_2 = len(
            s.execute(
                select(Provenance).where(Provenance.render_job_id == jid)
            ).scalars().all()
        )
        assert n_prov_2 == 1


def test_redelivery_after_failure_is_a_noop(default_tenant, sample_docx_bytes) -> None:
    """A job marked failed by a prior attempt stays failed on redelivery."""
    jid = _seed_job(default_tenant, sample_docx_bytes)

    # Manually mark the job as terminally failed.
    with sync_session() as s:
        job = s.get(RenderJob, jid)
        job.state = "failed"
        job.error = "prior_attempt_failed"
        from datetime import UTC, datetime
        job.completed_at = datetime.now(UTC)
        s.commit()
        original_error = job.error
        original_completed = job.completed_at

    render_job.fn(str(jid))

    with sync_session() as s:
        job = s.get(RenderJob, jid)
        assert job.state == "failed"
        assert job.error == original_error
        assert job.completed_at == original_completed
