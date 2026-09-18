"""Design-time step 4–5 (§6): apply confirmed mapping → tagged .docx, freeze the version.

The frozen tuple (tagged .docx + schema) is immutable. Any change = new version.
version_hash = sha256(tagged_bytes || canonical(schema)).
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile

import structlog
from lxml import etree
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.config import get_settings
from docgen.core.preflight import W_NS, preflight
from docgen.core.xml_safe import safe_fromstring
from docgen.core.schema_infer import FieldSpec, TemplateSchema
from docgen.designtime.audit import record_audit_event
from docgen.designtime.format_infer import infer_formats, merge_formats_into_schema
from docgen.providers.base import MappingProposal
from docgen.store.blob import get_object, put_object
from docgen.store.models import Template, TemplateVersion

_DOC_XML = "word/document.xml"
log = structlog.get_logger(__name__)


def _apply_replacements_to_document_xml(document_xml: str, replacements: dict[str, str]) -> str:
    """Paragraph-level replacement that survives run-splits.

    For each <w:p>, join all <w:t> text into one string, apply replacements, then put the
    entire new text into the first <w:t> and blank the rest. Paragraph formatting is preserved;
    per-run character formatting inside the affected paragraph is not.
    """
    if not replacements:
        return document_xml

    root = safe_fromstring(document_xml)
    for p in root.iter(f"{{{W_NS}}}p"):
        t_elements = p.findall(f".//{{{W_NS}}}t")
        if not t_elements:
            continue
        original = "".join((t.text or "") for t in t_elements)
        new = original
        for needle, jinja in replacements.items():
            if needle and needle in new:
                new = new.replace(needle, jinja)
        if new == original:
            continue
        t_elements[0].text = new
        # Preserve leading/trailing whitespace on the single surviving text node.
        t_elements[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        for t in t_elements[1:]:
            t.text = ""
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True).decode("utf-8")


def _rewrite_docx(docx_bytes: bytes, replacements: dict[str, str]) -> bytes:
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zin:
        parts = {name: zin.read(name) for name in zin.namelist()}

    if _DOC_XML not in parts:
        raise ValueError("word/document.xml missing — invalid .docx")

    original_xml = parts[_DOC_XML].decode("utf-8")
    new_xml = _apply_replacements_to_document_xml(original_xml, replacements)
    parts[_DOC_XML] = new_xml.encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in parts.items():
            zout.writestr(name, data)
    return out.getvalue()


def _canonical_schema(schema: dict) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _orphan_variables(detected: list[str], schema: dict) -> set[str]:
    """Top-level jinja variables that appear in the tagged docx but have no schema field.

    Freeze must reject these — an orphan is a variable the renderer will silently
    treat as undefined, producing wrong output that no runtime payload can fix.
    """
    schema_fields = {f.get("name") for f in schema.get("fields", []) if f.get("name")}
    return set(detected) - schema_fields


def _schema_field_names(schema: dict) -> set[str]:
    return {f.get("name") for f in schema.get("fields", []) if f.get("name")}


def _check_locale_schema_invariant(
    my_fields: set[str],
    siblings: list[tuple[str, set[str]]],
    version_number: int,
) -> None:
    """Raise ValueError if any sibling locale's field set differs from mine.

    `siblings` is a list of (locale, field_names) tuples for already-frozen
    variants of the same template + version_number.
    """
    for sib_locale, sib_fields in siblings:
        if sib_fields == my_fields:
            continue
        missing = sib_fields - my_fields
        extra = my_fields - sib_fields
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"extra {sorted(extra)}")
        raise ValueError(
            f"Freeze rejected: schema differs from sibling locale '{sib_locale}' "
            f"of v{version_number} ({'; '.join(parts)}). "
            "All locales of a version must share the same fields."
        )


def _version_hash(tagged_bytes: bytes, schema: dict) -> str:
    h = hashlib.sha256()
    h.update(tagged_bytes)
    h.update(b"|")
    h.update(_canonical_schema(schema))
    return h.hexdigest()


async def freeze_version(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    proposal: MappingProposal,
    actor: str | None = None,
) -> TemplateVersion:
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise ValueError(f"Template version {version_id} not found")

    # Serialize concurrent freeze/publish per-template so two callers can't
    # both race past the is_frozen check or write conflicting current_version
    # updates. Held until commit; cross-template ops stay parallel.
    # tenant-audit: allow (PK row-lock only; tenant scoping happens in the
    # REST/UI caller that resolved `version_id` for this tenant).
    await session.execute(
        select(Template.id).where(Template.id == tv.template_id).with_for_update()
    )
    await session.refresh(tv)

    if tv.is_frozen:
        raise ValueError("Version already frozen — create a new version to change mapping.")

    settings = get_settings()
    src = get_object(settings.s3_bucket_templates, tv.blob_key)

    replacements = {s.placeholder: s.jinja for s in proposal.suggestions if s.placeholder}
    tagged = _rewrite_docx(src, replacements)

    # Second preflight — after tag injection — is the guarantee that the frozen artifact renders.
    post = preflight(tagged)
    if post.outcome == "reject":
        raise ValueError(
            "Freeze rejected: post-injection preflight failed — " + str(post.to_dict())
        )

    schema = TemplateSchema(fields=proposal.fields()).model_dump(mode="json")

    orphans = _orphan_variables(post.detected_variables, schema)
    if orphans:
        raise ValueError(
            "Freeze rejected: template variables missing from mapping: "
            + ", ".join(sorted(orphans))
            + ". Add a mapping row for each in the review step, or remove the placeholder from the .docx."
        )

    # Cross-locale invariant: every frozen variant of this version number must
    # advertise the exact same set of schema fields — otherwise a caller sending
    # one JSON payload can't safely target different locales.
    siblings = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == tv.template_id,
            TemplateVersion.version == tv.version,
            TemplateVersion.is_frozen.is_(True),
            TemplateVersion.id != tv.id,
        )
    )
    my_fields = _schema_field_names(schema)
    sibling_pairs = [(sib.locale, _schema_field_names(sib.schema)) for sib in siblings.scalars()]
    _check_locale_schema_invariant(my_fields, sibling_pairs, tv.version)
    frozen_key = f"frozen/{tv.template_id}/v{tv.version}/{tv.locale}.docx"
    put_object(
        settings.s3_bucket_templates,
        frozen_key,
        tagged,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    tv.blob_key = frozen_key
    tv.schema = schema
    tv.preflight = post.to_dict()
    tv.version_hash = _version_hash(tagged, schema)
    tv.status = "frozen"
    tv.is_frozen = True

    # Phase 3 §draft/publish: freezing no longer auto-publishes. `current_version`
    # is set only via publish_version() so a frozen artifact can be reviewed
    # (or diffed) before it starts serving traffic.
    await record_audit_event(
        session,
        template_id=tv.template_id,
        version_id=tv.id,
        actor=actor,
        event_kind="frozen",
        summary={"field_count": len(schema.get("fields", []))},
    )
    await session.commit()
    await session.refresh(tv)
    log.info(
        "template_frozen",
        template_id=str(tv.template_id),
        version_id=str(tv.id),
        version=tv.version,
        version_hash=tv.version_hash,
        field_count=len(schema.get("fields", [])),
    )
    return tv


async def freeze_pdf_version(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    actor: str | None = None,
) -> TemplateVersion:
    """PDF sibling of freeze_version.

    No mapping proposal: for PDFs the placement name IS the payload key,
    so the schema is derived mechanically from pdf_field_placements.
    No text-injection into the source PDF — placements are pure metadata
    that the render pipeline consumes at merge time.

    version_hash covers source bytes + canonical placements + schema so
    an edit to any of the three produces a new hash. Placement edits are
    only possible pre-freeze; after freeze the tuple is immutable.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise ValueError(f"Template version {version_id} not found")

    # Same per-template lock as docx freeze — prevents two callers racing
    # past the is_frozen check.
    # tenant-audit: allow (PK row-lock only; see freeze_version above).
    await session.execute(
        select(Template.id).where(Template.id == tv.template_id).with_for_update()
    )
    await session.refresh(tv)

    if tv.pdf_flavor is None:
        raise ValueError("Version is not a PDF template — use freeze_version instead.")
    if tv.is_frozen:
        raise ValueError("Version already frozen — create a new version to change placements.")

    placements = (tv.pdf_field_placements or {}).get("placements", [])
    if not placements:
        raise ValueError(
            "Freeze rejected: no field placements. "
            "Open the placement editor to add fields, then freeze."
        )

    named_placements = [p for p in placements if p.get("name")]

    # Multiple widgets can legitimately share a name — radio-button groups
    # in the AcroForm spec use one field name across N widgets, one per
    # radio option. Dedupe by name for the schema so each unique name maps
    # to a single payload key; keep all widget positions in placements so
    # the renderer stamps every location.
    seen_names: set[str] = set()
    unique_named: list[dict] = []
    for p in named_placements:
        if p["name"] in seen_names:
            continue
        seen_names.add(p["name"])
        unique_named.append(p)
    dupe_count = len(named_placements) - len(unique_named)
    if dupe_count:
        log.info(
            "pdf_freeze_deduped_placement_names",
            version_id=str(tv.id),
            widget_count=len(named_placements),
            unique_field_count=len(unique_named),
            merged=dupe_count,
        )

    settings = get_settings()
    src = get_object(settings.s3_bucket_templates, tv.blob_key)

    fields = [_pdf_field_spec(p) for p in unique_named]
    schema = TemplateSchema(fields=fields).model_dump(mode="json")

    # One-shot format inference over the AcroForm's widget captions. Runs
    # once per freeze; result lives inside `schema["fields"][i]["format"]`
    # so runtime test-data generation reads it without re-parsing the PDF.
    # Soft-fail: a network hiccup or empty model response leaves formats
    # unset and downstream still runs on schema-only prompting.
    fmt_base_url = settings.ai_base_url
    fmt_model = settings.pdf_labeler_model or settings.ai_model
    if settings.ai_provider == "nebius":
        from docgen.providers.nebius import DEFAULT_BASE_URL, DEFAULT_MODEL

        fmt_base_url = fmt_base_url or DEFAULT_BASE_URL
        fmt_model = fmt_model or DEFAULT_MODEL
    if (
        settings.ai_api_key
        and fmt_base_url
        and settings.ai_provider in ("nebius", "openai_compat")
    ):
        try:
            formats = infer_formats(
                schema,
                unique_named,
                api_key=settings.ai_api_key,
                base_url=fmt_base_url,
                # Non-reasoning Nemotron-Super produces reliable one-shot
                # JSON. `ai_model` is often reasoning Nemotron-Nano, which
                # returns empty content on this prompt shape.
                model=fmt_model,
                timeout_s=settings.ai_timeout_seconds,
                provider_name=settings.ai_provider,
            )
            schema = merge_formats_into_schema(schema, formats)
            log.info(
                "pdf_freeze_format_infer_ok",
                version_id=str(tv.id),
                field_count=len(fields),
                inferred_count=len(formats),
            )
        except Exception as e:
            log.warning(
                "pdf_freeze_format_infer_error",
                version_id=str(tv.id),
                error=f"{type(e).__name__}: {e}",
            )

    # Cross-locale invariant — PDF templates default to locale='default' today,
    # but the check runs anyway so if locale variants ever land for PDFs the
    # guarantee is already in place.
    siblings = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == tv.template_id,
            TemplateVersion.version == tv.version,
            TemplateVersion.is_frozen.is_(True),
            TemplateVersion.id != tv.id,
        )
    )
    my_fields = _schema_field_names(schema)
    sibling_pairs = [(sib.locale, _schema_field_names(sib.schema)) for sib in siblings.scalars()]
    _check_locale_schema_invariant(my_fields, sibling_pairs, tv.version)

    # Rehost the source PDF under the frozen/ prefix so the draft blob can
    # be garbage-collected on future cleanup passes — parity with docx.
    frozen_key = f"frozen/{tv.template_id}/v{tv.version}/{tv.locale}.pdf"
    put_object(
        settings.s3_bucket_templates,
        frozen_key,
        src,
        content_type="application/pdf",
    )

    tv.blob_key = frozen_key
    tv.schema = schema
    tv.version_hash = _pdf_version_hash(src, placements, schema)
    tv.status = "frozen"
    tv.is_frozen = True

    await record_audit_event(
        session,
        template_id=tv.template_id,
        version_id=tv.id,
        actor=actor,
        event_kind="frozen",
        summary={"field_count": len(fields), "flavor": tv.pdf_flavor},
    )
    await session.commit()
    await session.refresh(tv)
    log.info(
        "pdf_template_frozen",
        template_id=str(tv.template_id),
        version_id=str(tv.id),
        version=tv.version,
        version_hash=tv.version_hash,
        flavor=tv.pdf_flavor,
        field_count=len(fields),
    )
    return tv


# Carry the placement's human-readable label into description so the runtime
# test-data generator can infer realistic values ("First Name" → John, "Date
# of Birth" → 05/12/1978) rather than falling back to "Sample field_xyz"
# from the payload key alone. payload_type lets checkbox placements freeze
# as booleans; defaults to string when the user hasn't set it (or the
# placement predates the dropdown).
def _pdf_field_spec(p: dict) -> FieldSpec:
    pt = p.get("payload_type")
    if pt not in ("string", "boolean"):
        pt = "boolean" if p.get("field_type") in ("checkbox", "radio") else "string"
    return FieldSpec(
        name=p["name"],
        type=pt,  # type: ignore[arg-type]
        required=True,
        description=(str(p.get("label") or "").strip() or None),
    )


# Hash covers all three components (source bytes + full placement list +
# derived schema) so any change to any of them produces a new hash. Called
# from both the initial freeze and the post-freeze re-save path.
def _pdf_version_hash(src: bytes, placements: list[dict], schema: dict) -> str:
    h = hashlib.sha256()
    h.update(src)
    h.update(b"|")
    h.update(_canonical_schema({"placements": placements}))
    h.update(b"|")
    h.update(_canonical_schema(schema))
    return h.hexdigest()


async def create_pdf_subversion(
    session: AsyncSession,
    *,
    parent_version_id: uuid.UUID,
    placements: list[dict],
    actor: str | None = None,
) -> TemplateVersion:
    """Clone a frozen PDF version into a new sub-version with edited placements.

    Preserves the parent row as immutable history. The new row shares the
    parent's `version` and locale, bumps `subversion` to
    max(existing_subs_for_this_version) + 1, and re-derives schema +
    version_hash from the caller-supplied placements. blob_key is reused
    (source PDF is unchanged).

    Rejects duplicate placement names — same reason as freeze_pdf_version:
    they'd make payload keys ambiguous.

    Only callable when the parent is the highest (version, subversion) for
    the template — the placement editor's lock check enforces this at the
    UI layer, but we also refuse mid-flight collisions here so two racing
    saves don't both try to become v1.2.
    """
    parent = await session.get(TemplateVersion, parent_version_id)
    if parent is None:
        raise ValueError(f"Template version {parent_version_id} not found")

    # Serialize per-template — two concurrent edits on the same frozen row
    # must produce (v1.1, v1.2), not two rows colliding on (v1.1).
    # tenant-audit: allow (PK row-lock only; see freeze_version above).
    await session.execute(
        select(Template.id).where(Template.id == parent.template_id).with_for_update()
    )
    await session.refresh(parent)

    if parent.pdf_flavor is None:
        raise ValueError("Parent version is not a PDF template.")
    if not parent.is_frozen:
        raise ValueError("Parent version is not frozen — edit it directly instead.")

    named_placements = [p for p in placements if p.get("name")]

    # Radio-button groups share one name across N widgets — dedupe by name
    # for the schema; all widgets stay in placements for rendering. Same
    # rationale as freeze_pdf_version above.
    seen_names: set[str] = set()
    unique_named: list[dict] = []
    for p in named_placements:
        if p["name"] in seen_names:
            continue
        seen_names.add(p["name"])
        unique_named.append(p)

    # Enforce "only highest sub is editable" server-side too. If a newer
    # sub exists for this (template, version, locale), reject — the editor
    # should have redirected the user before letting them save.
    max_sub_row = await session.execute(
        select(func.max(TemplateVersion.subversion)).where(
            TemplateVersion.template_id == parent.template_id,
            TemplateVersion.version == parent.version,
            TemplateVersion.locale == parent.locale,
        )
    )
    max_sub = max_sub_row.scalar() or 0
    if parent.subversion != max_sub:
        raise ValueError(
            f"v{parent.version}.{parent.subversion} is not the latest sub-version "
            f"(current tip is v{parent.version}.{max_sub}). Edit the tip instead."
        )

    settings = get_settings()
    src = get_object(settings.s3_bucket_templates, parent.blob_key)

    fields = [_pdf_field_spec(p) for p in unique_named]
    schema = TemplateSchema(fields=fields).model_dump(mode="json")

    new_sub = max_sub + 1
    new_tv = TemplateVersion(
        template_id=parent.template_id,
        version=parent.version,
        subversion=new_sub,
        locale=parent.locale,
        schema=schema,
        preflight=parent.preflight or {},
        # Rehost under a sub-versioned key so garbage collection of drafts
        # doesn't accidentally sweep a live sub. Source PDF bytes are the
        # same as parent, but the storage path encodes the sub number so
        # provenance stays legible in the blob store.
        blob_key=f"frozen/{parent.template_id}/v{parent.version}.{new_sub}/{parent.locale}.pdf",
        version_hash=_pdf_version_hash(src, placements, schema),
        status="frozen",
        is_frozen=True,
        pdf_flavor=parent.pdf_flavor,
        pdf_field_placements={"placements": placements},
        created_by=actor,
    )
    put_object(
        settings.s3_bucket_templates,
        new_tv.blob_key,
        src,
        content_type="application/pdf",
    )
    session.add(new_tv)
    await session.flush()

    await record_audit_event(
        session,
        template_id=new_tv.template_id,
        version_id=new_tv.id,
        actor=actor,
        event_kind="subversion_created",
        summary={
            "field_count": len(fields),
            "from_subversion": parent.subversion,
            "flavor": parent.pdf_flavor,
        },
    )
    await session.commit()
    await session.refresh(new_tv)
    log.info(
        "pdf_subversion_created",
        template_id=str(new_tv.template_id),
        parent_version_id=str(parent.id),
        new_version_id=str(new_tv.id),
        version=new_tv.version,
        subversion=new_tv.subversion,
        version_hash=new_tv.version_hash,
        field_count=len(fields),
    )
    return new_tv


async def supersede_docx_version(
    session: AsyncSession,
    *,
    parent_version_id: uuid.UUID,
    actor: str | None = None,
) -> list[TemplateVersion]:
    """Clone every locale of a frozen docx version into a fresh draft sub-version.

    The tagged .docx bytes and schema are copied verbatim — this only opens a
    draft the admin can then edit (currently: toggling `required` flags on
    schema fields) before re-freezing. The parent rows are preserved as
    immutable history and stay published/queryable.

    Locales are cloned together to keep the cross-locale invariant enforced by
    freeze: every locale of a (template, version) shares the same field set.
    """
    parent = await session.get(TemplateVersion, parent_version_id)
    if parent is None:
        raise ValueError(f"Template version {parent_version_id} not found")

    # Same per-template lock as freeze — two racing supersede calls must
    # produce (v1.1, v1.2), not two rows colliding on (v1.1).
    # tenant-audit: allow (PK row-lock only; see freeze_version above).
    await session.execute(
        select(Template.id).where(Template.id == parent.template_id).with_for_update()
    )
    await session.refresh(parent)

    if parent.pdf_flavor is not None:
        raise ValueError("PDF versions supersede via the placement editor, not this path.")
    if not parent.is_frozen:
        raise ValueError("Parent version is not frozen — edit it directly instead.")

    # Grab every frozen locale row at the parent's (version, subversion) tuple.
    # These are the exact source-of-truth artifacts for the new draft.
    sibling_rows = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == parent.template_id,
            TemplateVersion.version == parent.version,
            TemplateVersion.subversion == parent.subversion,
            TemplateVersion.is_frozen.is_(True),
        )
    )
    sources = list(sibling_rows.scalars().all())

    # The new sub number is one past the tip across ALL locales — otherwise
    # two locales could clone to the same sub number if the tip already
    # differs across locales (shouldn't happen today, but the guard is cheap).
    max_sub_row = await session.execute(
        select(func.max(TemplateVersion.subversion)).where(
            TemplateVersion.template_id == parent.template_id,
            TemplateVersion.version == parent.version,
        )
    )
    max_sub = max_sub_row.scalar() or 0
    new_sub = max_sub + 1

    new_rows: list[TemplateVersion] = []
    for src in sources:
        # blob_key is reused verbatim — the tagged .docx bytes don't change.
        # Only the schema (specifically `required` flags) is up for edit.
        new_tv = TemplateVersion(
            template_id=src.template_id,
            version=src.version,
            subversion=new_sub,
            locale=src.locale,
            schema=json.loads(json.dumps(src.schema)),
            preflight=src.preflight or {},
            blob_key=src.blob_key,
            version_hash=src.version_hash,
            status="draft",
            is_frozen=False,
            pdf_flavor=None,
            pdf_field_placements=None,
            created_by=actor,
        )
        session.add(new_tv)
        new_rows.append(new_tv)

    await session.flush()

    for new_tv in new_rows:
        await record_audit_event(
            session,
            template_id=new_tv.template_id,
            version_id=new_tv.id,
            actor=actor,
            event_kind="subversion_created",
            summary={
                "field_count": len(new_tv.schema.get("fields", [])),
                "from_subversion": parent.subversion,
            },
        )
    await session.commit()
    for new_tv in new_rows:
        await session.refresh(new_tv)
    log.info(
        "docx_supersede_drafted",
        template_id=str(parent.template_id),
        parent_version=parent.version,
        parent_subversion=parent.subversion,
        new_subversion=new_sub,
        locales=[r.locale for r in new_rows],
    )
    return new_rows


async def refreeze_docx_subversion(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    actor: str | None = None,
) -> TemplateVersion:
    """Freeze a superseded draft: no re-tagging, just re-lock schema + hash.

    The draft was cloned from a frozen row (see `supersede_docx_version`), so
    the tagged .docx bytes are already locked-in. The schema may have been
    edited between clone and freeze (currently: `required` flags). This path
    reads the current bytes from S3, recomputes `version_hash`, flips
    is_frozen=True, and enforces the same cross-locale field-set invariant as
    a fresh freeze.

    Kept distinct from `freeze_version` because that path requires a
    `MappingProposal` and re-tags the source .docx — neither applies here.
    """
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise ValueError(f"Template version {version_id} not found")

    # tenant-audit: allow (PK row-lock only; see freeze_version above).
    await session.execute(
        select(Template.id).where(Template.id == tv.template_id).with_for_update()
    )
    await session.refresh(tv)

    if tv.pdf_flavor is not None:
        raise ValueError("PDF versions freeze via freeze_pdf_version, not this path.")
    if tv.is_frozen:
        raise ValueError("Version already frozen — create another sub-version to change it.")
    if tv.subversion == 0:
        # Refreeze is a re-lock; sub 0 rows have never gone through freeze and
        # need the full `freeze_version` path (mapping proposal + re-tagging).
        raise ValueError("Sub-version 0 must be frozen via the mapping flow, not refreeze.")

    settings = get_settings()
    src = get_object(settings.s3_bucket_templates, tv.blob_key)

    # Re-run cross-locale invariant. If the admin edited required flags on
    # only one locale of a multi-locale version, the sibling draft(s) still
    # have the old flags — that's fine for `required` (field set unchanged),
    # but the guard runs anyway so future edits that could add/drop fields
    # are caught.
    siblings = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == tv.template_id,
            TemplateVersion.version == tv.version,
            TemplateVersion.is_frozen.is_(True),
            TemplateVersion.id != tv.id,
        )
    )
    my_fields = _schema_field_names(tv.schema)
    sibling_pairs = [(sib.locale, _schema_field_names(sib.schema)) for sib in siblings.scalars()]
    _check_locale_schema_invariant(my_fields, sibling_pairs, tv.version)

    tv.version_hash = _version_hash(src, tv.schema)
    tv.status = "frozen"
    tv.is_frozen = True

    await record_audit_event(
        session,
        template_id=tv.template_id,
        version_id=tv.id,
        actor=actor,
        event_kind="frozen",
        summary={"field_count": len(tv.schema.get("fields", []))},
    )
    await session.commit()
    await session.refresh(tv)
    log.info(
        "docx_subversion_frozen",
        template_id=str(tv.template_id),
        version_id=str(tv.id),
        version=tv.version,
        subversion=tv.subversion,
        locale=tv.locale,
        version_hash=tv.version_hash,
    )
    return tv


def apply_required_edits(schema: dict, required_map: dict[str, bool]) -> dict:
    """Return a new schema with `required` flags updated per `required_map`.

    Keys in `required_map` that don't match any field are ignored — the caller
    (a form POST) shouldn't be able to invent fields, but silently ignoring
    stale keys is friendlier than a 500 if a browser POST races with a
    concurrent schema change.
    """
    fields = []
    for f in schema.get("fields", []):
        name = f.get("name")
        new_field = dict(f)
        if name in required_map:
            new_field["required"] = bool(required_map[name])
        fields.append(new_field)
    return {**schema, "fields": fields}
