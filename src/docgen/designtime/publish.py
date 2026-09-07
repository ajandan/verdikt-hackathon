"""Draft/publish workflow (§Phase 3): freeze locks the artifact, publish flips
the version-serving pointer.

Publishing an older version is called a "rollback" — same operation, different
event_kind stamped into the audit trail. Publishing a not-yet-published version
is a "publish". Every call writes exactly one PublishEvent row.
"""
from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.designtime.audit import record_audit_event
from docgen.store.models import PublishEvent, Template, TemplateVersion

log = structlog.get_logger(__name__)


async def publish_version(
    session: AsyncSession,
    *,
    template_id: uuid.UUID,
    tenant_id: uuid.UUID,
    version: int,
    subversion: int | None = None,
    actor: str | None = None,
) -> tuple[Template, PublishEvent]:
    """Point Template.current_version at `(version, subversion)` and log the event.

    Requires that the target (version, subversion) default-locale row is
    frozen — otherwise callers with no explicit locale would 404 immediately
    after the publish "succeeds". When `subversion` is None, publishes the
    highest frozen sub for `version` (the natural "publish latest" action).
    """
    template = await session.get(Template, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise ValueError(f"Template {template_id} does not exist")

    # Serialize concurrent publish/freeze per-template so two publishers can't
    # race and lose one PublishEvent, or see stale current_version when
    # classifying rollback vs publish. Held until commit.
    # tenant-audit: allow (PK row-lock only; tenant scoping happens in the
    # REST/UI caller that resolved `template_id`; also verified above at
    # line ~40 via `template.tenant_id != tenant_id` check).
    await session.execute(
        select(Template.id).where(Template.id == template_id).with_for_update()
    )
    await session.refresh(template)

    if subversion is None:
        # Highest frozen sub for this version+default locale.
        max_sub_row = await session.execute(
            select(func.max(TemplateVersion.subversion)).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == version,
                TemplateVersion.locale == "default",
                TemplateVersion.is_frozen.is_(True),
            )
        )
        max_sub = max_sub_row.scalar()
        if max_sub is None:
            raise ValueError(
                f"Cannot publish v{version}: no frozen 'default' locale row exists."
            )
        subversion = max_sub

    default_frozen = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version,
            TemplateVersion.subversion == subversion,
            TemplateVersion.locale == "default",
            TemplateVersion.is_frozen.is_(True),
        )
    )
    if default_frozen.scalar_one_or_none() is None:
        raise ValueError(
            f"Cannot publish v{version}.{subversion}: the 'default' locale must be frozen first."
        )

    previous = template.current_version
    previous_sub = template.current_subversion
    if previous == version and previous_sub == subversion:
        raise ValueError(
            f"v{version}.{subversion} is already the currently-published version."
        )
    event_kind = await _classify_event(
        session=session,
        template_id=template_id,
        target_version=version,
        target_subversion=subversion,
        current_version=previous,
        current_subversion=previous_sub,
    )

    template.current_version = version
    template.current_subversion = subversion
    event = PublishEvent(
        template_id=template_id,
        version=version,
        subversion=subversion,
        previous_version=previous,
        previous_subversion=previous_sub,
        event_kind=event_kind,
        actor=actor,
    )
    session.add(event)
    await record_audit_event(
        session,
        template_id=template_id,
        version=version,
        subversion=subversion,
        actor=actor,
        event_kind=event_kind,
        summary={
            "previous_version": previous,
            "previous_subversion": previous_sub,
        },
    )
    await session.commit()
    await session.refresh(event)
    await session.refresh(template)

    log.info(
        "template_published",
        template_id=str(template_id),
        tenant_id=str(tenant_id),
        version=version,
        subversion=subversion,
        previous_version=previous,
        previous_subversion=previous_sub,
        event_kind=event_kind,
        actor=actor,
    )
    return template, event


async def _classify_event(
    *,
    session: AsyncSession,
    template_id: uuid.UUID,
    target_version: int,
    target_subversion: int,
    current_version: int | None,
    current_subversion: int | None,
) -> str:
    """Rollback = re-publishing a (version, subversion) we've published before.

    Compares as tuples so v2.0 → v1.5 is a rollback, v1.0 → v1.1 is a
    publish (moving forward within a major version).
    """
    if current_version is None:
        return "publish"
    current_tuple = (current_version, current_subversion or 0)
    target_tuple = (target_version, target_subversion)
    if target_tuple < current_tuple:
        return "rollback"
    # Forward move: still a rollback if we've published this exact pair before.
    prior = await session.execute(
        select(PublishEvent.id)
        .where(PublishEvent.template_id == template_id)
        .where(PublishEvent.version == target_version)
        .where(PublishEvent.subversion == target_subversion)
        .limit(1)
    )
    return "rollback" if prior.scalar_one_or_none() is not None else "publish"


async def list_publish_events(
    session: AsyncSession,
    *,
    template_id: uuid.UUID,
    tenant_id: uuid.UUID,
    limit: int = 50,
) -> list[PublishEvent]:
    """Return the template's publish history, newest first."""
    template = await session.get(Template, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise ValueError(f"Template {template_id} does not exist")
    result = await session.execute(
        select(PublishEvent)
        .where(PublishEvent.template_id == template_id)
        .order_by(PublishEvent.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
