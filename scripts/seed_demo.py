"""Load the Alderwood demo tenant from deploy/demo-seed/ into an empty install.

Inserts the rows from seed.json (ids preserved, so references line up),
uploads the PDFs to the object store, and mints one API key for the tenant.
Safe to run twice: existing rows are left alone and an existing key is kept.

Run after `alembic upgrade head`, in the web container:
    python scripts/seed_demo.py
The API key is printed once and cannot be shown again.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import MetaData, create_engine, func, select
from sqlalchemy.dialects.postgresql import insert

from docgen.auth import generate_api_key
from docgen.config import get_settings
from docgen.store.blob import ensure_bucket, put_object

SEED = Path(__file__).resolve().parents[1] / "deploy" / "demo-seed"
# Parents before children so foreign keys resolve.
ORDER = [
    "tenants",
    "templates",
    "template_versions",
    "static_documents",
    "static_document_versions",
    "doc_packages",
    "programs",
    "rules",
    "rule_versions",
    "program_rule_links",
]


def _coerce(table, row: dict) -> dict:
    """Turn JSON strings back into the python types the column expects."""
    out = {}
    for name, value in row.items():
        if value is None or name not in table.c:
            out[name] = value
            continue
        try:
            py = table.c[name].type.python_type
        except NotImplementedError:
            py = None
        if py is uuid.UUID and isinstance(value, str):
            value = uuid.UUID(value)
        elif py is dt.datetime and isinstance(value, str):
            value = dt.datetime.fromisoformat(value)
        out[name] = value
    return out


def main() -> None:
    seed = json.loads((SEED / "seed.json").read_text(encoding="utf-8"))
    s = get_settings()
    meta = MetaData()
    engine = create_engine(s.database_url_sync)
    meta.reflect(engine)

    with engine.begin() as conn:
        for name in ORDER:
            table = meta.tables[name]
            before = conn.execute(select(func.count()).select_from(table)).scalar_one()
            for row in seed[name]:
                conn.execute(insert(table).values(**_coerce(table, row)).on_conflict_do_nothing())
            after = conn.execute(select(func.count()).select_from(table)).scalar_one()
            added = after - before
            print(f"{name:26} {added} added, {len(seed[name]) - added} already there")

        tenant_id = uuid.UUID(seed["tenants"][0]["id"])
        keys = meta.tables["api_keys"]
        if conn.execute(select(keys.c.id).where(keys.c.tenant_id == tenant_id)).first():
            print("api key                    exists, not minting another")
        else:
            raw, prefix, digest = generate_api_key()
            conn.execute(
                keys.insert().values(
                    id=uuid.uuid4(), tenant_id=tenant_id, name="demo-seed",
                    key_prefix=prefix, key_hash=digest,
                )
            )
            print(f"\nAPI key (shown once): {raw}\n")

    buckets = {"templates": s.s3_bucket_templates, "documents": s.s3_bucket_documents}
    for bucket in buckets.values():
        ensure_bucket(bucket)
    for b in seed["_blobs"]:
        body = (SEED / "blobs" / b["kind"] / b["key"]).read_bytes()
        put_object(buckets[b["kind"]], b["key"], body, content_type="application/pdf")
    print(f"uploaded {len(seed['_blobs'])} PDFs")


if __name__ == "__main__":
    main()
