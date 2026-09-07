"""POST /render/mapped: caller-profile-driven render.

Route-level tests hit the failure modes (missing profile, cross-tenant leak).
The mapping engine itself is unit-tested in test_mapping.py, and the shared
render pipeline (`_execute_render`) is exercised by the existing /render tests.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from docgen.designtime.freeze import freeze_version
from docgen.designtime.upload import upload_template
from docgen.main import app
from docgen.providers.base import MappingProposal
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import CallerProfile, Provenance, RenderJob, Template, Tenant


def test_missing_profile_returns_404(auth_headers) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/render/mapped",
            json={"profile_id": str(uuid.uuid4()), "data": {"a": 1}},
            headers=auth_headers,
        )
    assert r.status_code == 404
    assert "profile" in r.json()["detail"].lower()


def test_profile_from_other_tenant_returns_404(auth_headers) -> None:
    """Cross-tenant lookup returns 404 (not 403) — same rule as templates/jobs."""
    other_tid = uuid.uuid4()
    other_template_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=other_tid, name=f"rm-other-{other_tid.hex[:8]}"))
        s.flush()
        s.add(Template(id=other_template_id, tenant_id=other_tid, name="rm-other-tmpl"))
        s.flush()
        s.add(
            CallerProfile(
                id=profile_id,
                tenant_id=other_tid,
                template_id=other_template_id,
                version=1,
                caller="acme",
                name="test",
                mapping={},
                sample_payload={},
            )
        )
        s.commit()

    try:
        with TestClient(app) as client:
            r = client.post(
                "/render/mapped",
                json={"profile_id": str(profile_id), "data": {}},
                headers=auth_headers,
            )
        assert r.status_code == 404
    finally:
        with sync_session() as s:
            # Template cascade-deletes the profile via FK ondelete=CASCADE.
            tmpl = s.get(Template, other_template_id)
            if tmpl:
                s.delete(tmpl)
            t = s.get(Tenant, other_tid)
            if t:
                s.delete(t)
            s.commit()


def test_unauthenticated_returns_401() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/render/mapped",
            json={"profile_id": str(uuid.uuid4()), "data": {}},
        )
    assert r.status_code == 401


def test_profile_version_pin_survives_template_bump(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    """Contract: a profile pinned to v=N must resolve v=N even after the
    template has been bumped to v=N+1 (`current_version` moved on).

    This is the "silent backfill" regression guard for the mapping engine:
    if `_resolve_frozen_version` ever accidentally used `template.current_version`
    instead of `profile.version`, callers with old profiles would suddenly render
    against a newer schema — payload keys silently drop, required fields silently
    miss, and the caller has no way to notice until a downstream consumer breaks.
    We'd rather force them onto v=1 forever (until they publish a new profile)
    than have that silent drift.
    """
    async def _prep() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        try:
            async with AsyncSessionLocal() as s:
                # v1: upload + freeze + publish (current_version=1)
                template, v1, _ = await upload_template(
                    s,
                    name=f"pin-{uuid.uuid4().hex[:6]}",
                    docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                )
                tv1 = await freeze_version(
                    s, version_id=v1.id, proposal=MappingProposal(suggestions=[])
                )
                template.current_version = tv1.version
                await s.commit()

                # v2: same source bytes, appended as a new version + frozen + published.
                _, v2, _ = await upload_template(
                    s,
                    name=template.name,  # ignored — template_id short-circuits the name path
                    docx_bytes=sample_docx_bytes,
                    tenant_id=default_tenant,
                    template_id=template.id,
                )
                tv2 = await freeze_version(
                    s, version_id=v2.id, proposal=MappingProposal(suggestions=[])
                )
                template.current_version = tv2.version
                await s.commit()

                # Profile pinned to v=1. Mapping mirrors the payload keys —
                # each target references the same-named source path.
                profile = CallerProfile(
                    tenant_id=default_tenant,
                    template_id=template.id,
                    version=1,
                    caller="acme",
                    name="pin-test",
                    mapping={
                        "MEMBER_NAME": {"source": "$.MEMBER_NAME"},
                        "CLAIM_ID": {"source": "$.CLAIM_ID"},
                        "CLAIM_DATE": {"source": "$.CLAIM_DATE"},
                        "AMOUNT": {"source": "$.AMOUNT"},
                        "agent_name": {"source": "$.agent_name"},
                    },
                    sample_payload={},
                )
                s.add(profile)
                await s.commit()
                await s.refresh(profile)
                return template.id, tv1.id, tv2.id, profile.id
        finally:
            await async_engine.dispose()

    template_id, tv1_id, tv2_id, profile_id = asyncio.run(_prep())

    try:
        with TestClient(app) as client:
            r = client.post(
                "/render/mapped",
                json={
                    "profile_id": str(profile_id),
                    "data": {
                        "MEMBER_NAME": "Alice",
                        "CLAIM_ID": "C1",
                        "CLAIM_DATE": "2026-01-01",
                        "AMOUNT": 10,
                        "agent_name": "Bob",
                    },
                },
                headers=auth_headers,
            )
        assert r.status_code == 200, r.text
        job_id = uuid.UUID(r.json()["job_id"])

        with sync_session() as s:
            job = s.get(RenderJob, job_id)
            assert job is not None
            # The pinned version wins. If this ever flips to tv2_id, the pinning
            # contract has silently broken and old callers are drifting onto a
            # newer schema without any signal.
            assert job.template_version_id == tv1_id, (
                f"profile pinned to v=1 (tv={tv1_id}) resolved to tv={job.template_version_id}; "
                f"expected v=1, current_version was bumped to v=2 (tv={tv2_id}) but the profile "
                "must not silently upgrade."
            )
    finally:
        with sync_session() as s:
            # RenderJob.template_version_id and Provenance.render_job_id are both
            # ondelete=RESTRICT, so clear rows scoped to *this template's* versions
            # (never tenant-wide — that would eat unrelated tests' jobs on the
            # shared default tenant) before dropping the template itself.
            job_ids = s.execute(
                select(RenderJob.id).where(RenderJob.template_version_id.in_([tv1_id, tv2_id]))
            ).scalars().all()
            if job_ids:
                s.execute(delete(Provenance).where(Provenance.render_job_id.in_(job_ids)))
                s.execute(delete(RenderJob).where(RenderJob.id.in_(job_ids)))
            t = s.get(Template, template_id)
            if t:
                s.delete(t)
            s.commit()
