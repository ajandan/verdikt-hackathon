"""One-shot backfill: retype PDF template schema fields for checkbox widgets.

Before this ran, upload.py hard-coded every PDF-derived schema field to
"string" — including checkboxes and radios. Now the mapping treats /Btn
widgets as boolean. This script walks existing PDF template versions and
rewrites their schema so callers get the right validation contract.

Frozen versions are patched too: this is a bug fix to the schema, not a
semantic change. Anyone rendering with a boolean payload gets the right
answer now (X in the checkbox) rather than "Yes" text overflowing.
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from docgen.store.db import sync_session
from docgen.store.models import Template, TemplateVersion


def main() -> int:
    changed_versions = 0
    changed_fields = 0
    with sync_session() as session:
        result = session.execute(
            select(TemplateVersion).where(TemplateVersion.pdf_flavor.is_not(None))
        )
        for tv in result.scalars().all():
            placements = (tv.pdf_field_placements or {}).get("placements") or []
            checkbox_names = {
                p["name"] for p in placements if p.get("field_type") == "checkbox"
            }
            if not checkbox_names:
                continue
            fields = list((tv.schema or {}).get("fields") or [])
            if not fields:
                continue
            local_changes = 0
            new_fields = []
            for f in fields:
                if f.get("name") in checkbox_names and f.get("type") != "boolean":
                    new_fields.append({**f, "type": "boolean"})
                    local_changes += 1
                else:
                    new_fields.append(f)
            if local_changes:
                tv.schema = {**(tv.schema or {}), "fields": new_fields}
                flag_modified(tv, "schema")
                changed_versions += 1
                changed_fields += local_changes
                tname = session.get(Template, tv.template_id).name
                frozen = "frozen" if tv.is_frozen else "draft"
                print(
                    f"  {tname} v{tv.version} ({tv.locale}, {frozen}): "
                    f"{local_changes} field(s) → boolean"
                )
        if changed_versions:
            session.commit()
    print(f"\nUpdated {changed_fields} field(s) across {changed_versions} version(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
