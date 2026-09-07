"""Supersede workflow (Issue #1): clone a frozen docx version into a fresh
draft sub-version, edit required flags, refreeze.

Covers:
  * supersede_docx_version() clones every locale of a frozen version
  * The clone reuses blob_key, copies schema, starts as draft
  * apply_required_edits() only touches `required` flags
  * refreeze_docx_subversion() produces a new version_hash and doesn't
    require re-tagging
  * The parent frozen row is unchanged after supersede+refreeze
  * Guardrails: can't supersede an unfrozen or PDF version; can't refreeze
    sub 0 (needs the full mapping flow); can't refreeze twice
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from docgen.designtime.freeze import (
    apply_required_edits,
    freeze_version,
    refreeze_docx_subversion,
    supersede_docx_version,
)
from docgen.designtime.upload import upload_locale_variant, upload_template
from docgen.providers.base import FieldSpec, MappingProposal, Suggestion
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import Template, TemplateVersion, Tenant


@pytest.fixture
def isolated_tenant():
    tid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"sup-t-{tid.hex[:8]}"))
        s.commit()
    yield tid
    with sync_session() as s:
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


def _proposal_for_sample() -> MappingProposal:
    """Mapping proposal for the placeholders in sample_docx_bytes."""
    return MappingProposal(suggestions=[
        Suggestion(
            placeholder="[MEMBER_NAME]", jinja="{{ member_name }}",
            field=FieldSpec(name="member_name", type="string", required=True),
        ),
        Suggestion(
            placeholder="[CLAIM_ID]", jinja="{{ claim_id }}",
            field=FieldSpec(name="claim_id", type="string", required=True),
        ),
        Suggestion(
            placeholder="[CLAIM_DATE]", jinja="{{ claim_date }}",
            field=FieldSpec(name="claim_date", type="date", required=True),
        ),
        Suggestion(
            placeholder="[AMOUNT]", jinja="{{ amount }}",
            field=FieldSpec(name="amount", type="number", required=True),
        ),
    ])


# ---------------------------------------------------------------------------
# apply_required_edits — pure schema transform, no DB
# ---------------------------------------------------------------------------


def test_apply_required_edits_toggles_flag_only() -> None:
    schema = {"fields": [
        {"name": "a", "type": "string", "required": True, "description": "x"},
        {"name": "b", "type": "integer", "required": True, "description": None},
    ]}
    out = apply_required_edits(schema, {"a": False, "b": True})
    assert out["fields"][0]["required"] is False
    assert out["fields"][0]["type"] == "string"
    assert out["fields"][0]["description"] == "x"
    assert out["fields"][1]["required"] is True
    # Original untouched — apply_ returns a fresh dict.
    assert schema["fields"][0]["required"] is True


def test_apply_required_edits_ignores_unknown_names() -> None:
    schema = {"fields": [{"name": "a", "type": "string", "required": True}]}
    out = apply_required_edits(schema, {"a": False, "ghost": True})
    assert [f["name"] for f in out["fields"]] == ["a"]
    assert out["fields"][0]["required"] is False


def test_apply_required_edits_leaves_fields_absent_from_map_alone() -> None:
    schema = {"fields": [
        {"name": "a", "type": "string", "required": True},
        {"name": "b", "type": "string", "required": False},
    ]}
    out = apply_required_edits(schema, {"a": False})  # b omitted
    assert out["fields"][0]["required"] is False
    assert out["fields"][1]["required"] is False  # unchanged


# ---------------------------------------------------------------------------
# supersede_docx_version — DB + MinIO integration
# ---------------------------------------------------------------------------


def test_supersede_clones_default_locale(isolated_tenant, sample_docx_bytes) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            _template, base, _ = await upload_template(
                s, name="sup-basic", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            frozen = await freeze_version(
                s, version_id=base.id, proposal=_proposal_for_sample(),
            )
            new_rows = await supersede_docx_version(
                s, parent_version_id=frozen.id, actor="alice",
            )
            return frozen, new_rows

    frozen, new_rows = _run(_go())
    assert len(new_rows) == 1
    draft = new_rows[0]
    assert draft.version == frozen.version
    assert draft.subversion == frozen.subversion + 1
    assert draft.locale == "default"
    assert draft.is_frozen is False
    assert draft.status == "draft"
    assert draft.blob_key == frozen.blob_key  # reuses tagged bytes
    assert draft.schema == frozen.schema
    assert draft.id != frozen.id
    # Deep copy: mutating the draft's schema doesn't touch the parent's.
    draft.schema["fields"][0]["required"] = False
    assert frozen.schema["fields"][0]["required"] is True


def test_supersede_clones_all_locales(isolated_tenant, sample_docx_bytes) -> None:
    """Multi-locale version: supersede must clone every frozen locale row so
    the cross-locale field-set invariant continues to hold on refreeze."""
    async def _go():
        async with AsyncSessionLocal() as s:
            template, base, _ = await upload_template(
                s, name="sup-multi", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            # Add a second locale, then freeze both.
            variant, _ = await upload_locale_variant(
                s, template_id=template.id, tenant_id=isolated_tenant,
                version_number=base.version, locale="es-419",
                docx_bytes=sample_docx_bytes,
            )
            await freeze_version(
                s, version_id=base.id, proposal=_proposal_for_sample(),
            )
            await freeze_version(
                s, version_id=variant.id, proposal=_proposal_for_sample(),
            )
            # Grab the frozen default row again after freeze.
            from sqlalchemy import select
            row = (await s.execute(
                select(TemplateVersion).where(
                    TemplateVersion.template_id == template.id,
                    TemplateVersion.locale == "default",
                    TemplateVersion.is_frozen.is_(True),
                )
            )).scalar_one()
            new_rows = await supersede_docx_version(
                s, parent_version_id=row.id,
            )
            return new_rows

    new_rows = _run(_go())
    locales = sorted(r.locale for r in new_rows)
    assert locales == ["default", "es-419"]
    subs = {r.subversion for r in new_rows}
    assert subs == {1}
    for r in new_rows:
        assert r.is_frozen is False
        assert r.status == "draft"


def test_supersede_rejects_unfrozen_parent(isolated_tenant, sample_docx_bytes) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            _template, base, _ = await upload_template(
                s, name="sup-unfrozen", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            with pytest.raises(ValueError, match="not frozen"):
                await supersede_docx_version(s, parent_version_id=base.id)

    _run(_go())


def test_supersede_rejects_missing_version(isolated_tenant) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            with pytest.raises(ValueError, match="not found"):
                await supersede_docx_version(s, parent_version_id=uuid.uuid4())

    _run(_go())


# ---------------------------------------------------------------------------
# refreeze_docx_subversion — the whole loop
# ---------------------------------------------------------------------------


def test_refreeze_after_required_edit_produces_new_hash(
    isolated_tenant, sample_docx_bytes,
) -> None:
    """The classic supersede loop: freeze v1.0 → supersede → flip a required
    flag → refreeze → assert new hash, old row unchanged."""
    async def _go():
        async with AsyncSessionLocal() as s:
            _template, base, _ = await upload_template(
                s, name="sup-loop", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            v10 = await freeze_version(
                s, version_id=base.id, proposal=_proposal_for_sample(),
            )
            original_hash = v10.version_hash
            original_schema = {"fields": [dict(f) for f in v10.schema["fields"]]}

            drafts = await supersede_docx_version(
                s, parent_version_id=v10.id,
            )
            draft = drafts[0]

            # Flip member_name to optional.
            draft.schema = apply_required_edits(draft.schema, {"member_name": False})
            await s.commit()

            frozen11 = await refreeze_docx_subversion(
                s, version_id=draft.id, actor="bob",
            )
            # Re-fetch the parent to prove immutability.
            from sqlalchemy import select
            parent = (await s.execute(
                select(TemplateVersion).where(TemplateVersion.id == v10.id)
            )).scalar_one()
            return original_hash, original_schema, frozen11, parent

    original_hash, original_schema, frozen11, parent = _run(_go())
    assert frozen11.subversion == 1
    assert frozen11.is_frozen is True
    assert frozen11.status == "frozen"
    assert frozen11.version_hash != original_hash
    # Old row: schema + hash untouched.
    assert parent.version_hash == original_hash
    assert parent.schema == original_schema
    # New row: member_name is now optional.
    name_field = next(f for f in frozen11.schema["fields"] if f["name"] == "member_name")
    assert name_field["required"] is False


def test_refreeze_rejects_already_frozen(isolated_tenant, sample_docx_bytes) -> None:
    async def _go():
        async with AsyncSessionLocal() as s:
            _template, base, _ = await upload_template(
                s, name="sup-refreeze-twice", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            v10 = await freeze_version(
                s, version_id=base.id, proposal=_proposal_for_sample(),
            )
            drafts = await supersede_docx_version(s, parent_version_id=v10.id)
            draft = drafts[0]
            await refreeze_docx_subversion(s, version_id=draft.id)
            with pytest.raises(ValueError, match="already frozen"):
                await refreeze_docx_subversion(s, version_id=draft.id)

    _run(_go())


def test_refreeze_rejects_sub_zero(isolated_tenant, sample_docx_bytes) -> None:
    """Sub 0 rows must go through freeze_version (mapping proposal path).
    refreeze is a re-lock helper for cloned drafts only."""
    async def _go():
        async with AsyncSessionLocal() as s:
            _template, base, _ = await upload_template(
                s, name="sup-refreeze-sub0", docx_bytes=sample_docx_bytes,
                tenant_id=isolated_tenant,
            )
            # Uploaded but not frozen — sub=0, is_frozen=False.
            with pytest.raises(ValueError, match="Sub-version 0"):
                await refreeze_docx_subversion(s, version_id=base.id)

    _run(_go())
