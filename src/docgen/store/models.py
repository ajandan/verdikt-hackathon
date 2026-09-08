from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    key_ref: Mapped[str | None] = mapped_column(String(255))
    ai_provider_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Inbox-agent settings. Keys the agent reads:
    #   sender_domains: [str]            allow-list, case-insensitive
    #   subject_tag: str | None          e.g. "[tenant:acme]", matched first
    #   auto_approve_threshold: float    0..100, publish when score >= this
    #   notify_addresses: [str]          extra CC on outbound replies
    inbound_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(255))
    # "docx" (Word + Jinja) or "pdf" (fillable form or coord-based overlay).
    # Determines the render pipeline; can't change after the first version is
    # created (a template is one artifact family for its lifetime).
    kind: Mapped[str] = mapped_column(String(16), default="docx", nullable=False)
    # Soft-delete / archive flag. Inactive templates are hidden from listings
    # (UI + MCP) and refuse to render; their versions, blobs, and provenance
    # stay intact so an operator can reactivate without losing history.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_version: Mapped[int | None] = mapped_column(Integer)
    # Which sub-version of `current_version` is live. Placement edits on a
    # frozen version create a new (version, subversion+1) row rather than
    # mutating in place, so publish must pin both numbers to be precise.
    # Null when no version has ever been published.
    current_subversion: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    versions: Mapped[list["TemplateVersion"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    __table_args__ = (
        UniqueConstraint(
            "template_id", "version", "subversion", "locale",
            name="uq_template_version_locale",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Sub-version bump within a major version. Every edit to a frozen PDF's
    # placements clones the row into (version, subversion+1); the parent row
    # is preserved as immutable history. Fresh PDF uploads bump `version`
    # and reset subversion to 0.
    subversion: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Locale tag (BCP 47 style, e.g. "en", "en-GB", "es-419") or "default" for the
    # base variant. All locales of the same version_number share one JSON schema —
    # enforced at freeze time so payload contracts don't drift across languages.
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="default")
    schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    preflight: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    blob_key: Mapped[str] = mapped_column(String(512), nullable=False)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Only set when Template.kind == "pdf": "acroform" for fillable-widget PDFs,
    # "flat" for coord-overlay PDFs. Null for docx.
    pdf_flavor: Mapped[str | None] = mapped_column(String(16))
    # For pdf_flavor == "flat": list of {name, page, x_pct, y_pct, w_pct,
    # font_size, anchor} placements, coordinates in page-relative percentages
    # so they survive minor page-size changes. Null otherwise.
    pdf_field_placements: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    template: Mapped[Template] = relationship(back_populates="versions")


class RenderJob(Base):
    __tablename__ = "render_jobs"
    __table_args__ = (
        # Dashboards filter tenant + range/order on submitted_at.
        Index("ix_render_jobs_tenant_submitted", "tenant_id", "submitted_at"),
        # FK join target for provenance + "top templates".
        Index("ix_render_jobs_template_version", "template_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    caller: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_format: Mapped[str] = mapped_column(String(16), default="docx", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_blob_key: Mapped[str | None] = mapped_column(String(512))
    result_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    callback_url: Mapped[str | None] = mapped_column(String(1024))
    webhook_state: Mapped[str] = mapped_column(String(32), default="not_configured", nullable=False)
    webhook_last_error: Mapped[str | None] = mapped_column(Text)
    webhook_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AdminUser(Base):
    """UI login identity. `tenant_id` is NULL for super_admins (cross-tenant);
    otherwise pins the user to exactly one tenant's data.

    Coexists with the env-var admin (`DOCGEN_ADMIN_USER` + `_PASSWORD_HASH`),
    which stays as a break-glass super_admin when this table is empty — so a
    fresh deploy can't lock itself out. Once you seed a real super_admin, the
    env-var identity is still accepted at login (also as super_admin) for
    disaster recovery; delete/rotate the env vars if you want to retire it.
    """

    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('super_admin', 'tenant_admin')",
            name="ck_admin_users_role",
        ),
        # A tenant_admin must have a tenant_id; a super_admin must not.
        CheckConstraint(
            "(role = 'super_admin' AND tenant_id IS NULL) OR "
            "(role = 'tenant_admin' AND tenant_id IS NOT NULL)",
            name="ck_admin_users_role_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # Nullable = super_admin (no tenant scope). tenant_admin must have a tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # "super_admin" | "tenant_admin". Enforced at write time in the management
    # UI; DB uses a check constraint below so bad rows can't be inserted by
    # a rogue seed script.
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublishEvent(Base):
    """Audit trail of publish and rollback actions on a template.

    Freeze locks the artifact; publish flips `Template.current_version` to it.
    Every publish (including a rollback, which is just publishing an older
    version_number) writes one row so the timeline is queryable.
    """

    __tablename__ = "publish_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Sub-version pointer — precise identifier of the frozen row that was
    # published. Existing rows backfill to 0 (see migration 0013).
    subversion: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    previous_version: Mapped[int | None] = mapped_column(Integer)
    previous_subversion: Mapped[int | None] = mapped_column(Integer)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "publish" | "rollback"
    actor: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TemplateAuditEvent(Base):
    """Per-template audit trail: who did what and when.

    Complements PublishEvent (which is scoped to publish/rollback). This
    table records every meaningful designtime action — version creation,
    placement edits, AI passes, freezes, sub-version clones, publishes —
    so operators can reconstruct the history of a template.

    `template_version_id` is nullable so template-scoped events (e.g. the
    initial version upload before the row's UUID is known to the caller)
    still land in the trail. `version`/`subversion`/`locale` are denormalized
    for display so the timeline stays readable even if a version row is
    later deleted.
    """

    __tablename__ = "template_audit_events"
    __table_args__ = (
        Index("ix_audit_events_template_created", "template_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int | None] = mapped_column(Integer)
    subversion: Mapped[int | None] = mapped_column(Integer)
    locale: Mapped[str | None] = mapped_column(String(16))
    actor: Mapped[str | None] = mapped_column(String(255))
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Provenance(Base):
    __tablename__ = "provenance"
    __table_args__ = (
        # Provenance list filters on template_version_id and orders by rendered_at desc.
        Index("ix_provenance_template_version_rendered", "template_version_id", "rendered_at"),
        # FK lookup by render_job_id.
        Index("ix_provenance_render_job", "render_job_id"),
        # One provenance row per job. Makes the render actor idempotent on retry.
        UniqueConstraint("render_job_id", name="uq_provenance_render_job"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    render_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("render_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=False
    )
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    render_stack_hash: Mapped[str | None] = mapped_column(String(16), index=True)
    caller: Mapped[str | None] = mapped_column(String(255))
    rendered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyKey(Base):
    """Caller-supplied Idempotency-Key -> RenderJob mapping.

    Composite PK on (tenant_id, key) keeps namespaces separated per tenant so
    two callers can safely reuse the same key string. `request_hash` lets us
    detect key re-use with a changed payload (returned as 409).
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT (not CASCADE): if a job is deleted, the mapping must be
        # deleted first — otherwise a retry with the same key would allow a
        # *new* render (different job_id), silently breaking idempotency.
        # Cleanup path: prune old IdempotencyKey rows on a TTL, then let ops
        # delete jobs.
        ForeignKey("render_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CallerProfile(Base):
    """Saved translation from a caller's native JSON shape to a template schema.

    Scoped to (tenant, template, version, caller, name): one caller can keep
    multiple named profiles per template (e.g. "long form" vs "short form"), and
    each profile is pinned to a specific frozen version so schema drift surfaces
    as an upgrade prompt rather than a silent break.
    """

    __tablename__ = "caller_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "template_id",
            "version",
            "caller",
            "name",
            name="uq_caller_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    caller: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    sample_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class InboxEvent(Base):
    """One row per email the inbox agent has seen.

    Written unconditionally by the poller (even when the sender doesn't
    resolve to a tenant) so operators can debug ingestion. The unique
    constraint on `message_id` is what makes the poller idempotent across
    restarts. `reply_message_id` holds the Message-ID of our outbound
    reply and is what Phase-3 reply-to-approve matches against.
    """

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_inbox_events_message_id"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'skipped')",
            name="ck_inbox_events_status",
        ),
        CheckConstraint(
            "review_state IS NULL OR review_state IN "
            "('auto_approved', 'pending_review', 'approved', 'rejected')",
            name="ck_inbox_events_review_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    from_addr: Mapped[str] = mapped_column(String(320), nullable=False)
    to_addr: Mapped[str | None] = mapped_column(String(320))
    cc_addrs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    subject: Mapped[str | None] = mapped_column(String(998))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="SET NULL"), nullable=True
    )
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 0..100 confidence produced by propose_mapping. Null if the pipeline
    # failed before scoring.
    score: Mapped[float | None] = mapped_column()
    review_state: Mapped[str | None] = mapped_column(String(32), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    reply_message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    attachment_name: Mapped[str | None] = mapped_column(String(255))
    attachment_hash: Mapped[str | None] = mapped_column(String(64))
    # First ~8KB of the plain-text body, truncated by the poller. Rendered on
    # the review page so operators can see what was received without
    # re-fetching from IMAP.
    body_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
