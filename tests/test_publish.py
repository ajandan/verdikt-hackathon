"""Phase 3 (§draft/publish): freeze locks the artifact, publish flips
Template.current_version. Publishing an already-published version is
recorded as "rollback".

Covers:
  * freeze_version() no longer sets current_version (auto-publish removed)
  * publish_version() preconditions, happy path, event classification
  * list_publish_events() ordering + tenant scoping
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from docgen.designtime.freeze import freeze_version
from docgen.designtime.publish import list_publish_events, publish_version
from docgen.designtime.upload import upload_template
from docgen.providers.base import MappingProposal
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import PublishEvent, Template, TemplateVersion, Tenant


@pytest.fixture
def isolated_tenant():
    tid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"pub-t-{tid.hex[:8]}"))
        s.commit()
    yield tid
    with sync_session() as s:
        # Templates cascade to versions + publish_events via FK ondelete=CASCADE.
        for tmpl in s.query(Template).filter(Template.tenant_id == tid).all():
            s.delete(tmpl)
        s.commit()
        t = s.get(Tenant, tid)
        if t is not None:
            s.delete(t)
            s.commit()


def _run(coro):
    async def _wrap():
        try:
            return await coro
        finally:
            await async_engine.dispose()
    return asyncio.run(_wrap())


async def _make_frozen_version(
    session, *, template_id: uuid.UUID, version_number: int, locale: str = "default"
) -> TemplateVersion:
    """Hand-flip a draft row to frozen without needing MinIO + a real proposal.
    publish_version() only reads is_frozen; the blob contents don't matter here.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version_number,
            TemplateVersion.locale == locale,
        )
    )
    tv = result.scalar_one()
    tv.is_frozen = True
    tv.status = "frozen"
    await session.commit()
    await session.refresh(tv)
    return tv


# ---------------------------------------------------------------------------
# freeze no longer auto-publishes
# ---------------------------------------------------------------------------


def test_freeze_does_not_set_current_version(isolated_tenant, sample_docx_bytes) -> None:
    """After freeze_version(), template.current_version stays None.

    Publish is now a separate action — freezing only locks the artifact.
    """
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="fno-pub", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            # Empty proposal: sample_docx has no jinja braces so post-injection
            # preflight sees zero variables and freeze succeeds with an empty schema.
            frozen = await freeze_version(
                s, version_id=base.id, proposal=MappingProposal(suggestions=[])
            )
            await s.refresh(template)
            return frozen.is_frozen, template.current_version

    is_frozen, current = _run(_go())
    assert is_frozen is True
    assert current is None


# ---------------------------------------------------------------------------
# publish preconditions
# ---------------------------------------------------------------------------


def test_publish_requires_frozen_default_locale(isolated_tenant, sample_docx_bytes) -> None:
    """Publishing a version whose 'default' locale isn't frozen is a 400 —
    otherwise callers with no explicit locale would 404 immediately after."""
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="p-unfrozen", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            # publish_version raises one of two messages when the default
            # locale isn't frozen — the "no frozen row exists" branch fires
            # here because subversion is None (we let it auto-pick).
            with pytest.raises(ValueError, match="no frozen 'default' locale"):
                await publish_version(
                    s, template_id=template.id, tenant_id=isolated_tenant,
                    version=base.version,
                )

    _run(_go())


def test_publish_rejects_unknown_template(isolated_tenant) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            with pytest.raises(ValueError, match="does not exist"):
                await publish_version(
                    s, template_id=uuid.uuid4(), tenant_id=isolated_tenant, version=1,
                )

    _run(_go())


def test_publish_rejects_wrong_tenant(isolated_tenant, sample_docx_bytes) -> None:
    other_tenant = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=other_tenant, name=f"other-{other_tenant.hex[:8]}"))
        s.commit()

    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="p-wrong-t", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=base.version,
            )
            with pytest.raises(ValueError, match="does not exist"):
                await publish_version(
                    s, template_id=template.id, tenant_id=other_tenant,
                    version=base.version,
                )

    try:
        _run(_go())
    finally:
        with sync_session() as s:
            for tmpl in s.query(Template).filter(Template.tenant_id == other_tenant).all():
                s.delete(tmpl)
            s.commit()
            t = s.get(Tenant, other_tenant)
            if t is not None:
                s.delete(t)
                s.commit()


def test_publish_rejects_already_current_version(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="p-dup", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=base.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant,
                version=base.version,
            )
            with pytest.raises(ValueError, match="already the currently-published"):
                await publish_version(
                    s, template_id=template.id, tenant_id=isolated_tenant,
                    version=base.version,
                )

    _run(_go())


# ---------------------------------------------------------------------------
# happy path + event classification
# ---------------------------------------------------------------------------


def test_publish_sets_current_version_and_logs_publish_event(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="p-first", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=base.version,
            )
            tmpl, event = await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant,
                version=base.version, actor="alice@example.com",
            )
            return (
                tmpl.current_version,
                event.version, event.previous_version,
                event.event_kind, event.actor,
            )

    current, ver, prev, kind, actor = _run(_go())
    assert current == ver
    assert prev is None
    assert kind == "publish"
    assert actor == "alice@example.com"


def test_rollback_classified_as_rollback_event(
    isolated_tenant, sample_docx_bytes
) -> None:
    """Publishing an older version after a newer one → event_kind='rollback'."""
    async def _go():
        async with AsyncSessionLocal() as s:
            template, v1, _ = await upload_template(
                s, name="p-roll", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            _, v2, _ = await upload_template(
                s, name="p-roll", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant, template_id=template.id,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=v1.version,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=v2.version,
            )
            # First publish v1 (initial), then v2 (forward publish), then back to v1.
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v1.version,
            )
            _, e_fwd = await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v2.version,
            )
            tmpl, e_back = await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v1.version,
            )
            return (
                tmpl.current_version, v1.version, v2.version,
                e_fwd.event_kind, e_fwd.previous_version,
                e_back.event_kind, e_back.previous_version,
            )

    current, v1n, v2n, fwd_kind, fwd_prev, back_kind, back_prev = _run(_go())
    assert v2n == v1n + 1
    assert current == v1n
    assert fwd_kind == "publish"
    assert fwd_prev == v1n
    assert back_kind == "rollback"
    assert back_prev == v2n


def test_republish_forward_after_rollback_is_rollback(
    isolated_tenant, sample_docx_bytes
) -> None:
    """Re-publishing a *higher* version we've published before is still 'rollback' —
    _classify_event checks history, not just numeric order."""
    async def _go():
        async with AsyncSessionLocal() as s:
            template, v1, _ = await upload_template(
                s, name="p-fwd", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            _, v2, _ = await upload_template(
                s, name="p-fwd", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant, template_id=template.id,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=v1.version,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=v2.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v1.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v2.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v1.version,
            )
            # Now re-publish v2 (was published earlier → rollback despite v2 > v1).
            _, event = await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v2.version,
            )
            return event.event_kind

    assert _run(_go()) == "rollback"


# ---------------------------------------------------------------------------
# list_publish_events
# ---------------------------------------------------------------------------


def test_list_publish_events_returns_newest_first(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, v1, _ = await upload_template(
                s, name="p-hist", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            _, v2, _ = await upload_template(
                s, name="p-hist", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant, template_id=template.id,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=v1.version,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=v2.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v1.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant, version=v2.version,
            )
            events = await list_publish_events(
                s, template_id=template.id, tenant_id=isolated_tenant,
            )
            return [(e.version, e.event_kind) for e in events]

    history = _run(_go())
    # Newest first: v2 publish, then v1 publish.
    assert [v for v, _ in history] == [history[0][0], history[1][0]]
    assert history[0][1] == "publish"
    assert history[1][1] == "publish"
    assert history[0][0] > history[1][0]


def test_list_publish_events_scoped_to_tenant(
    isolated_tenant, sample_docx_bytes
) -> None:
    other_tenant = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=other_tenant, name=f"other-{other_tenant.hex[:8]}"))
        s.commit()

    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="p-scope", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=base.version,
            )
            await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant,
                version=base.version,
            )
            with pytest.raises(ValueError, match="does not exist"):
                await list_publish_events(
                    s, template_id=template.id, tenant_id=other_tenant,
                )

    try:
        _run(_go())
    finally:
        with sync_session() as s:
            t = s.get(Tenant, other_tenant)
            if t is not None:
                s.delete(t)
                s.commit()


# ---------------------------------------------------------------------------
# publish_events row persisted correctly
# ---------------------------------------------------------------------------


def test_publish_event_row_persisted_with_full_audit(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="p-audit", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            await _make_frozen_version(
                s, template_id=template.id, version_number=base.version,
            )
            _, event = await publish_version(
                s, template_id=template.id, tenant_id=isolated_tenant,
                version=base.version, actor="ops@x",
            )
            return template.id, event.id

    tmpl_id, event_id = _run(_go())

    with sync_session() as s:
        row = s.get(PublishEvent, event_id)
        assert row is not None
        assert row.template_id == tmpl_id
        assert row.event_kind == "publish"
        assert row.previous_version is None
        assert row.actor == "ops@x"
        assert row.created_at is not None


# ---------------------------------------------------------------------------
# concurrency: FOR UPDATE lock on Template row serializes publish/freeze
# ---------------------------------------------------------------------------


def test_concurrent_publish_serializes_per_template(
    isolated_tenant, sample_docx_bytes
) -> None:
    """Two publishes racing on the same template must serialize — the
    second publisher should see the first's committed current_version
    when computing previous_version. Without the FOR UPDATE lock both
    workers would race past `template.current_version` read and both
    would write previous_version=None, corrupting the audit chain.

    Uses ``asyncio.gather`` on two independent sessions in the same loop.
    Threads + asyncpg + fresh-event-loop-per-thread deadlocks in a way
    that has nothing to do with the FOR UPDATE lock we want to prove.
    """
    from sqlalchemy import select as _select

    # Seed: template with v1 and v2 both frozen. Use the sync session to
    # avoid entangling with the async event loop we're about to run.
    with sync_session() as s:
        from docgen.store.models import TemplateVersion as _TV

        tmpl = Template(tenant_id=isolated_tenant, name="p-concurrent")
        s.add(tmpl)
        s.flush()
        for v in (1, 2):
            s.add(
                _TV(
                    template_id=tmpl.id, version=v, locale="default",
                    schema={"fields": []}, blob_key=f"seed/{tmpl.id}/v{v}.docx",
                    version_hash=f"h{v}" * 8, status="frozen", is_frozen=True,
                )
            )
        s.commit()
        template_id = tmpl.id

    async def _publish_one(version_to_publish: int) -> None:
        # Fresh session per coroutine — same shape as two racing HTTP
        # requests would use.
        async with AsyncSessionLocal() as s:
            await publish_version(
                s, template_id=template_id, tenant_id=isolated_tenant,
                version=version_to_publish,
            )

    async def _race():
        await asyncio.gather(_publish_one(1), _publish_one(2))

    asyncio.run(_race())

    # Both publishes must have succeeded and the audit chain must be intact.
    # We order by created_at for readability, but PG's server-side `now()` is
    # transaction-*start* time, not commit time — so under contention the row
    # ordering by created_at can invert the actual commit order. The invariant
    # we care about is the chain: exactly one event has previous_version=None
    # (the loser of the race committed first), and the other event's
    # previous_version links back to it.
    with sync_session() as s:
        rows = s.execute(
            _select(PublishEvent)
            .where(PublishEvent.template_id == template_id)
        ).scalars().all()
        assert len(rows) == 2
        firsts = [r for r in rows if r.previous_version is None]
        seconds = [r for r in rows if r.previous_version is not None]
        assert len(firsts) == 1 and len(seconds) == 1, (
            f"expected one chain-head and one chain-tail, got firsts={firsts!r} seconds={seconds!r}"
        )
        head, tail = firsts[0], seconds[0]
        assert tail.previous_version == head.version, (
            f"concurrent publish corrupted audit chain: "
            f"head={head.version}, tail={tail.version} prev={tail.previous_version}"
        )
        tmpl = s.get(Template, template_id)
        assert tmpl.current_version == tail.version
