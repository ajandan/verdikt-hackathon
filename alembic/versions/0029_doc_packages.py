"""doc packages — tenant-scoped static doc library + ordered composition

Adds the outbound-artifact model described in ADR-0003:

- `static_documents` (tenant-scoped) + `static_document_versions`
  (immutable per version) — the byte-stable PDF library (guides, appeals
  notices, blank forms).
- `doc_packages` (tenant-scoped) — ordered composition of items where
  each item is `{kind: "template" | "static", ref_id: uuid}`. No
  version pin on items; the assembler picks the latest published
  version at runtime.
- `doc_package_assemblies` (append-only) — one row per assembler run,
  stamping the resolved (template_version_id / static_document_version_id)
  tuple per item and the merged PDF blob key. This is the audit answer
  to "what exact bytes did this case receive."
- `programs.outcomes` (JSONB array of outcome slugs).
- `programs.package_bindings` (JSONB map `outcome -> package_id`).
  Enforced at publish (not at DB level): every entry in outcomes[]
  must have a package_bindings entry; every rule's emitted outcome must
  appear in outcomes[].

The seeded outcome vocabulary for existing programs is
`["approve", "deny", "missing_info"]` — the three verdicts the
Verdikt pipeline emits today (`pap_pipeline/schemas/decision.py`).
Bindings start empty so operators must consciously wire packages
before marking programs publishable.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-12
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


_DEFAULT_OUTCOMES: list[str] = ["approve", "deny", "missing_info"]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # static_documents — the tenant's byte-stable PDF library.
    # ------------------------------------------------------------------
    op.create_table(
        "static_documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "current_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_static_documents_tenant_name"),
    )

    # ------------------------------------------------------------------
    # static_document_versions — immutable per version.
    # ------------------------------------------------------------------
    op.create_table(
        "static_document_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "static_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("static_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("blob_key", sa.String(1024), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "static_document_id", "version", name="uq_static_document_versions_doc_ver"
        ),
    )

    # ------------------------------------------------------------------
    # doc_packages — ordered composition of template + static items.
    # ------------------------------------------------------------------
    op.create_table(
        "doc_packages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # items: ordered list of {kind: "template"|"static", ref_id: uuid,
        #                        label: str | null}. Auto-follows latest version.
        sa.Column(
            "items",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_doc_packages_tenant_name"),
    )

    # ------------------------------------------------------------------
    # doc_package_assemblies — append-only provenance for each merge.
    # ------------------------------------------------------------------
    op.create_table(
        "doc_package_assemblies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "doc_package_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("doc_packages.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "inbox_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_events.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("outcome", sa.String(64), nullable=False),
        # resolved_items: ordered list of what was assembled, e.g.
        # [{"kind": "template", "ref_id": "...", "version_id": "..."},
        #  {"kind": "static",   "ref_id": "...", "version_id": "..."}]
        sa.Column("resolved_items", postgresql.JSONB(), nullable=False),
        sa.Column("merged_blob_key", sa.String(1024), nullable=False),
        sa.Column("merged_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("merged_page_count", sa.Integer(), nullable=True),
        sa.Column(
            "assembled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------
    # programs.outcomes[] + programs.package_bindings{}
    # ------------------------------------------------------------------
    op.add_column(
        "programs",
        sa.Column(
            "outcomes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "programs",
        sa.Column(
            "package_bindings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Seed the outcome vocabulary on existing programs so the admin UI
    # has something to render at first hit. Bindings stay empty so the
    # operator must consciously wire packages — no silent fallback.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE programs SET outcomes = CAST(:outcomes AS jsonb) "
            "WHERE outcomes = '[]'::jsonb"
        ),
        {"outcomes": json.dumps(_DEFAULT_OUTCOMES)},
    )


def downgrade() -> None:
    op.drop_column("programs", "package_bindings")
    op.drop_column("programs", "outcomes")
    op.drop_table("doc_package_assemblies")
    op.drop_table("doc_packages")
    op.drop_table("static_document_versions")
    op.drop_table("static_documents")
