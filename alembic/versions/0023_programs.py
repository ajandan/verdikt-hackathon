"""programs — tenant-scoped PAP program with per-program extraction schema

Each Program owns an extraction schema (list of semantic slots) derived from
its enrollment template. Replaces the hardcoded `Intake` shape in the PAP
pipeline: the orchestrator now looks up the Program by (tenant, slug), builds
the vision-model prompt from the Program's slots, and returns a program-shaped
intake dict.

Seed step: for each existing tenant, create a `cardiovita-pap-2026` Program
with the canonical Intake slots pre-populated (source_field still null — the
admin can wire those up later by picking an enrollment template). Ensures
in-flight intakes keep resolving during the cutover.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-11
"""
from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


# Canonical slots for cardiovita-pap-2026. Mirrors the fields the CV-* rules
# reference, so seeded programs stay compatible with the existing rule YAMLs
# out of the box. Admins can add/remove slots per-program via the UI once
# they've bound an enrollment template.
_CARDIOVITA_SLOTS: list[dict] = [
    {"name": "patient_name", "type": "string", "description": "Patient full legal name as written on the form.", "source_field": None, "required": False, "enum_values": None},
    {"name": "family_name", "type": "string", "description": "Patient surname / last name only.", "source_field": None, "required": False, "enum_values": None},
    {"name": "honorific", "type": "enum", "description": "Title (Ms., Mr., Dr.) — only when explicitly written.", "source_field": None, "required": False, "enum_values": ["Mr.", "Ms.", "Mrs.", "Mx.", "Dr."]},
    {"name": "date_of_birth", "type": "date", "description": "ISO date; leave null if partial or unreadable.", "source_field": None, "required": False, "enum_values": None},
    {"name": "address_line1", "type": "string", "description": "Street address, first line.", "source_field": None, "required": False, "enum_values": None},
    {"name": "address_line2", "type": "string", "description": "Apt / unit / suite — null if none.", "source_field": None, "required": False, "enum_values": None},
    {"name": "city", "type": "string", "description": "City or town.", "source_field": None, "required": False, "enum_values": None},
    {"name": "postal_code", "type": "string", "description": "ZIP or postal code as written.", "source_field": None, "required": False, "enum_values": None},
    {"name": "country", "type": "string", "description": "Country name.", "source_field": None, "required": False, "enum_values": None},
    {"name": "household_size", "type": "integer", "description": "Integer >= 1.", "source_field": None, "required": False, "enum_values": None},
    {"name": "annual_household_income", "type": "number", "description": "USD, no currency symbol; annualize monthly figures.", "source_field": None, "required": False, "enum_values": None},
    {"name": "insurance_status", "type": "enum", "description": "commercial_denied only when the page IS a denial letter.", "source_field": None, "required": False, "enum_values": ["uninsured", "medicare", "medicaid", "commercial", "commercial_denied", "unknown"]},
    {"name": "has_denial_letter", "type": "boolean", "description": "True only when the page IS a denial letter.", "source_field": None, "required": False, "enum_values": None},
    {"name": "country_of_residence", "type": "string", "description": "Country of residence (for CV-RES-01).", "source_field": None, "required": False, "enum_values": None},
    {"name": "mailing_state", "type": "string", "description": "US state name or 2-letter abbrev. Leave null for non-US applicants.", "source_field": None, "required": False, "enum_values": None},
    {"name": "prescribed_drug", "type": "string", "description": "Drug brand or generic name.", "source_field": None, "required": False, "enum_values": None},
    {"name": "prescriber_npi", "type": "string", "description": "10-digit NPI as string.", "source_field": None, "required": False, "enum_values": None},
    {"name": "prescription_date", "type": "date", "description": "ISO date.", "source_field": None, "required": False, "enum_values": None},
    {"name": "referral_id", "type": "string", "description": "Form-supplied application/reference number. Do not invent.", "source_field": None, "required": False, "enum_values": None},
]


def upgrade() -> None:
    op.create_table(
        "programs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "enrollment_template_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("template_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("extraction_schema", postgresql.JSONB(), nullable=True),
        sa.Column("inbound_alias", sa.String(255), nullable=True, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_programs_tenant_slug"),
    )

    # Seed cardiovita-pap-2026 for every existing tenant so in-flight PAP
    # traffic keeps resolving after the cutover. Uses raw SQL so the
    # migration doesn't need to import the ORM (which may drift from the
    # migration author's view of the schema).
    conn = op.get_bind()
    tenants = conn.execute(sa.text("SELECT id FROM tenants")).fetchall()
    schema_json = json.dumps(_CARDIOVITA_SLOTS)
    for (tenant_id,) in tenants:
        conn.execute(
            sa.text(
                """
                INSERT INTO programs
                    (id, tenant_id, slug, name, extraction_schema, is_active)
                VALUES
                    (:id, :tid, :slug, :name, CAST(:schema AS jsonb), true)
                ON CONFLICT (tenant_id, slug) DO NOTHING
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "slug": "cardiovita-pap-2026",
                "name": "CardioVita PAP 2026",
                "schema": schema_json,
            },
        )


def downgrade() -> None:
    op.drop_table("programs")
