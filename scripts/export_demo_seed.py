"""Export the Alderwood demo tenant to deploy/demo-seed/ for scripts/seed_demo.py.

Writes seed.json (one list of rows per table, original ids kept so every
foreign key and every reference inside a JSON column still lines up) and the
template / guideline PDFs the rows point at.

Only what the demo needs is exported: the tenant, its program, rules, the
enrollment form and the three reply letters, the four guideline PDFs, and the
three doc packages. API keys, admin users, inbox events and the throwaway test
templates are left out, and personal addresses are scrubbed.

Run it where the database and object store are reachable (the web container):
    python scripts/export_demo_seed.py
Env:
    ALDERWOOD_TENANT_NAME  tenant to export (default "Alderwood Specialty Pharmacy")
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import MetaData, create_engine, select

from docgen.config import get_settings
from docgen.store.blob import get_object

OUT = Path(__file__).resolve().parents[1] / "deploy" / "demo-seed"
TENANT_NAME = os.environ.get("ALDERWOOD_TENANT_NAME", "Alderwood Specialty Pharmacy")
# Guideline PDFs the three packages use. The throwaway "test" document is skipped.
STATIC_DOC_NAMES = {
    "Appeals rights notice",
    "CardioVita program overview",
    "Prescription fulfillment guide",
    "Missing documents checklist",
}


def _jsonable(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        raise TypeError("binary column in export")
    return v


def _rows(conn, table, where) -> list[dict]:
    out = []
    for r in conn.execute(select(table).where(where)).mappings():
        out.append({k: _jsonable(v) for k, v in r.items()})
    return out


def main() -> None:
    s = get_settings()
    meta = MetaData()
    engine = create_engine(s.database_url_sync)
    meta.reflect(engine)
    t = meta.tables

    with engine.connect() as c:
        tenant = c.execute(select(t["tenants"]).where(t["tenants"].c.name == TENANT_NAME)).mappings().one()
        tid = tenant["id"]

        tenants = [{k: _jsonable(v) for k, v in tenant.items()}]
        # Scrub: no personal addresses in a public repo.
        cfg = dict(tenants[0]["inbound_config"] or {})
        cfg["notify_addresses"] = []
        tenants[0]["inbound_config"] = cfg
        tenants[0]["key_ref"] = None
        tenants[0]["ai_provider_config"] = {}

        programs = _rows(c, t["programs"], t["programs"].c.tenant_id == tid)
        rules = _rows(c, t["rules"], t["rules"].c.tenant_id == tid)
        rule_ids = [uuid.UUID(r["id"]) for r in rules]
        program_ids = [uuid.UUID(p["id"]) for p in programs]
        rule_versions = _rows(c, t["rule_versions"], t["rule_versions"].c.rule_id.in_(rule_ids))
        links = _rows(c, t["program_rule_links"], t["program_rule_links"].c.program_id.in_(program_ids))

        packages = _rows(c, t["doc_packages"], t["doc_packages"].c.tenant_id == tid)
        template_ids: set[str] = set()
        static_ids: set[str] = set()
        for p in packages:
            for item in p["items"] or []:
                (template_ids if item["kind"] == "template" else static_ids).add(item["ref_id"])
        # The program's enrollment form is a template too.
        enroll_vids = [p["enrollment_template_version_id"] for p in programs if p["enrollment_template_version_id"]]
        tv_all = t["template_versions"]
        for vid in enroll_vids:
            row = c.execute(select(tv_all.c.template_id).where(tv_all.c.id == uuid.UUID(vid))).first()
            if row:
                template_ids.add(str(row[0]))

        templates = _rows(c, t["templates"], t["templates"].c.id.in_([uuid.UUID(x) for x in template_ids]))
        template_versions = _rows(c, tv_all, tv_all.c.template_id.in_([uuid.UUID(x) for x in template_ids]))

        sd = t["static_documents"]
        static_docs = [
            r for r in _rows(c, sd, sd.c.tenant_id == tid) if r["name"] in STATIC_DOC_NAMES
        ]
        static_ids |= {r["id"] for r in static_docs}
        static_docs = [r for r in static_docs if r["id"] in static_ids]
        sdv = t["static_document_versions"]
        static_versions = _rows(
            c, sdv, sdv.c.static_document_id.in_([uuid.UUID(r["id"]) for r in static_docs])
        )

    # Packages may only reference what is exported; fail loudly if not.
    have = {x["id"] for x in templates} | {x["id"] for x in static_docs}
    for p in packages:
        for item in p["items"] or []:
            if item["ref_id"] not in have:
                raise SystemExit(f"package {p['name']!r} references {item['ref_id']} which is not exported")

    # Scrub who-did-it fields: they hold the author's own address or user id.
    for r in templates:
        r["owner"] = "demo-seed"
    for r in template_versions:
        r["created_by"] = "demo-seed"
    for r in static_versions:
        r["uploaded_by"] = None
    for r in rule_versions:
        r["actor_user_id"] = None
        r["actor_username"] = "demo-seed"

    blobs: list[dict] = []
    for v in template_versions:
        blobs.append({"kind": "templates", "key": v["blob_key"]})
    for v in static_versions:
        blobs.append({"kind": "documents", "key": v["blob_key"]})

    (OUT / "blobs").mkdir(parents=True, exist_ok=True)
    buckets = {"templates": s.s3_bucket_templates, "documents": s.s3_bucket_documents}
    for b in blobs:
        dest = OUT / "blobs" / b["kind"] / b["key"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(get_object(buckets[b["kind"]], b["key"]))

    seed = {
        "tenants": tenants,
        "templates": templates,
        "template_versions": template_versions,
        "static_documents": static_docs,
        "static_document_versions": static_versions,
        "doc_packages": packages,
        "programs": programs,
        "rules": rules,
        "rule_versions": rule_versions,
        "program_rule_links": links,
        "_blobs": blobs,
    }
    (OUT / "seed.json").write_text(json.dumps(seed, indent=1, sort_keys=True), encoding="utf-8")
    for k, v in seed.items():
        print(f"{k:26} {len(v)}")


if __name__ == "__main__":
    main()
