"""End-to-end happy path: upload → map → freeze → publish → render → download.

Walks the whole REST surface with a single TestClient so a regression anywhere
along the pipeline surfaces here. Locks in the product loop so we can refactor
freely — anything that breaks upload/preflight/mapping/freeze/publish/render
fails one test.

Runs against the same real Postgres + MinIO stack the other integration tests
use. Uses the deterministic stub AI provider (default in tests), so mapping
suggestions are stable across runs.
"""
from __future__ import annotations

import io
import uuid
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from docgen.main import app
from docgen.store.db import sync_session
from docgen.store.models import Provenance, RenderJob, Template, TemplateVersion


def _drop_template(template_id: uuid.UUID) -> None:
    """Templates cascade to versions, but jobs + provenance don't."""
    with sync_session() as s:
        job_ids = s.execute(
            select(RenderJob.id)
            .join(TemplateVersion, TemplateVersion.id == RenderJob.template_version_id)
            .where(TemplateVersion.template_id == template_id)
        ).scalars().all()
        if job_ids:
            s.execute(delete(Provenance).where(Provenance.render_job_id.in_(job_ids)))
            s.execute(delete(RenderJob).where(RenderJob.id.in_(job_ids)))
        tmpl = s.get(Template, template_id)
        if tmpl is not None:
            s.delete(tmpl)
        s.commit()


def test_full_pipeline_upload_to_download(
    auth_headers, default_tenant, sample_docx_bytes
) -> None:
    template_id: uuid.UUID | None = None
    try:
        with TestClient(app) as client:
            # 1. Upload -----------------------------------------------------------
            r_up = client.post(
                "/templates",
                headers=auth_headers,
                data={"name": f"e2e-{uuid.uuid4().hex[:6]}", "owner": "e2e@test"},
                files={"file": (
                    "sample.docx",
                    sample_docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )},
            )
            assert r_up.status_code == 201, r_up.text
            up = r_up.json()
            assert up["accepted"] is True, up["preflight"]
            template_id = uuid.UUID(up["template_id"])
            version_id = up["version_id"]
            version_number = up["version"]

            # 2. Map --------------------------------------------------------------
            r_map = client.post(
                f"/templates/{version_id}/map", headers=auth_headers
            )
            assert r_map.status_code == 200, r_map.text
            suggestions = r_map.json()["proposal"]["suggestions"]
            # sample_docx_bytes has 4 bracket + 1 angle placeholder → 5 fields.
            field_names = {s["field"]["name"] for s in suggestions}
            assert field_names == {
                "member_name", "claim_id", "claim_date", "amount", "agent_name"
            }, field_names

            # 3. Freeze -----------------------------------------------------------
            r_freeze = client.post(
                f"/templates/{version_id}/freeze",
                headers=auth_headers,
                json={"suggestions": suggestions, "notes": "e2e"},
            )
            assert r_freeze.status_code == 200, r_freeze.text
            frozen = r_freeze.json()
            assert frozen["is_frozen"] is True
            assert {f["name"] for f in frozen["schema"]["fields"]} == field_names

            # 4. Publish ----------------------------------------------------------
            r_pub = client.post(
                f"/templates/{template_id}/publish",
                headers=auth_headers,
                json={"version": version_number, "actor": "e2e@test"},
            )
            assert r_pub.status_code == 200, r_pub.text
            assert r_pub.json()["event_kind"] == "publish"

            # 5. Render (sync, inline) --------------------------------------------
            payload = {
                "member_name": "Alice Example",
                "claim_id": "CLM-42",
                "claim_date": "2026-08-24",
                "amount": 123.45,
                "agent_name": "Bob Ombudsman",
            }
            r_render = client.post(
                "/render",
                headers=auth_headers,
                json={"template_id": str(template_id), "data": payload},
            )
            assert r_render.status_code == 200, r_render.text
            rr = r_render.json()
            assert rr["state"] == "succeeded"
            assert rr["inline"] is True
            assert rr["output_format"] == "docx"
            job_id = rr["job_id"]

            # 6. Download & verify contents ---------------------------------------
            r_dl = client.get(f"/jobs/{job_id}/result", headers=auth_headers)
            assert r_dl.status_code == 200, r_dl.text
            assert r_dl.headers["content-type"].startswith(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            body = r_dl.content
            # DOCX is a ZIP — first bytes must be PK\x03\x04.
            assert body[:4] == b"PK\x03\x04", body[:8]

            # Rendered field values must appear in word/document.xml.
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                doc_xml = zf.read("word/document.xml").decode("utf-8")
            for value in ("Alice Example", "CLM-42", "2026-08-24", "Bob Ombudsman"):
                assert value in doc_xml, f"missing '{value}' in rendered document"
            # Numeric values may be re-formatted; assert the integer part is present.
            assert "123" in doc_xml
    finally:
        if template_id is not None:
            _drop_template(template_id)
