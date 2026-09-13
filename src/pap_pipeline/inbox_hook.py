"""Registers the PAP pipeline as a plugin against the inbox agent's handler slot.

Loaded by docgen's plugin loader when `DOCGEN_PLUGINS=pap_pipeline.inbox_hook`
is set at boot. Docgen never imports this module directly (see the boundary
test) — the loader resolves it dynamically, and this module reaches back into
docgen only from the pap_pipeline side of the fence.

Contract:
  - Called after resolve_tenant + tenant_id stamp, before _pick_attachment.
  - Owns its own status/review_state/reply lifecycle; must return a
    ProcessResult so the actor can log the outcome.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from docgen.config import Settings
from docgen.core.render_pipeline import execute_merge
from docgen.inbox.agent import ProcessResult, set_pap_handler, set_pap_reply_sender
from docgen.inbox.imap_poller import InboundMessage
from docgen.inbox.mailer import Attachment, MailerError, send_reply
from docgen.store.blob import get_object, put_object
from docgen.store.models import InboxEvent, Program, Template, TemplateVersion, Tenant

from pap_pipeline.assembler import (
    AssemblerError,
    AssemblerMissingBinding,
    AssemblyResult,
    assemble_reply_package,
)
from pap_pipeline.orchestrator import PipelineConfig, run_with_intake
from pap_pipeline.rules import load_rules
from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.mdm_lookup import MdmSourceConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig

log = structlog.get_logger(__name__)

# Every pipeline outcome parks in pending_review — nothing goes back to the
# applicant until a human hits Approve on the pipeline detail page. A wrong
# auto-send (denial to an approvable case, approval to a fraudulent one)
# cannot be un-sent, so we prefer a queue over an outbox.
_OUTCOME_TO_REVIEW_STATE = {
    "approve": "pending_review",
    "deny": "pending_review",
    "missing_info": "pending_review",
}


_AI_ACTOR = "AI: pap-pipeline"
_SYSTEM_ACTOR = "System: mailer"


def _append_audit(event: InboxEvent, *, action: str, actor: str, note: str) -> None:
    """Append an audit entry to the event's review_history JSONB list.

    JSONB list mutation needs a fresh list assignment to trip the ORM's dirty
    check — .append() on the loaded list is invisible to SQLAlchemy without
    a MutableList wrapper. Kept in-sync with the UI-side helper in routes.py.
    """
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "action": action,
        "actor": actor,
        "note": note,
    }
    event.review_history = [*(event.review_history or []), entry]


def _pipeline_config(
    settings: Settings, tenant: Tenant | None = None
) -> PipelineConfig:
    """Build a PipelineConfig from docgen Settings + the tenant's MDM config.

    The three AI stages share `ai_base_url` + `ai_api_key` because they all
    target Nebius Token Factory. The MDM slot is per-tenant: each tenant
    owns its own connector config on the `Tenant.mdm_config` JSONB column
    (ADR-0004). When the tenant isn't in scope (rare — the JSON-in API path
    resolves it) or hasn't wired a config, we fall back to the simulated
    source so the demo works out-of-the-box.
    """
    base_url = (settings.ai_base_url or "https://api.studio.nebius.com/v1").rstrip("/")
    api_key = settings.ai_api_key or ""
    return PipelineConfig(
        extract=ExtractClientConfig(
            base_url=base_url,
            api_key=api_key,
            vision_model=settings.pdf_vision_ocr_model or "openbmb/MiniCPM-V-4_5",
            timeout=settings.ai_timeout_seconds,
        ),
        retrieve=RetrieveClientConfig(
            base_url=base_url,
            api_key=api_key,
            # Nebius Token Factory doesn't host the NVIDIA embed/rerank NIMs
            # yet, so use their sole embed model and skip rerank. The NVIDIA
            # requirement is met by the reason stage's Nemotron.
            embed_model="Qwen/Qwen3-Embedding-8B",
            rerank_model=None,
            timeout=settings.ai_timeout_seconds,
        ),
        reason=ReasonClientConfig(
            base_url=base_url,
            api_key=api_key,
            model=settings.ai_verdict_model or "nvidia/nemotron-3-super-120b-a12b",
            timeout=settings.ai_timeout_seconds,
        ),
        mdm=_mdm_config_for_tenant(tenant),
    )


# Numeric fields on MdmSourceConfig — coerced from JSON with a safe
# fallback so a stray string in `tenant.mdm_config` can't crash the
# whole pipeline. Missing keys inherit the dataclass default.
_MDM_NUMERIC_KEYS = ("max_window_days", "max_events", "timeout")


def _mdm_config_for_tenant(tenant: Tenant | None) -> MdmSourceConfig:
    """Map ``tenant.mdm_config`` (JSONB) → :class:`MdmSourceConfig`.

    Empty / missing dict → ``provider="simulated"``. Operators opt out
    explicitly with ``{"provider": "null"}`` — silently falling back to
    "null" would hide misconfiguration, which is worse than showing a
    demo scenario the operator didn't ask for.

    ``api_key_env`` holds the *name* of an env var; the actual secret
    never lands in the DB. Missing env var → api_key stays ``None`` and
    the connector fails at lookup time with a clear message rather than
    running with an empty bearer token.
    """
    cfg = dict((tenant.mdm_config if tenant is not None else None) or {})
    provider = str(cfg.get("provider") or "").strip().lower() or "simulated"

    api_key: str | None = None
    env_name = cfg.get("api_key_env")
    if isinstance(env_name, str) and env_name:
        api_key = os.environ.get(env_name) or None
        if api_key is None:
            log.warning(
                "pap_mdm_api_key_env_missing",
                tenant_id=str(tenant.id) if tenant else None,
                env_name=env_name,
            )

    families = cfg.get("program_families") or ()
    if isinstance(families, list):
        families = tuple(str(f) for f in families)
    elif not isinstance(families, tuple):
        families = ()

    numeric: dict[str, Any] = {}
    defaults = {"max_window_days": 1825, "max_events": 200, "timeout": 30.0}
    for key in _MDM_NUMERIC_KEYS:
        raw_val = cfg.get(key)
        if raw_val is None:
            continue
        try:
            numeric[key] = type(defaults[key])(raw_val)
        except (TypeError, ValueError):
            log.warning(
                "pap_mdm_config_bad_numeric",
                tenant_id=str(tenant.id) if tenant else None,
                key=key,
                value=raw_val,
            )

    return MdmSourceConfig(
        provider=provider,
        program_families=families,
        base_url=cfg.get("base_url") or None,
        api_key=api_key,
        **numeric,
    )


def _pap_attachments(msg: InboundMessage) -> list[bytes]:
    """Every attachment goes to the pipeline — a claim often spans a pay stub,
    an insurance card, and a prescription across separate files."""
    return [a.content for a in msg.attachments if a.content]


# InboundAttachment.kind is docx|pdf; the poller filters everything else,
# so this map covers the only two we ever land on. Falls back to the
# generic bytestream MIME so a future third kind still gets served.
_KIND_TO_CONTENT_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _persist_inbound_attachments(
    event: InboxEvent, msg: InboundMessage, settings: Settings
) -> list[dict[str, Any]]:
    """Upload each inbound attachment to the documents bucket and return a
    manifest the UI can render as preview links.

    Blob-store failures don't abort the pipeline — a missing preview is less
    bad than dropping the claim. Each failed upload just gets its blob_key
    left null in the manifest so the UI renders it as "(upload failed)".
    """
    manifest: list[dict[str, Any]] = []
    for idx, a in enumerate(msg.attachments):
        if not a.content:
            continue
        content_type = _KIND_TO_CONTENT_TYPE.get(a.kind, "application/octet-stream")
        safe_name = (a.filename or f"attachment-{idx}").replace("/", "_").replace("\\", "_")
        blob_key = f"inbox-inbound/{event.id}/{idx:02d}-{safe_name}"
        entry: dict[str, Any] = {
            "filename": a.filename or safe_name,
            "content_type": content_type,
            "size": len(a.content),
            "blob_key": None,
        }
        try:
            put_object(
                settings.s3_bucket_documents,
                blob_key,
                a.content,
                content_type=content_type,
            )
            entry["blob_key"] = blob_key
        except Exception as e:
            log.warning(
                "pap_inbound_attachment_persist_failed",
                event_id=str(event.id),
                filename=a.filename,
                error=f"{type(e).__name__}: {e}",
            )
        manifest.append(entry)
    return manifest


def _score_from_decision(decision: Decision) -> float | None:
    """Highest citation score, scaled to 0–100 to match the template flow's
    review-page score column. None when no citation carries a score.

    Only rule-typed citations carry retrieval scores; history-typed
    citations (ADR-0004) don't and are skipped here.
    """
    scores = [
        c.score
        for c in decision.citations
        if c.type == "rule" and c.score is not None
    ]
    if not scores:
        return None
    return round(max(scores) * 100.0, 1)


def _build_reply(decision: Decision) -> tuple[str, str, str]:
    """(subject_suffix, text, html) for the applicant reply.

    Only rule-typed citations are rendered as the "rules considered"
    list. History-typed citations (ADR-0004) belong in the outcome-bound
    template body, not this evidence summary.
    """
    outcome = decision.outcome
    rule_cites = [c for c in decision.citations if c.type == "rule"]
    cite_lines = [
        f"- {c.rule_id}"
        + (f" (score {c.score:.2f})" if c.score is not None else "")
        + (" — matched" if c.matched else " — not matched")
        for c in rule_cites
    ]
    missing_lines = [f"- {m}" for m in decision.missing_fields]

    text_parts = [
        f"Decision: {outcome}",
        "",
        decision.rationale or "(no rationale provided)",
    ]
    if cite_lines:
        text_parts += ["", "Rules considered:", *cite_lines]
    if missing_lines:
        text_parts += ["", "Missing information:", *missing_lines]
    text = "\n".join(text_parts) + "\n"

    html_parts = [
        f"<p><b>Decision:</b> {outcome}</p>",
        f"<p>{decision.rationale or '(no rationale provided)'}</p>",
    ]
    if cite_lines:
        html_parts.append(
            "<p><b>Rules considered:</b></p><ul>"
            + "".join(f"<li>{line[2:]}</li>" for line in cite_lines)
            + "</ul>"
        )
    if missing_lines:
        html_parts.append(
            "<p><b>Missing information:</b></p><ul>"
            + "".join(f"<li>{line[2:]}</li>" for line in missing_lines)
            + "</ul>"
        )
    html = "".join(html_parts)
    return outcome, text, html


def _reply_subject(original: str | None, outcome: str) -> str:
    base = (original or "PAP intake").strip()
    prefix = "" if base.lower().startswith("re:") else "Re: "
    return f"{prefix}{base} — {outcome}"


# Outcome → substring on template name. The Alderwood letter set is
# ASP-201 (approval), ASP-202 (denial), ASP-203 (missing-info) — we key on
# the human-readable middle segment so re-uploads under new versions still
# resolve as long as the naming stays consistent.
_OUTCOME_TO_TEMPLATE_HINT = {
    "approve": "approv",
    "deny": "deni",
    "missing_info": "missing",
}


def _find_reply_template(
    session: Any, tenant: Tenant, outcome: str
) -> TemplateVersion | None:
    """Resolve the tenant's frozen reply template for this outcome.

    Picks the most recent frozen version whose template name contains the
    outcome hint (approv / deni / missing). Returns None if the tenant hasn't
    uploaded that outcome's template yet — the caller then falls back to a
    text-only reply so a single missing template doesn't break the flow.
    """
    hint = _OUTCOME_TO_TEMPLATE_HINT.get(outcome)
    if not hint:
        return None
    row = (
        session.query(TemplateVersion)
        .join(Template, Template.id == TemplateVersion.template_id)
        .filter(
            Template.tenant_id == tenant.id,
            Template.is_active.is_(True),
            Template.name.ilike(f"%{hint}%"),
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(
            TemplateVersion.version.desc(),
            TemplateVersion.subversion.desc(),
            TemplateVersion.created_at.desc(),
        )
        .first()
    )
    return row


def _short_id(event_id: Any) -> str:
    return str(event_id).replace("-", "")[:8].upper()


# Long program codes ("CARDIOVITA-PAP-2026") overflow the header band cell
# on the ASP-20x letters — the cell fits ~83pt, the long form needs ~110pt at
# 9.8pt Helvetica-Bold, so it clips silently at the band edge. Compressing
# common program slugs to their short form keeps the header legible without
# widening the template.
_PROGRAM_CODE_SHORT = {
    "cardiovita-pap-2026": "CV-PAP-2026",
}


def _short_program_code(program: str | None) -> str:
    slug = (program or "cardiovita-pap-2026").lower()
    return _PROGRAM_CODE_SHORT.get(slug, slug.upper())


def _compose_salutation(intake: Intake) -> str:
    """Compose a natural greeting from honorific + family name.

    Prefers "Dear Ms. Chen," when the form supplied a title, falling back
    through last-name-only ("Dear Chen,") and finally the full legal name
    ("Dear Maria L Chen,") only when nothing else is available. The reply
    letter reserves the whole salutation line so a merged full-name string
    reads as an obvious template dump rather than a natural greeting.
    """
    honorific = (intake.honorific or "").strip().rstrip(".")
    family = (intake.family_name or "").strip()
    if honorific and family:
        return f"Dear {honorific}. {family},"
    if family:
        return f"Dear {family},"
    if intake.patient_name:
        return f"Dear {intake.patient_name},"
    return "Dear Applicant,"


def _compose_city_state_zip(intake: Intake) -> str:
    """Assemble a `City, State ZIP` (or non-US equivalent) line from parts.

    Non-US applicants often have no `mailing_state`; the `city` + `country`
    is still worth showing so the recipient block on the denial letter
    matches what the applicant actually wrote, rather than the state-only
    fragment the old mapper produced.
    """
    city = (intake.city or "").strip()
    state = (intake.mailing_state or "").strip()
    postal = (intake.postal_code or "").strip()
    country = (intake.country or intake.country_of_residence or "").strip()

    left = ", ".join(p for p in (city, state) if p)
    parts = [p for p in (left, postal) if p]
    line = " ".join(parts).strip()
    # Only append country when it's non-US — US recipients don't expect
    # "United States" on their own address block.
    if country and country.lower() not in {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}:
        line = f"{line}, {country}" if line else country
    return line


def _extras_only(raw_extraction: dict[str, Any]) -> dict[str, Any]:
    """Fields in the raw extraction that aren't part of Intake.

    Used by the reply mapper so that when a program defines a custom slot
    like `member_id` and the tenant's letter template has a matching field
    name, the value flows through end-to-end without needing an Intake
    schema change.
    """
    known = set(Intake.model_fields.keys())
    return {k: v for k, v in (raw_extraction or {}).items() if k not in known}


def _build_render_data(
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map Intake + Decision onto the ASP-20x template field schema.

    Common fields (`alderwood_*`) appear on all three templates. Outcome
    branches (`application_has_*`, `we_not_*`, `we_need_*`) add the fields
    the specific letter uses. Missing template fields are simply left as
    empty strings — the renderer treats them as blanks rather than failing.
    """
    today = date.today()
    patient = intake.patient_name or "Applicant"
    salutation = _compose_salutation(intake)
    dob = intake.date_of_birth.strftime("%m/%d/%Y") if intake.date_of_birth else ""
    street = (intake.address_line1 or "").strip()
    apt = (intake.address_line2 or "").strip()
    city_state_zip = _compose_city_state_zip(intake)
    program_code = _short_program_code(program)
    # Prefer the referral ID printed on the paper form so the reply
    # reconciles back to the applicant's file. Fall back to a pipeline-
    # invented `ALD-{short-uuid}` only when the form didn't carry one.
    referral = (intake.referral_id or "").strip() or f"ALD-{_short_id(event.id)}"
    coordinator = "Alderwood PAP Care Coordinator"
    direct_line = "1-877-555-0100"
    coord_title = "Patient Assistance Care Coordinator"
    med = intake.prescribed_drug or ""

    # Program-specific slots seed the render dict first — a template field
    # named `member_id` picks up the extracted value here even though it's
    # not part of Intake. Alderwood mappings below overwrite these where
    # slot names collide with the canonical `alderwood_*` schema.
    common: dict[str, Any] = {k: v for k, v in (extras or {}).items() if v is not None}
    common.update({
        "alderwood_addressee": patient,
        "alderwood_patient": patient,
        "alderwood_dob": dob,
        "alderwood_letter_date": today.strftime("%m/%d/%Y"),
        "alderwood_street_address": street,
        "alderwood_apt_unit": apt,
        "alderwood_city_state_zip": city_state_zip,
        "alderwood_program_code": program_code,
        "alderwood_referral_id": referral,
    })

    if decision.outcome == "approve":
        approval_end = today.replace(year=today.year + 1)
        common.update({
            "application_has_salutation": salutation,
            "application_has_med": med,
            "application_has_rx": med,
            "application_has_strength_dosage_form": "",
            "application_has_approval_period": (
                f"{today.strftime('%m/%d/%Y')} – {approval_end.strftime('%m/%d/%Y')}"
            ),
            "application_has_refills_auth": "",
            "application_has_quantity_per_fill": "",
            "application_has_ship": "Patient home",
            "application_has_pap_member_id": referral,
            "application_has_coordinator": coordinator,
            "application_has_title": coord_title,
            "application_has_direct_line": direct_line,
        })
    elif decision.outcome == "deny":
        recon = today + timedelta(days=30)
        common.update({
            "we_not_salutation": salutation,
            "we_not_med_requested": med,
            "we_not_rx": med,
            "we_not_application_received": today.strftime("%m/%d/%Y"),
            "we_not_determination_date": today.strftime("%m/%d/%Y"),
            "we_not_outcome": "denied",
            "we_not_reason_decision": decision.rationale or "See enclosed rule citations.",
            "we_not_reconsideration_deadline": recon.strftime("%m/%d/%Y"),
            "we_not_coordinator": coordinator,
            "we_not_title": coord_title,
            "we_not_direct_line": direct_line,
        })
    elif decision.outcome == "missing_info":
        respond_by = today + timedelta(days=14)
        items = "\n".join(f"• {m}" for m in decision.missing_fields) or (
            decision.rationale or "Please review the enclosed request."
        )
        common.update({
            "we_need_salutation": salutation,
            "we_need_med_requested": med,
            "we_need_rx": med,
            "we_need_application_received": today.strftime("%m/%d/%Y"),
            "we_need_items_still_needed": items,
            "we_need_respond": respond_by.strftime("%m/%d/%Y"),
            "we_need_coordinator": coordinator,
            "we_need_title": coord_title,
            "we_need_direct_line": direct_line,
        })

    return common


def _render_reply_attachment(
    session: Any,
    tenant: Tenant,
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    settings: Settings,
    *,
    extras: dict[str, Any] | None = None,
) -> tuple[Attachment | None, str | None]:
    """Render the outcome-matched Alderwood letter to PDF bytes.

    Returns `(attachment, blob_key)`. Either half can be None independently:

    - `(None, None)` — template missing / render failed. Caller falls back to
      a text-only reply so a single missing template doesn't drop the flow.
    - `(Attachment, None)` — render succeeded, blob upload failed. Caller
      still sends the PDF over SMTP; the reviewer just can't re-download it
      from the pipeline detail page. All failure modes log for the operator.

    The caller assigns `blob_key` onto `event.reply_attachment_blob_key`
    explicitly — the sequencing is visible at the call site rather than
    hidden behind a side effect on this function.
    """
    tv = _find_reply_template(session, tenant, decision.outcome)
    if tv is None:
        log.warning("pap_reply_template_missing", outcome=decision.outcome, tenant=str(tenant.id))
        return None, None
    data = _build_render_data(intake, decision, program, event, extras=extras)
    try:
        pdf_bytes = execute_merge(tv, data, output_format="pdf", settings=settings)
    except Exception as e:
        log.warning(
            "pap_reply_render_failed",
            template_version=str(tv.id),
            outcome=decision.outcome,
            error=f"{type(e).__name__}: {e}",
        )
        return None, None
    filename = f"alderwood_{decision.outcome}_{_short_id(event.id)}.pdf"
    attachment = Attachment(
        filename=filename, content=pdf_bytes, content_type="application/pdf"
    )
    # Persist the exact bytes we're about to send so the reviewer can pull
    # the same PDF from the pipeline detail page. Blob-store failures
    # shouldn't drop the email attachment — log and return a None key so the
    # caller skips the DB write.
    blob_key = f"inbox-replies/{event.id}/{filename}"
    try:
        put_object(
            settings.s3_bucket_documents,
            blob_key,
            pdf_bytes,
            content_type="application/pdf",
        )
    except Exception as e:
        log.warning(
            "pap_reply_blob_persist_failed",
            event_id=str(event.id),
            error=f"{type(e).__name__}: {e}",
        )
        return attachment, None
    return attachment, blob_key


def _lookup_program_row(
    session: Any, tenant: Tenant, program: str | None
) -> Program | None:
    """Fetch the Program row for this tenant + slug, if any."""
    slug = (program or "").strip().lower()
    if not slug:
        return None
    return (
        session.query(Program)
        .filter(
            Program.tenant_id == tenant.id,
            Program.slug == slug,
            Program.is_active.is_(True),
        )
        .first()
    )


def _draft_reply_artifact(
    session: Any,
    tenant: Tenant,
    intake: Intake,
    decision: Decision,
    program: str | None,
    event: InboxEvent,
    settings: Settings,
    *,
    extras: dict[str, Any] | None,
) -> str | None:
    """Return the persisted blob key for the reply PDF, or None on failure.

    Dispatches between two rendering modes based on program configuration:

      1. Program has a package bound to this outcome → run the assembler.
         The merged package (approval letter + guides + forms) becomes the
         reply attachment; a `doc_package_assemblies` row records the exact
         versions merged for audit + downstream retrieval.
      2. No binding, or binding lookup fails → fall back to the legacy
         single-template flow (`_render_reply_attachment`). Kept so tenants
         that haven't declared outcomes/bindings yet still get a reply
         during the migration window.

    Any assembler failure other than "no binding" is logged and falls through
    to the legacy flow — losing the enrichment (static guides + forms) is
    preferable to blocking the reply entirely. Bindings are configuration;
    a config error shouldn't stall an approved case.
    """
    prog = _lookup_program_row(session, tenant, program)
    if prog is not None and (prog.package_bindings or {}).get(decision.outcome):
        try:
            data = _build_render_data(intake, decision, program, event, extras=extras)
            result: AssemblyResult = assemble_reply_package(
                session,
                tenant=tenant,
                program=prog,
                decision=decision,
                event=event,
                render_data=data,
                settings=settings,
            )
        except AssemblerMissingBinding as e:
            # Shouldn't happen — we just checked the binding — but keep the
            # branch tidy for concurrent binding edits.
            log.warning(
                "pap_assembler_missing_binding",
                event_id=str(event.id),
                outcome=decision.outcome,
                error=str(e),
            )
        except AssemblerError as e:
            # Item missing / render failed. Log with enough detail for the
            # operator to fix the package, then fall through to legacy.
            log.warning(
                "pap_assembler_failed",
                event_id=str(event.id),
                outcome=decision.outcome,
                error=f"{type(e).__name__}: {e}",
            )
        else:
            return result.blob_key

    _, blob_key = _render_reply_attachment(
        session, tenant, intake, decision, program, event, settings,
        extras=extras,
    )
    return blob_key


def _resolve_program_slots(
    session: Any, tenant: Tenant, program: str | None
) -> list[dict[str, Any]] | None:
    """Look up the Program row's extraction_schema for this tenant + slug.

    Returns None when the program isn't registered (in which case the
    extract stage falls back to its `_DEFAULT_SLOTS`, keeping legacy
    tenants working during the Program cutover) or when the row exists
    but hasn't had its schema populated yet.
    """
    slug = (program or "").strip().lower()
    if not slug:
        return None
    row = (
        session.query(Program)
        .filter(
            Program.tenant_id == tenant.id,
            Program.slug == slug,
            Program.is_active.is_(True),
        )
        .first()
    )
    if row is None or not row.extraction_schema:
        return None
    return list(row.extraction_schema)


def _run_pipeline(
    attachments: list[bytes],
    program: str | None,
    slots: list[dict[str, Any]] | None,
    rules: list[Rule] | None,
    settings: Settings,
    tenant: Tenant | None = None,
) -> tuple[dict[str, Any], Intake, Decision]:
    """Sync wrapper — the inbox actor is sync (asyncio.run per call is fine
    here; each call gets its own loop and NullPool means no cross-loop DB
    reuse). Returns the raw extraction dict, the Intake coerced from it, and
    the Decision — the raw dict is what the UI and reply mapper read to see
    program-specific slots that Intake doesn't carry.

    `rules` is the tenant/program-scoped corpus resolved from the DB (with
    a YAML fallback baked in). Passing it here rather than letting the
    retrieve stage load its own copy keeps the docgen.store boundary on
    the calling side — pap_pipeline stages never touch the DB directly.

    `tenant` flows into the pipeline config so the MDM adapter is picked
    from the tenant's `mdm_config` JSONB column (ADR-0004). The inbox
    handler always has a tenant; the JSON-in API path also resolves one
    before calling here.
    """
    import asyncio

    config = _pipeline_config(settings, tenant)
    return asyncio.run(
        run_with_intake(
            attachments,
            config=config,
            program=program,
            tenant_id=str(tenant.id) if tenant else "",
            slots=slots,
            rules=rules,
        )
    )


def _handler(
    session: Any,
    *,
    event: InboxEvent,
    msg: InboundMessage,
    tenant: Tenant,
    program: str | None,
    settings: Settings,
) -> ProcessResult:
    """PAP handler entry point. Signature is fixed by docgen.inbox.agent."""
    attachments = _pap_attachments(msg)
    if not attachments:
        event.status = "skipped"
        event.error = "pap: no attachments to process"
        _append_audit(
            event,
            action="pipeline_skipped",
            actor=_AI_ACTOR,
            note="No attachments on the inbound message — nothing to process.",
        )
        session.commit()
        log.info("pap_no_attachments", event_id=str(event.id))
        return ProcessResult(event.id, "skipped", event.error)

    event.status = "processing"
    event.attachment_name = msg.attachments[0].filename if msg.attachments else None
    # Persist raw inbound files up-front so the reviewer can preview them
    # even if the pipeline itself blows up mid-run.
    event.inbound_attachments = _persist_inbound_attachments(event, msg, settings)
    slots = _resolve_program_slots(session, tenant, program)
    # Resolve the rule corpus on this side of the boundary so pap_pipeline
    # stages stay ignorant of docgen.store. load_rules resolves the tenant's
    # library rules linked to this program via `program_rule_links`, and
    # falls through to the YAML pack when no Program row matches.
    rules = (
        load_rules(program, session=session, tenant_id=tenant.id)
        if program
        else None
    )
    schema_source = (
        f"program schema ({len(slots)} slots)" if slots else "built-in default schema"
    )
    rules_source = (
        f"program rules ({len(rules)} active)" if rules else "YAML fallback"
    )
    _append_audit(
        event,
        action="pipeline_started",
        actor=_AI_ACTOR,
        note=(
            f"Received {len(attachments)} attachment(s) from {event.from_addr}. "
            f"Program: {program or 'default'}. Extraction: {schema_source}. "
            f"Rules: {rules_source}."
        ),
    )
    session.commit()

    try:
        raw_extraction, intake, decision = _run_pipeline(
            attachments,
            program=program,
            slots=slots,
            rules=rules,
            settings=settings,
            tenant=tenant,
        )
    except Exception as e:
        event.status = "failed"
        event.error = f"pap_pipeline_failed: {type(e).__name__}: {e}"
        _append_audit(
            event,
            action="pipeline_failed",
            actor=_AI_ACTOR,
            note=f"{type(e).__name__}: {e}",
        )
        session.commit()
        log.exception("pap_pipeline_failed", event_id=str(event.id))
        return ProcessResult(event.id, "failed", event.error)

    event.score = _score_from_decision(decision)
    event.review_state = _OUTCOME_TO_REVIEW_STATE.get(decision.outcome, "pending_review")
    event.status = "succeeded"
    event.pap_intake = intake.model_dump(mode="json")
    # Persist the full extraction dict so the pipeline detail page and reply
    # mapper can see program-specific slots that don't live on Intake.
    event.pap_extraction = raw_extraction
    event.pap_decision = decision.model_dump(mode="json")
    _append_audit(
        event,
        action="verdict_rendered",
        actor=_AI_ACTOR,
        note=(
            f"Outcome: {decision.outcome}. "
            f"Citations: {len(decision.citations)}, "
            f"missing fields: {len(decision.missing_fields)}. "
            f"Queued for human review."
        ),
    )
    session.commit()

    # Draft the reply artifact now so the reviewer previews the exact bytes
    # that will be sent. Two paths — package assembler wins when the program
    # has an outcome→package binding, else we fall back to the single-template
    # legacy flow so tenants that haven't migrated yet still get a reply.
    blob_key = _draft_reply_artifact(
        session, tenant, intake, decision, program, event, settings,
        extras=_extras_only(raw_extraction),
    )
    if blob_key:
        event.reply_attachment_blob_key = blob_key
        _append_audit(
            event,
            action="reply_drafted",
            actor=_AI_ACTOR,
            note=f"Drafted {decision.outcome} reply and stored for reviewer preview.",
        )
        session.commit()
    else:
        _append_audit(
            event,
            action="reply_draft_skipped",
            actor=_AI_ACTOR,
            note="No outcome-matched template or package found — reply will be text-only when sent.",
        )
        session.commit()

    log.info(
        "pap_decision",
        event_id=str(event.id),
        outcome=decision.outcome,
        citations=len(decision.citations),
        missing=len(decision.missing_fields),
    )
    return ProcessResult(event.id, "succeeded", decision.outcome)


def _send_pap_reply_for_event(
    session: Any, event: InboxEvent, settings: Settings
) -> str | None:
    """Send the queued reply after a human approves the pipeline verdict.

    Reconstructs the reply from fields persisted on the event (decision,
    from/cc/subject, previously rendered PDF blob). Called by the UI's
    /pipeline/approve endpoint via the docgen reply-sender slot.

    Raises MailerError on SMTP failure; the caller renders that back to
    the reviewer so they know the send didn't happen.
    """
    if event.pap_decision is None:
        raise MailerError("event has no pipeline decision to reply about")
    decision = Decision.model_validate(event.pap_decision)

    attachment: Attachment | None = None
    if event.reply_attachment_blob_key:
        try:
            body = get_object(
                settings.s3_bucket_documents, event.reply_attachment_blob_key
            )
        except Exception as e:
            # Log and continue with a text-only reply. Losing the PDF is
            # bad but not as bad as blocking the approved send entirely.
            log.warning(
                "pap_reply_attachment_fetch_failed",
                event_id=str(event.id),
                blob_key=event.reply_attachment_blob_key,
                error=f"{type(e).__name__}: {e}",
            )
        else:
            filename = event.reply_attachment_blob_key.rsplit("/", 1)[-1]
            attachment = Attachment(
                filename=filename, content=body, content_type="application/pdf"
            )

    if not settings.smtp_host:
        raise MailerError("SMTP not configured (DOCGEN_SMTP_HOST is empty)")
    outcome, text, html = _build_reply(decision)
    reply_msg_id = send_reply(
        settings=settings,
        to_addrs=[event.from_addr],
        cc_addrs=list(event.cc_addrs or []),
        subject=_reply_subject(event.subject, outcome),
        body_text=text,
        body_html=html,
        in_reply_to=event.message_id,
        references=[event.message_id],
        attachments=[attachment] if attachment else None,
    )
    _append_audit(
        event,
        action="reply_sent",
        actor=_SYSTEM_ACTOR,
        note=(
            f"Sent {outcome} reply to {event.from_addr}"
            + (f" with attachment {attachment.filename}" if attachment else " (text-only)")
            + "."
        ),
    )
    # Caller commits — this helper is invoked inside the approve endpoint's
    # sync worker which owns the transaction boundary.
    return reply_msg_id


# Register at import time — docgen.plugins.load_plugins() imports this module
# when DOCGEN_PLUGINS names it, and the side effect wires both slots.
set_pap_handler(_handler)
set_pap_reply_sender(_send_pap_reply_for_event)


__all__ = ["_handler", "_send_pap_reply_for_event"]
