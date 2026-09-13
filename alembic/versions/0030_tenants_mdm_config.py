"""tenants.mdm_config — per-tenant MDM connector configuration

Adds the tenant-scoped MDM connector config called out in ADR-0004.
Every tenant carries its own :class:`MdmSourceConfig` shape as JSONB so
the pipeline can pick the right adapter (null / simulated / real REST)
per tenant without a redeploy.

Schema of ``mdm_config`` when populated::

  {
    "provider": "simulated" | "null" | "state-mdm-v1" | ...,
    "base_url": "https://mdm.example.com/api",   # required when provider is http
    "api_key_env": "TENANT_ACME_MDM_KEY",        # env var name; never the raw key
    "program_families": ["cardiovita", ...],     # empty tuple = all
    "max_window_days": 1825,
    "max_events": 200,
    "timeout": 30.0
  }

Empty dict (default) → the hackathon build resolves to
``SimulatedMdmSource`` so new tenants demo end-to-end without setup.
Operators explicitly wire ``provider: "null"`` when they want no MDM.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-13
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "mdm_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "mdm_config")
