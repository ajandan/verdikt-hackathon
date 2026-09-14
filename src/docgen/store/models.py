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
    text,
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
    # MDM connector config (ADR-0004). Schema:
    #   provider: "simulated" | "null" | "<real-connector-name>"
    #   base_url, api_key_env, program_families, max_window_days,
    #   max_events, timeout — provider-specific.
    # Empty dict → SimulatedMdmSource by default so a fresh tenant demos
    # end-to-end without setup. Operators wire `provider: "null"` to opt
    # out explicitly, or a real connector name to point at a live MDM.
    mdm_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Per-tenant usage caps (nullable = no cap). Enforced by docgen.quotas at
    # render-submit and template-upload time; storage_bytes is reserved but
    # not enforced yet — see migration 0032 note.
    quota_renders_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_active_templates: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_storage_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Program(Base):
    """A tenant-scoped PAP program with its own extraction schema.

    `extraction_schema` is a JSONB list of slot dicts:
      {"name": "patient_name", "type": "string", "description": "...",
       "source_field": "<enrollment-template field>" | null,
       "required": bool, "enum_values": [str] | null}

    Slots drive the vision-model extraction prompt at intake time, so this row
    is the source of truth for "what fields does this program capture." The
    enrollment template link lets the UI pre-populate slots from the form's
    field manifest; once finalized, the extraction schema is decoupled from
    the template so re-labeling the form doesn't invalidate live intakes.
    """

    __tablename__ = "programs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_programs_tenant_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enrollment_template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    extraction_schema: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Optional inbound-routing hint — e.g. a subject-tag alias `[pap:cv-2026]`.
    # None means the program is only reachable when the subject explicitly
    # names its slug.
    inbound_alias: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # M2M submissions (REST + MCP) to this program hold for reviewer approval
    # instead of returning the full verdict inline. When true, the intake
    # response is `status: "queued"` and the reviewer's approve/reject fires
    # a signed webhook (if the caller supplied callback_url) with the final
    # decision. Email cases always hold; this flag only affects the M2M lane.
    requires_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Declared outcome vocabulary — the set of verdict slugs this program's
    # rules can emit. Every entry must have a package_bindings entry before
    # the program can be published (see ADR-0003). Seeded to
    # ["approved", "denied", "missing_info"] by migration 0029.
    outcomes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Outcome -> doc_package_id map. Assembler at runtime does
    # package_bindings[verdict.outcome]. Missing binding is a hard error at
    # publish, not a silent fall-through.
    package_bindings: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Declared history slots (ADR-0004). Each entry is a slot definition
    # dict — see pap_pipeline.stages.slot_compile.SlotDef. Program activation
    # refuses when a linked rule references a slot name not declared here.
    history_slots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StaticDocument(Base):
    """A tenant-scoped byte-stable PDF (guide, appeals notice, blank form).

    Static docs are the non-generated half of a doc package. Unlike Templates,
    they are never merged with case data — the assembler concatenates them
    as-is into the outbound bundle. Versioned so an operator can update a
    guide without breaking the audit trail of packages already assembled with
    the old version. See ADR-0003.
    """

    __tablename__ = "static_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_static_documents_tenant_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Denormalized latest version number. Zero means no versions uploaded yet
    # (the row exists but the assembler will refuse to include it in a package
    # resolution).
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StaticDocumentVersion(Base):
    """One immutable version of a StaticDocument.

    A new upload for the same document name creates a new row. The blob is
    hashed on upload (SHA-256) so uploading the same bytes twice can be
    detected upstream and skipped.
    """

    __tablename__ = "static_document_versions"
    __table_args__ = (
        UniqueConstraint(
            "static_document_id", "version", name="uq_static_document_versions_doc_ver"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    static_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("static_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocPackage(Base):
    """Tenant-scoped ordered composition of template + static items.

    `items` is an ordered JSONB list where each entry is:
      {"kind": "template" | "static",
       "ref_id": "<uuid>",
       "label": "<optional operator-facing label>"}

    Items reference the parent Template or StaticDocument by id — not by
    version. The assembler picks the latest published version at runtime
    (see ADR-0003 for the version-pinning rationale).
    """

    __tablename__ = "doc_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_doc_packages_tenant_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DocPackageAssembly(Base):
    """Append-only provenance for one assembler run.

    `resolved_items` stamps the exact (template_version_id /
    static_document_version_id) tuple per item — so "which bytes did this
    case receive?" is a single primary-key lookup against `merged_blob_key`,
    and "what versions were merged into those bytes?" is `resolved_items`.
    """

    __tablename__ = "doc_package_assemblies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doc_package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("doc_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    inbox_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    merged_blob_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    merged_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    merged_page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assembled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Rule(Base):
    """One editable eligibility rule in the tenant's master rule library.

    Rules were originally YAML packs shipped with the pipeline (see
    `pap_pipeline/rules/*.yaml`). This table lifts them into a tenant-scoped,
    editable library so ops can add / edit / retire rules from the admin UI
    without a code deploy. Rules attach to programs through `ProgramRuleLink`
    — one rule can be reused by many programs, and edits propagate to every
    program that links it.

    `rule_id` is the stable public identifier cited in Decision.citations —
    e.g. "CV-INC-01". Kept string-based (not UUID) so it stays legible in
    the reply letter and the decision ledger. Unique per tenant so a rule
    id printed on a letter always refers to one rule row.
    """

    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_id", name="uq_rules_tenant_rule_id"),
        CheckConstraint(
            "kind IN ('financial', 'insurance', 'residency', 'clinical')",
            name="ck_rules_kind",
        ),
        Index("ix_rules_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Structured rule body. Kept short so the LLM sees a compact contract and
    # the coordinator reads the same block on the detail card.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    criterion: Mapped[str] = mapped_column(String(400), nullable=False)
    exception: Mapped[str] = mapped_column(String(400), nullable=False, default="", server_default="")
    # Intake field names this rule needs to be evaluable. When any listed
    # field is None at reason time, the rule contributes to missing_fields
    # rather than a match/fail verdict.
    fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Slot names this rule reads (ADR-0004). Nullable because rules that
    # don't touch history shouldn't be forced to carry an empty list — the
    # publish gate uses NULL as "does not participate in slot mediation".
    references_slots: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True
    )
    # Library-level active flag. A rule with is_active=False is hidden from
    # the picker and skipped by the retriever even for programs that link it
    # — think "retired rule" rather than "temporarily off for this program".
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RuleVersion(Base):
    """Immutable snapshot of a Rule row at a specific point in time.

    Written on every rule create and rule update. Enables a compliance-grade
    history: any decision letter cites a rule id + an implicit "as of" date,
    and this table lets ops reconstruct exactly what the LLM saw at that
    moment.

    `version` is monotonic per rule (1 on create, then 2, 3, …). Rollback is
    not built in — a "restore" is implemented as writing a new version whose
    payload equals the older snapshot's payload, so history stays append-only.
    """

    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("rule_id", "version", name="uq_rule_versions_rule_version"),
        Index("ix_rule_versions_rule_id", "rule_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Snapshot of the mutable rule body at save time. Public rule_id is
    # snapshotted too so a rename (should we ever allow it) leaves the older
    # citations legible.
    public_rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    criterion: Mapped[str] = mapped_column(String(400), nullable=False)
    exception: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    references_slots: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Actor context. `actor_user_id` is null when the write came from the
    # env-var break-glass super_admin (no DB user row) or from a backfill.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"),
    )
    actor_username: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    change_note: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProgramRuleLink(Base):
    """Join row: this program uses this rule from the library.

    Split from :class:`Rule` so a single library rule can be attached to
    many programs. Per-program `position` orders the reviewer view; per-
    program `is_active` lets a program temporarily skip a rule without
    detaching it or hiding it from other programs. Deletions cascade both
    ways — if the rule is retired or the program is deleted, the link
    disappears.
    """

    __tablename__ = "program_rule_links"
    __table_args__ = (
        UniqueConstraint("program_id", "rule_id", name="uq_program_rule_links_pair"),
        Index("ix_program_rule_links_program_id", "program_id"),
        Index("ix_program_rule_links_rule_id", "rule_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("programs.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


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
    # PAP pipeline artifacts. Populated only when the pap_pipeline handler
    # processes this event; NULL for template-flow events. The pipeline detail
    # page reads these to render extract/retrieve/reason evidence.
    pap_intake: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Raw dict returned by the extract stage — union of every slot the program
    # schema defined, before it's coerced onto the fixed Intake shape. Used by
    # the pipeline detail UI to show custom slots and by the reply mapper to
    # populate program-specific template fields. NULL for pre-migration events.
    pap_extraction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pap_decision: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Blob key of the PDF letter the reply email carried. Written by the PAP
    # handler after the render+attach step; reviewers download it via the
    # pipeline detail page to inspect exactly what the applicant received.
    reply_attachment_blob_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Per-file manifest of inbound attachments the poller received —
    # [{filename, content_type, blob_key, size}, ...]. Populated by the PAP
    # handler so reviewers can preview the raw files the applicant sent.
    # NULL for template-flow events (which store their single attachment
    # inside the template version) and pre-migration events.
    inbound_attachments: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # Append-only audit trail of human review actions. Each entry:
    # {"at": iso8601, "action": "approved|rejected|reply_sent|...", "actor": str, "note": str}.
    # Rendered on the reviewer detail pages so "who did what, when" is visible.
    review_history: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # Webhook delivery bookkeeping for the hybrid REST/MCP intake path.
    # callback_url is set by the caller at intake when the program requires
    # reviewer approval; the approve/reject endpoints POST the final verdict
    # to it and record delivery outcomes. NULL for cases without a webhook
    # (email cases + sync-verdict M2M cases).
    callback_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    callback_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    callback_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    callback_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    callback_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Short human-readable sequence for reviewer references. Populated by a
    # Postgres sequence at insert time — see migration 0022.
    case_number: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("nextval('inbox_case_seq')")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def case_id(self) -> str:
        """Short, human-readable case identifier — `QC-0042` style.

        Zero-padded to 4 digits for typical loads; grows to more digits
        naturally past 9999. Backed by the `inbox_case_seq` sequence so
        every event has a monotonic, spoken-friendly reference.
        """
        return f"QC-{self.case_number:04d}"


class InboxTestResult(Base):
    """One AI-planned test-render trial for an ingested template.

    Persisted so reviewers see the same set of trials in the email reply
    and the admin portal, with per-row downloads of the rendered artifact
    and the AI's verdict on whether the output looks right. Rows cascade
    with the parent inbox_events row — trials are useless without the
    event that produced them.
    """

    __tablename__ = "inbox_test_results"
    __table_args__ = (
        UniqueConstraint(
            "inbox_event_id", "ordinal", name="uq_inbox_test_results_event_ordinal"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    inbox_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    blob_key: Mapped[str | None] = mapped_column(String(512))
    ext: Mapped[str | None] = mapped_column(String(8))
    verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
