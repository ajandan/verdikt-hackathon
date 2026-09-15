"""Per-tenant quota checks.

The three quotas live on the ``tenants`` row (nullable = no cap):

- ``quota_renders_per_day``  → rolling 24h count of ``render_jobs``
- ``quota_active_templates`` → count of ``templates`` with ``is_active=true``
- ``quota_storage_bytes``    → reserved; enforcement not wired here yet

Callers raise :class:`QuotaExceeded` on trip. HTTP layers translate it
to 429 with a body naming which quota tripped and what the current
usage vs cap is.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class QuotaExceeded(Exception):
    """One quota tripped. ``kind`` is a stable identifier for the UI."""

    def __init__(self, kind: str, limit: int, current: int) -> None:
        self.kind = kind
        self.limit = limit
        self.current = current
        super().__init__(
            f"tenant quota exceeded: {kind} ({current}/{limit})"
        )


async def check_render_quota(session: "AsyncSession", tenant_id: uuid.UUID) -> None:
    """Raise :class:`QuotaExceeded` if this tenant has hit its 24h render cap.

    Counts every render job submitted in the last 24 hours regardless of
    terminal state — retries and failed renders both consume quota so an
    operator can't dodge a cap by cancelling and resubmitting.
    """
    from docgen.store.models import RenderJob, Tenant

    cap_stmt = select(Tenant.quota_renders_per_day).where(Tenant.id == tenant_id)
    cap = (await session.execute(cap_stmt)).scalar_one_or_none()
    if cap is None:
        return

    since = datetime.now(tz=UTC) - timedelta(hours=24)
    count_stmt = (
        select(func.count())
        .select_from(RenderJob)
        .where(RenderJob.tenant_id == tenant_id)
        .where(RenderJob.submitted_at >= since)
    )
    current = (await session.execute(count_stmt)).scalar_one()
    if current >= cap:
        raise QuotaExceeded("renders_per_day", cap, current)


async def check_active_template_quota(
    session: "AsyncSession", tenant_id: uuid.UUID
) -> None:
    """Raise :class:`QuotaExceeded` if creating another active template
    would exceed the tenant's ``quota_active_templates`` cap.

    Compares the current count of active templates (before this call
    creates or activates another) against the cap. The caller invokes
    this *before* insert / activate to keep the write path clean.
    """
    from docgen.store.models import Template, Tenant

    cap_stmt = select(Tenant.quota_active_templates).where(Tenant.id == tenant_id)
    cap = (await session.execute(cap_stmt)).scalar_one_or_none()
    if cap is None:
        return

    count_stmt = (
        select(func.count())
        .select_from(Template)
        .where(Template.tenant_id == tenant_id)
        .where(Template.is_active.is_(True))
    )
    current = (await session.execute(count_stmt)).scalar_one()
    if current >= cap:
        raise QuotaExceeded("active_templates", cap, current)
