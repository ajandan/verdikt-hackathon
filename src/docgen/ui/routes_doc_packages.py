"""Admin routes for static documents + doc packages + program bindings.

Split into its own module because `docgen/ui/routes.py` is already large and
this feature is a self-contained slice (models + storage helpers + templates
+ routes all landed together for ADR-0003).

The functions here are wired onto the main `router` by
`docgen.ui.routes_register_doc_packages(router)` — called from the same
module that assembles the FastAPI app. Kept as free functions taking the
router so the module has no import cycle with `docgen.ui.routes`.
"""
from __future__ import annotations

import io
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.api.deps import get_session
from docgen.config import get_settings
from docgen.store.blob import get_object
from docgen.store.doc_packages import (
    DocPackageValidationError,
    StaticDocRejected,
    add_static_document_version,
    create_doc_package,
    create_static_document,
    list_static_document_versions,
    normalize_package_items,
)
from docgen.store.models import (
    DocPackage,
    Program,
    StaticDocument,
    StaticDocumentVersion,
    Template,
    TemplateVersion,
    Tenant,
)

log = structlog.get_logger(__name__)


def register(
    router: APIRouter,
    templates,  # Jinja2Templates
    base_ctx,
    forced_tenant_scope,
    enforce_scope,
    redirect,
):
    """Attach all doc-package routes to `router`. Helpers are injected so the
    module has no import cycle with routes.py."""

    # ------------------------------------------------------------------
    # Static Documents — list, create, detail, upload, download.
    # ------------------------------------------------------------------

    @router.get("/static-documents", response_class=HTMLResponse)
    async def static_docs_list(
        request: Request,
        session: AsyncSession = Depends(get_session),
        flash: str | None = None,
        flash_kind: str | None = None,
        page: str | None = None,
    ) -> HTMLResponse:
        from docgen.pagination import PageParams, paginate_query
        scope = forced_tenant_scope(request)
        stmt = (
            select(StaticDocument, Tenant.name)
            .join(Tenant, Tenant.id == StaticDocument.tenant_id)
            .order_by(Tenant.name.asc(), StaticDocument.name.asc())
        )
        if scope is not None:
            stmt = stmt.where(StaticDocument.tenant_id == scope)
        page_result = await paginate_query(
            session, stmt, PageParams.from_request(page), scalars=False,
        )
        docs = [
            {
                "id": d.id,
                "tenant_id": d.tenant_id,
                "tenant_name": tenant_name,
                "name": d.name,
                "description": d.description or "",
                "current_version": d.current_version,
                "updated_at": d.updated_at,
            }
            for (d, tenant_name) in page_result.items
        ]
        tenants_all = list((await session.execute(select(Tenant).order_by(Tenant.name.asc()))).scalars())
        return templates.TemplateResponse(
            request,
            "admin_static_documents.html",
            await base_ctx(
                request, session, active="static-documents",
                docs=docs, tenants_all=tenants_all,
                page=page_result,
                flash=flash, flash_kind=flash_kind,
            ),
        )

    @router.post("/static-documents")
    async def static_docs_create(
        request: Request,
        tenant_id: uuid.UUID = Form(...),
        name: str = Form(...),
        description: str = Form(""),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        enforce_scope(request, tenant_id)
        name_clean = name.strip()
        if not name_clean:
            return redirect("/ui/static-documents", flash="Name is required.", flash_kind="error")
        if await session.get(Tenant, tenant_id) is None:
            raise HTTPException(404, "Tenant not found")
        try:
            doc = await create_static_document(
                session, tenant_id, name_clean, description.strip() or None
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return redirect(
                "/ui/static-documents",
                flash=f"A static document named '{name_clean}' already exists in this tenant.",
                flash_kind="error",
            )
        log.info("static_document_created", tenant_id=str(tenant_id), name=name_clean)
        return redirect(
            f"/ui/static-documents/{doc.id}",
            flash=f"Created '{name_clean}'. Upload a PDF version to make it usable in packages.",
            flash_kind="success",
        )

    @router.get("/static-documents/{doc_id}", response_class=HTMLResponse)
    async def static_doc_detail(
        doc_id: uuid.UUID,
        request: Request,
        session: AsyncSession = Depends(get_session),
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        doc = await session.get(StaticDocument, doc_id)
        if doc is None:
            raise HTTPException(404, "Static document not found")
        enforce_scope(request, doc.tenant_id)
        tenant = await session.get(Tenant, doc.tenant_id)
        versions = await list_static_document_versions(session, doc.id)
        return templates.TemplateResponse(
            request,
            "admin_static_document_detail.html",
            await base_ctx(
                request, session, active="static-documents",
                doc=doc, tenant=tenant, versions=versions,
                flash=flash, flash_kind=flash_kind,
            ),
        )

    @router.post("/static-documents/{doc_id}/versions")
    async def static_doc_upload_version(
        doc_id: uuid.UUID,
        request: Request,
        file: UploadFile = File(...),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        doc = await session.get(StaticDocument, doc_id)
        if doc is None:
            raise HTTPException(404, "Static document not found")
        enforce_scope(request, doc.tenant_id)
        body = await file.read()
        filename = file.filename or "document.pdf"
        # Env-var break-glass admins have no admin_users row and therefore no
        # UUID to record; DB-backed admins do. Falling back to None keeps the
        # break-glass upload path working without lying about who uploaded.
        identity = getattr(request.state, "admin_identity", None)
        uploader_id = getattr(identity, "user_id", None) if identity else None
        try:
            ver = await add_static_document_version(
                session, doc, body, filename,
                uploaded_by=uploader_id,
            )
            await session.commit()
        except StaticDocRejected as e:
            await session.rollback()
            return redirect(
                f"/ui/static-documents/{doc_id}",
                flash=f"Upload rejected: {e.message}",
                flash_kind="error",
            )
        log.info(
            "static_document_version_added",
            doc_id=str(doc_id), version=ver.version, bytes=ver.byte_size,
        )
        return redirect(
            f"/ui/static-documents/{doc_id}",
            flash=f"Uploaded version {ver.version}.",
            flash_kind="success",
        )

    @router.get("/static-documents/{doc_id}/versions/{version}/download")
    async def static_doc_download(
        doc_id: uuid.UUID,
        version: int,
        request: Request,
        session: AsyncSession = Depends(get_session),
    ) -> StreamingResponse:
        doc = await session.get(StaticDocument, doc_id)
        if doc is None:
            raise HTTPException(404, "Static document not found")
        enforce_scope(request, doc.tenant_id)
        stmt = select(StaticDocumentVersion).where(
            StaticDocumentVersion.static_document_id == doc.id,
            StaticDocumentVersion.version == version,
        )
        ver = (await session.execute(stmt)).scalar_one_or_none()
        if ver is None:
            raise HTTPException(404, "Version not found")
        settings = get_settings()
        body = get_object(settings.s3_bucket_documents, ver.blob_key)
        return StreamingResponse(
            io.BytesIO(body),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{doc.name}-v{version}.pdf"',
                "Cache-Control": "private, no-store",
            },
        )

    # ------------------------------------------------------------------
    # Doc Packages — list, create, detail (composer), items ops.
    # ------------------------------------------------------------------

    @router.get("/packages", response_class=HTMLResponse)
    async def packages_list(
        request: Request,
        session: AsyncSession = Depends(get_session),
        flash: str | None = None,
        flash_kind: str | None = None,
        page: str | None = None,
    ) -> HTMLResponse:
        from docgen.pagination import PageParams, paginate_query
        scope = forced_tenant_scope(request)
        stmt = (
            select(DocPackage, Tenant.name)
            .join(Tenant, Tenant.id == DocPackage.tenant_id)
            .order_by(Tenant.name.asc(), DocPackage.name.asc())
        )
        if scope is not None:
            stmt = stmt.where(DocPackage.tenant_id == scope)
        page_result = await paginate_query(
            session, stmt, PageParams.from_request(page), scalars=False,
        )
        packages = [
            {
                "id": p.id,
                "tenant_id": p.tenant_id,
                "tenant_name": tenant_name,
                "name": p.name,
                "description": p.description or "",
                "item_count": len(p.items or []),
                "is_active": p.is_active,
                "updated_at": p.updated_at,
            }
            for (p, tenant_name) in page_result.items
        ]
        tenants_all = list((await session.execute(select(Tenant).order_by(Tenant.name.asc()))).scalars())
        return templates.TemplateResponse(
            request,
            "admin_doc_packages.html",
            await base_ctx(
                request, session, active="packages",
                packages=packages, tenants_all=tenants_all,
                page=page_result,
                flash=flash, flash_kind=flash_kind,
            ),
        )

    @router.post("/packages")
    async def packages_create(
        request: Request,
        tenant_id: uuid.UUID = Form(...),
        name: str = Form(...),
        description: str = Form(""),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        enforce_scope(request, tenant_id)
        name_clean = name.strip()
        if not name_clean:
            return redirect("/ui/packages", flash="Name is required.", flash_kind="error")
        if await session.get(Tenant, tenant_id) is None:
            raise HTTPException(404, "Tenant not found")
        try:
            pkg = await create_doc_package(
                session, tenant_id, name_clean, description.strip() or None, items=[]
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return redirect(
                "/ui/packages",
                flash=f"A package named '{name_clean}' already exists in this tenant.",
                flash_kind="error",
            )
        log.info("doc_package_created", tenant_id=str(tenant_id), name=name_clean)
        return redirect(
            f"/ui/packages/{pkg.id}",
            flash=f"Created '{name_clean}'. Add template and static-document items in order.",
            flash_kind="success",
        )

    @router.get("/packages/{package_id}", response_class=HTMLResponse)
    async def package_detail(
        package_id: uuid.UUID,
        request: Request,
        session: AsyncSession = Depends(get_session),
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        pkg = await session.get(DocPackage, package_id)
        if pkg is None:
            raise HTTPException(404, "Package not found")
        enforce_scope(request, pkg.tenant_id)
        tenant = await session.get(Tenant, pkg.tenant_id)

        items_view = await _resolve_items_for_display(session, pkg)

        # Add-item pickers: tenant's templates and static docs, minus the ones
        # already in the package (still allow duplicates? for now yes — an
        # operator may legitimately want the same appendix twice).
        tmpls = list((await session.execute(
            select(Template)
            .where(Template.tenant_id == pkg.tenant_id)
            .order_by(Template.name.asc())
        )).scalars())
        statics = list((await session.execute(
            select(StaticDocument)
            .where(StaticDocument.tenant_id == pkg.tenant_id)
            .order_by(StaticDocument.name.asc())
        )).scalars())
        return templates.TemplateResponse(
            request,
            "admin_doc_package_detail.html",
            await base_ctx(
                request, session, active="packages",
                package=pkg, tenant=tenant,
                items_view=items_view,
                templates_available=tmpls,
                statics_available=statics,
                flash=flash, flash_kind=flash_kind,
            ),
        )

    @router.post("/packages/{package_id}/items/add")
    async def package_add_item(
        package_id: uuid.UUID,
        request: Request,
        kind: str = Form(...),
        ref_id: uuid.UUID = Form(...),
        label: str = Form(""),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        pkg = await session.get(DocPackage, package_id)
        if pkg is None:
            raise HTTPException(404, "Package not found")
        enforce_scope(request, pkg.tenant_id)
        items = list(pkg.items or [])
        entry: dict[str, Any] = {"kind": kind, "ref_id": str(ref_id)}
        if label.strip():
            entry["label"] = label.strip()
        items.append(entry)
        try:
            pkg.items = normalize_package_items(items)
        except DocPackageValidationError as e:
            return redirect(
                f"/ui/packages/{package_id}",
                flash=f"Invalid item: {e}",
                flash_kind="error",
            )
        await session.commit()
        return redirect(f"/ui/packages/{package_id}", flash="Added.", flash_kind="success")

    @router.post("/packages/{package_id}/items/move")
    async def package_move_item(
        package_id: uuid.UUID,
        request: Request,
        index: int = Form(...),
        direction: str = Form(...),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        pkg = await session.get(DocPackage, package_id)
        if pkg is None:
            raise HTTPException(404, "Package not found")
        enforce_scope(request, pkg.tenant_id)
        items = list(pkg.items or [])
        if not 0 <= index < len(items):
            return redirect(f"/ui/packages/{package_id}", flash="Item not found.", flash_kind="error")
        if direction == "up" and index > 0:
            items[index - 1], items[index] = items[index], items[index - 1]
        elif direction == "down" and index < len(items) - 1:
            items[index], items[index + 1] = items[index + 1], items[index]
        pkg.items = items
        await session.commit()
        return redirect(f"/ui/packages/{package_id}")

    @router.post("/packages/{package_id}/items/remove")
    async def package_remove_item(
        package_id: uuid.UUID,
        request: Request,
        index: int = Form(...),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        pkg = await session.get(DocPackage, package_id)
        if pkg is None:
            raise HTTPException(404, "Package not found")
        enforce_scope(request, pkg.tenant_id)
        items = list(pkg.items or [])
        if 0 <= index < len(items):
            items.pop(index)
            pkg.items = items
            await session.commit()
        return redirect(f"/ui/packages/{package_id}", flash="Removed.", flash_kind="success")

    # ------------------------------------------------------------------
    # Program bindings — outcome -> package_id map, edited on the program
    # detail page. Save endpoint accepts one binding row at a time to keep
    # the surface flat and forgiving; publish-time validation lives in the
    # pipeline, not here.
    # ------------------------------------------------------------------

    @router.post("/programs/{program_id}/bindings")
    async def program_save_binding(
        program_id: uuid.UUID,
        request: Request,
        outcome: str = Form(...),
        package_id: str = Form(""),  # empty = clear the binding
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        prog = await session.get(Program, program_id)
        if prog is None:
            raise HTTPException(404, "Program not found")
        enforce_scope(request, prog.tenant_id)
        outcome_clean = outcome.strip().lower()
        if not outcome_clean:
            return redirect(
                f"/ui/programs/{program_id}",
                flash="Outcome is required.",
                flash_kind="error",
            )
        # Ensure the outcome is declared on the program — no silent creation
        # of new outcome slugs through the bindings screen.
        declared = list(prog.outcomes or [])
        if outcome_clean not in declared:
            return redirect(
                f"/ui/programs/{program_id}",
                flash=(
                    f"Outcome '{outcome_clean}' is not declared on this program. "
                    "Add it to the outcome vocabulary first."
                ),
                flash_kind="error",
            )
        bindings = dict(prog.package_bindings or {})
        pkg_id_str = package_id.strip()
        if not pkg_id_str:
            bindings.pop(outcome_clean, None)
            note = f"Cleared binding for '{outcome_clean}'."
        else:
            try:
                pkg_uuid = uuid.UUID(pkg_id_str)
            except ValueError:
                return redirect(
                    f"/ui/programs/{program_id}",
                    flash="Invalid package id.",
                    flash_kind="error",
                )
            pkg = await session.get(DocPackage, pkg_uuid)
            if pkg is None or pkg.tenant_id != prog.tenant_id:
                return redirect(
                    f"/ui/programs/{program_id}",
                    flash="Package not found in this tenant.",
                    flash_kind="error",
                )
            bindings[outcome_clean] = str(pkg_uuid)
            note = f"Bound '{outcome_clean}' → '{pkg.name}'."
        prog.package_bindings = bindings
        await session.commit()
        return redirect(f"/ui/programs/{program_id}", flash=note, flash_kind="success")

    @router.post("/programs/{program_id}/outcomes")
    async def program_edit_outcomes(
        program_id: uuid.UUID,
        request: Request,
        outcomes: str = Form(...),
        session: AsyncSession = Depends(get_session),
    ) -> RedirectResponse:
        """Set the outcome vocabulary from a comma-separated list. Removes
        stale bindings for any outcome that got dropped so the two columns
        stay consistent."""
        prog = await session.get(Program, program_id)
        if prog is None:
            raise HTTPException(404, "Program not found")
        enforce_scope(request, prog.tenant_id)
        parts = [p.strip().lower() for p in outcomes.replace("\n", ",").split(",")]
        clean = [p for p in parts if p]
        seen: set[str] = set()
        deduped: list[str] = []
        for p in clean:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        prog.outcomes = deduped
        # Drop bindings whose outcome is no longer declared.
        prog.package_bindings = {
            k: v for k, v in (prog.package_bindings or {}).items() if k in seen
        }
        await session.commit()
        return redirect(
            f"/ui/programs/{program_id}",
            flash=f"Outcome vocabulary set to {len(deduped)} entr{'y' if len(deduped) == 1 else 'ies'}.",
            flash_kind="success",
        )


async def _resolve_items_for_display(
    session: AsyncSession, pkg: DocPackage
) -> list[dict[str, Any]]:
    """Hydrate each item's referenced row so the composer can render name +
    current version + a "missing" flag when the referenced row is gone.

    Assembler doesn't care about missing rows at design time — this is
    purely for the operator's screen. The pipeline preflight catches
    unresolvable refs at publish time.
    """
    out: list[dict[str, Any]] = []
    for i, item in enumerate(pkg.items or []):
        kind = item.get("kind")
        ref_id_raw = item.get("ref_id")
        try:
            ref_id = uuid.UUID(str(ref_id_raw))
        except (TypeError, ValueError):
            out.append({
                "index": i, "kind": kind, "ref_id": str(ref_id_raw),
                "label": item.get("label", ""), "name": "?", "missing": True,
                "version_note": "invalid id",
            })
            continue

        if kind == "template":
            tmpl = await session.get(Template, ref_id)
            if tmpl is None or tmpl.tenant_id != pkg.tenant_id:
                out.append({
                    "index": i, "kind": "template", "ref_id": str(ref_id),
                    "label": item.get("label", ""), "name": "(deleted)",
                    "missing": True, "version_note": "template removed",
                })
                continue
            # Latest frozen version for this template — matches assembler
            # runtime selection.
            latest = (await session.execute(
                select(TemplateVersion)
                .where(TemplateVersion.template_id == tmpl.id, TemplateVersion.is_frozen.is_(True))
                .order_by(TemplateVersion.version.desc(), TemplateVersion.subversion.desc())
                .limit(1)
            )).scalar_one_or_none()
            out.append({
                "index": i, "kind": "template", "ref_id": str(ref_id),
                "label": item.get("label", "") or tmpl.name, "name": tmpl.name,
                "missing": False,
                "version_note": (
                    f"v{latest.version}.{latest.subversion}" if latest is not None
                    else "no frozen version"
                ),
                "has_version": latest is not None,
            })
        elif kind == "static":
            doc = await session.get(StaticDocument, ref_id)
            if doc is None or doc.tenant_id != pkg.tenant_id:
                out.append({
                    "index": i, "kind": "static", "ref_id": str(ref_id),
                    "label": item.get("label", ""), "name": "(deleted)",
                    "missing": True, "version_note": "static doc removed",
                })
                continue
            out.append({
                "index": i, "kind": "static", "ref_id": str(ref_id),
                "label": item.get("label", "") or doc.name, "name": doc.name,
                "missing": False,
                "version_note": (
                    f"v{doc.current_version}" if doc.current_version
                    else "no version uploaded"
                ),
                "has_version": bool(doc.current_version),
            })
        else:
            out.append({
                "index": i, "kind": str(kind), "ref_id": str(ref_id_raw),
                "label": item.get("label", ""), "name": "?", "missing": True,
                "version_note": f"unknown kind {kind!r}",
            })
    return out
