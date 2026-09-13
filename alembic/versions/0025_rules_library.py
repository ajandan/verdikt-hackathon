"""rules + program_rule_links — tenant-scoped rule library, many-to-many to programs

Lifts the YAML rule packs (pap_pipeline/rules/*.yaml) into a two-table shape:

  * `rules` — the tenant's master library. Edit once, reuse across programs.
  * `program_rule_links` — which library rules a program uses, with per-program
    position + is_active.

Seed step: for every existing Tenant, load every YAML pack into `rules` under
that tenant (each tenant gets its own copy of the library so edits stay
scoped). Then for every Program row, insert a link for each YAML rule whose
`program` slug matches the program's slug — so programs come pre-linked to
the rules they used to own directly.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-12
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
import yaml
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


# Rules ship in the source tree as YAML packs; the migration reads them at
# upgrade time so the seed data stays in one place. If the packs are absent
# (running against a stripped image) the migration still creates the tables —
# it just skips seeding.
_RULES_DIR = Path(__file__).resolve().parents[2] / "src" / "pap_pipeline" / "rules"


def _load_rule_packs() -> dict[str, list[dict]]:
    """Return `{program_slug: [rule_dict, ...]}` from the YAML packs on disk."""
    packs: dict[str, list[dict]] = {}
    if not _RULES_DIR.exists():
        return packs
    for path in sorted(_RULES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        slug = (data or {}).get("program")
        if not slug:
            continue
        packs.setdefault(slug, []).append(data)
    return packs


def upgrade() -> None:
    op.create_table(
        "rules",
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
        ),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.UniqueConstraint("tenant_id", "rule_id", name="uq_rules_tenant_rule_id"),
        sa.CheckConstraint(
            "kind IN ('financial', 'insurance', 'residency', 'clinical')",
            name="ck_rules_kind",
        ),
    )
    op.create_index("ix_rules_tenant_id", "rules", ["tenant_id"])

    op.create_table(
        "program_rule_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "program_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("programs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.UniqueConstraint("program_id", "rule_id", name="uq_program_rule_links_pair"),
    )
    op.create_index(
        "ix_program_rule_links_program_id", "program_rule_links", ["program_id"]
    )
    op.create_index(
        "ix_program_rule_links_rule_id", "program_rule_links", ["rule_id"]
    )

    packs = _load_rule_packs()
    if not packs:
        return

    conn = op.get_bind()
    tenants = conn.execute(sa.text("SELECT id FROM tenants")).fetchall()
    programs = conn.execute(
        sa.text("SELECT id, tenant_id, slug FROM programs")
    ).fetchall()

    # Every tenant gets a copy of every YAML rule as their starting library.
    # Keyed by (tenant_id, program_slug) -> {rule_id: rules.id} so we can
    # wire the program_rule_links below.
    #
    # Only kinds allowed by *this migration's* ck_rules_kind are seeded here.
    # Later kinds (e.g. 'history' added in 0031) are seeded by their own
    # migration after the CHECK constraint has been widened. Without this
    # filter, adding a new kind to a shipped YAML pack silently breaks
    # `alembic upgrade head` on fresh databases.
    _ALLOWED_KINDS_AT_0025 = {"financial", "insurance", "residency", "clinical"}
    seeded: dict[tuple[str, str], dict[str, str]] = {}
    for (tenant_id,) in tenants:
        for slug, pack in packs.items():
            for rule in pack:
                rule_public_id = rule.get("id")
                if not rule_public_id:
                    continue
                if rule.get("kind") not in _ALLOWED_KINDS_AT_0025:
                    continue
                new_rules_id = str(uuid.uuid4())
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO rules
                            (id, tenant_id, rule_id, kind, text, fields, is_active)
                        VALUES
                            (:id, :tid, :rid, :kind, :text, CAST(:fields AS jsonb), true)
                        ON CONFLICT (tenant_id, rule_id) DO NOTHING
                        """
                    ),
                    {
                        "id": new_rules_id,
                        "tid": str(tenant_id),
                        "rid": rule_public_id,
                        "kind": rule.get("kind"),
                        "text": rule.get("text") or "",
                        "fields": json.dumps(rule.get("fields") or []),
                    },
                )
                # ON CONFLICT may have skipped our insert — grab the id that
                # actually landed so the link points to the right row.
                row = conn.execute(
                    sa.text(
                        "SELECT id FROM rules WHERE tenant_id = :tid AND rule_id = :rid"
                    ),
                    {"tid": str(tenant_id), "rid": rule_public_id},
                ).first()
                if row is not None:
                    seeded.setdefault((str(tenant_id), slug), {})[rule_public_id] = str(row[0])

    # Link each program to the tenant-scoped rules for its slug, preserving
    # YAML order as the initial reviewer position.
    for program_id, tenant_id, slug in programs:
        rule_map = seeded.get((str(tenant_id), slug))
        if not rule_map:
            continue
        pack = packs.get(slug) or []
        for pos, rule in enumerate(pack):
            rule_public_id = rule.get("id")
            rules_id = rule_map.get(rule_public_id) if rule_public_id else None
            if not rules_id:
                continue
            conn.execute(
                sa.text(
                    """
                    INSERT INTO program_rule_links
                        (id, program_id, rule_id, position, is_active)
                    VALUES
                        (:id, :pid, :rid, :pos, true)
                    ON CONFLICT (program_id, rule_id) DO NOTHING
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "pid": str(program_id),
                    "rid": rules_id,
                    "pos": pos,
                },
            )


def downgrade() -> None:
    op.drop_index(
        "ix_program_rule_links_rule_id", table_name="program_rule_links"
    )
    op.drop_index(
        "ix_program_rule_links_program_id", table_name="program_rule_links"
    )
    op.drop_table("program_rule_links")
    op.drop_index("ix_rules_tenant_id", table_name="rules")
    op.drop_table("rules")
