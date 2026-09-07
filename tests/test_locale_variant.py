"""Phase 4 (§14): locale variants share a version number.

Covers:
  * upload_locale_variant() — happy path + guard rails
  * freeze cross-locale schema invariant helper
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from docgen.designtime.freeze import _check_locale_schema_invariant
from docgen.designtime.upload import upload_locale_variant, upload_template
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import Template, TemplateVersion, Tenant

# ---------------------------------------------------------------------------
# _check_locale_schema_invariant
# ---------------------------------------------------------------------------


def test_invariant_passes_when_field_sets_match() -> None:
    _check_locale_schema_invariant({"a", "b"}, [("en", {"a", "b"}), ("es", {"b", "a"})], 3)


def test_invariant_passes_when_no_siblings() -> None:
    _check_locale_schema_invariant({"a"}, [], 1)


def test_invariant_rejects_when_sibling_has_extra_field() -> None:
    with pytest.raises(ValueError) as ei:
        _check_locale_schema_invariant({"a"}, [("en", {"a", "b"})], 2)
    msg = str(ei.value)
    assert "sibling locale 'en'" in msg
    assert "v2" in msg
    assert "missing ['b']" in msg


def test_invariant_rejects_when_mine_has_extra_field() -> None:
    with pytest.raises(ValueError) as ei:
        _check_locale_schema_invariant({"a", "c"}, [("de", {"a"})], 5)
    msg = str(ei.value)
    assert "sibling locale 'de'" in msg
    assert "extra ['c']" in msg


def test_invariant_reports_both_missing_and_extra() -> None:
    with pytest.raises(ValueError) as ei:
        _check_locale_schema_invariant({"a", "c"}, [("fr", {"a", "b"})], 1)
    msg = str(ei.value)
    assert "missing ['b']" in msg
    assert "extra ['c']" in msg


# ---------------------------------------------------------------------------
# upload_locale_variant — integration against real DB + MinIO
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_tenant():
    tid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"locale-t-{tid.hex[:8]}"))
        s.commit()
    yield tid
    with sync_session() as s:
        # Templates cascade to versions via FK ondelete=CASCADE; kill them first
        # so the tenant delete doesn't trip fk_templates_tenant.
        for tmpl in s.query(Template).filter(Template.tenant_id == tid).all():
            s.delete(tmpl)
        s.commit()
        t = s.get(Tenant, tid)
        if t is not None:
            s.delete(t)
            s.commit()


def _run(coro):
    async def _wrap():
        try:
            return await coro
        finally:
            await async_engine.dispose()
    return asyncio.run(_wrap())


def test_upload_locale_variant_creates_sibling_row(isolated_tenant, sample_docx_bytes) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="loc-test", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            tv, _report = await upload_locale_variant(
                s,
                template_id=template.id,
                tenant_id=isolated_tenant,
                version_number=base.version,
                locale="es-419",
                docx_bytes=sample_docx_bytes,
            )
            return template.id, base.id, base.version, tv.id, tv.locale, tv.version

    tid, base_id, base_ver, var_id, var_locale, var_ver = _run(_go())
    assert var_locale == "es-419"
    assert var_ver == base_ver
    assert var_id != base_id

    with sync_session() as s:
        rows = (
            s.query(TemplateVersion)
            .filter(TemplateVersion.template_id == tid)
            .order_by(TemplateVersion.locale)
            .all()
        )
        assert [(r.locale, r.version) for r in rows] == [
            ("default", base_ver),
            ("es-419", base_ver),
        ]


def test_upload_locale_variant_rejects_duplicate_locale(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="dup-loc", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            await upload_locale_variant(
                s,
                template_id=template.id,
                tenant_id=isolated_tenant,
                version_number=base.version,
                locale="en-GB",
                docx_bytes=sample_docx_bytes,
            )
            with pytest.raises(ValueError, match="already exists"):
                await upload_locale_variant(
                    s,
                    template_id=template.id,
                    tenant_id=isolated_tenant,
                    version_number=base.version,
                    locale="en-GB",
                    docx_bytes=sample_docx_bytes,
                )

    _run(_go())


def test_upload_locale_variant_rejects_default_locale(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="def-loc", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            with pytest.raises(ValueError, match="non-default tag"):
                await upload_locale_variant(
                    s,
                    template_id=template.id,
                    tenant_id=isolated_tenant,
                    version_number=base.version,
                    locale="default",
                    docx_bytes=sample_docx_bytes,
                )
            with pytest.raises(ValueError, match="non-default tag"):
                await upload_locale_variant(
                    s,
                    template_id=template.id,
                    tenant_id=isolated_tenant,
                    version_number=base.version,
                    locale="",
                    docx_bytes=sample_docx_bytes,
                )

    _run(_go())


def test_upload_locale_variant_rejects_unknown_version(
    isolated_tenant, sample_docx_bytes
) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            template, _, _ = await upload_template(
                s, name="ver-loc", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            with pytest.raises(ValueError, match="does not exist"):
                await upload_locale_variant(
                    s,
                    template_id=template.id,
                    tenant_id=isolated_tenant,
                    version_number=999,
                    locale="fr",
                    docx_bytes=sample_docx_bytes,
                )

    _run(_go())


def test_upload_locale_variant_rejects_wrong_tenant(
    isolated_tenant, sample_docx_bytes
) -> None:
    other_tenant = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=other_tenant, name=f"other-{other_tenant.hex[:8]}"))
        s.commit()

    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="t-loc", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            with pytest.raises(ValueError, match="does not exist"):
                await upload_locale_variant(
                    s,
                    template_id=template.id,
                    tenant_id=other_tenant,
                    version_number=base.version,
                    locale="fr",
                    docx_bytes=sample_docx_bytes,
                )

    try:
        _run(_go())
    finally:
        with sync_session() as s:
            for tmpl in s.query(Template).filter(Template.tenant_id == other_tenant).all():
                s.delete(tmpl)
            s.commit()
            t = s.get(Tenant, other_tenant)
            if t is not None:
                s.delete(t)
                s.commit()


# ---------------------------------------------------------------------------
# _resolve_frozen_version — render routing by locale
# ---------------------------------------------------------------------------


def test_resolver_defaults_to_default_locale_when_none_requested(
    isolated_tenant, sample_docx_bytes
) -> None:
    """When no locale is passed, resolver must land on the 'default' variant."""
    from fastapi import HTTPException

    from docgen.api.routes import _resolve_frozen_version

    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="rl1", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            # Fake a "frozen" default so the resolver returns it. We bypass
            # freeze_version() (which needs a real proposal) by hand-flipping the row.
            base.is_frozen = True
            base.status = "frozen"
            template.current_version = base.version
            await s.commit()

            tv = await _resolve_frozen_version(
                s, template.id, None, isolated_tenant, locale=None
            )
            assert tv.locale == "default"
            assert tv.id == base.id

            # Explicit "default" also works.
            tv = await _resolve_frozen_version(
                s, template.id, base.version, isolated_tenant, locale="default"
            )
            assert tv.id == base.id

            # Missing locale variant → 404, not the default fallback.
            with pytest.raises(HTTPException) as ei:
                await _resolve_frozen_version(
                    s, template.id, base.version, isolated_tenant, locale="es-419"
                )
            assert ei.value.status_code == 404
            assert "es-419" in ei.value.detail

    _run(_go())


def test_resolver_picks_correct_locale_when_variant_exists(
    isolated_tenant, sample_docx_bytes
) -> None:
    from docgen.api.routes import _resolve_frozen_version

    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="rl2", docx_bytes=sample_docx_bytes, tenant_id=isolated_tenant
            )
            variant, _ = await upload_locale_variant(
                s,
                template_id=template.id,
                tenant_id=isolated_tenant,
                version_number=base.version,
                locale="es-419",
                docx_bytes=sample_docx_bytes,
            )
            # Freeze both by hand — resolver only requires is_frozen=True.
            base.is_frozen = True
            variant.is_frozen = True
            template.current_version = base.version
            await s.commit()

            tv_default = await _resolve_frozen_version(
                s, template.id, base.version, isolated_tenant, locale=None
            )
            assert tv_default.locale == "default"
            assert tv_default.id == base.id

            tv_es = await _resolve_frozen_version(
                s, template.id, base.version, isolated_tenant, locale="es-419"
            )
            assert tv_es.locale == "es-419"
            assert tv_es.id == variant.id
            assert tv_es.blob_key != tv_default.blob_key

    _run(_go())
