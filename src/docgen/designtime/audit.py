"""Per-template audit trail writes.

Every meaningful designtime action (version created, placements edited,
AI passes, freezes, sub-version clones, publishes) writes one row to
`template_audit_events`. Callers pass a compact summary dict; the shape
is per-event and consumed only by the UI timeline.

Kept as a thin helper (not a service) so any route/pipeline can drop a
row without pulling in extra abstractions. Does not commit — leaves that
to the caller so the audit row is part of the same transaction as the
action being audited (an event that reports "frozen" without the freeze
actually landing would be worse than no event).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from docgen.store.models import TemplateAuditEvent, TemplateVersion


async def record_audit_event(
    session: AsyncSession,
    *,
    template_id: uuid.UUID,
    event_kind: str,
    actor: str | None = None,
    version_id: uuid.UUID | None = None,
    version: int | None = None,
    subversion: int | None = None,
    locale: str | None = None,
    summary: dict[str, Any] | None = None,
) -> TemplateAuditEvent:
    """Write one row. Caller commits."""
    # If the caller passed a version_id but not the denormalized fields,
    # fill them from the row so the timeline stays readable even after the
    # version row is later deleted.
    if version_id is not None and (version is None or subversion is None or locale is None):
        tv = await session.get(TemplateVersion, version_id)
        if tv is not None:
            if version is None:
                version = tv.version
            if subversion is None:
                subversion = tv.subversion
            if locale is None:
                locale = tv.locale

    event = TemplateAuditEvent(
        template_id=template_id,
        template_version_id=version_id,
        version=version,
        subversion=subversion,
        locale=locale,
        actor=actor,
        event_kind=event_kind,
        summary=summary or {},
    )
    session.add(event)
    return event
