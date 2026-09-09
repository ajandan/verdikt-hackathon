"""One-off backfill for `TemplateVersion.schema[fields][i].format`.

Runs the same format-inference call that freeze_pdf_version now issues,
against a template that was frozen BEFORE that step existed. Non-destructive:
it only merges the freshly-inferred `format` block into each field entry
and re-writes `schema`. Does NOT bump `version_hash` — the format spec is
descriptive metadata that has no effect on rendered output.

Usage:
    docker compose exec worker python scripts/backfill_format_infer.py <template_version_id>
"""
from __future__ import annotations

import json
import sys
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from docgen.config import get_settings
from docgen.designtime.format_infer import infer_formats, merge_formats_into_schema
from docgen.store.models import TemplateVersion


def main(version_id: str) -> None:
    settings = get_settings()
    # Reuse the async DB URL but drive it via the sync psycopg driver so this
    # script needs no asyncio boilerplate. The service password + db name are
    # baked into the same env the worker reads.
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg").replace(
        "postgresql+psycopg2", "postgresql+psycopg"
    )
    engine = create_engine(sync_url)

    fmt_base_url = settings.ai_base_url
    fmt_model = settings.pdf_labeler_model or settings.ai_model
    if settings.ai_provider == "nebius":
        from docgen.providers.nebius import DEFAULT_BASE_URL, DEFAULT_MODEL

        fmt_base_url = fmt_base_url or DEFAULT_BASE_URL
        fmt_model = fmt_model or DEFAULT_MODEL

    if not settings.ai_api_key or not fmt_base_url:
        raise SystemExit("No AI provider configured — aborting backfill.")

    with Session(engine) as session:
        tv = session.get(TemplateVersion, uuid.UUID(version_id))
        if tv is None:
            raise SystemExit(f"No template version {version_id}")
        placements = (tv.pdf_field_placements or {}).get("placements", [])
        seen: set[str] = set()
        unique_named: list[dict] = []
        for p in placements:
            n = p.get("name")
            if not n or n in seen:
                continue
            seen.add(n)
            unique_named.append(p)

        formats = infer_formats(
            tv.schema,
            unique_named,
            api_key=settings.ai_api_key,
            base_url=fmt_base_url,
            model=fmt_model,
            timeout_s=settings.ai_timeout_seconds,
            provider_name=settings.ai_provider,
        )
        if not formats:
            raise SystemExit("format-infer returned no rows — check worker logs")

        new_schema = merge_formats_into_schema(dict(tv.schema), formats)
        tv.schema = new_schema
        # SQLAlchemy needs a mutation flag for JSONB to persist since we replaced
        # a nested value inside the dict-of-dicts.
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(tv, "schema")
        session.commit()

        with_format = sum(
            1
            for f in (new_schema.get("fields") or [])
            if isinstance(f.get("format"), dict)
        )
        print(
            json.dumps(
                {
                    "version_id": version_id,
                    "field_count": len(new_schema.get("fields") or []),
                    "with_format": with_format,
                }
            )
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: backfill_format_infer.py <template_version_id>")
    main(sys.argv[1])
