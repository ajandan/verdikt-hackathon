"""Assemble the canonical downstream verdict envelope from a persisted event.

The JSON verdict is the *second* artifact the pipeline emits — the first is
the deterministic reply letter, the second is this structured object.
Downstream systems (call center ops, CRM, case-management) consume it to
keep the case moving after the letter is sent.

Design note: the envelope is built on-demand from the InboxEvent row rather
than persisted as its own column. `pap_intake`, `pap_extraction`,
`pap_decision`, and `review_history` are already source-of-truth; assembling
the verdict at read time keeps the audit data path and the operational data
path pointing at the same object — no reconciliation drift.
"""
from __future__ import annotations

from typing import Any

from docgen.store.models import DocPackageAssembly, InboxEvent, Tenant

VERDICT_SCHEMA = "verdikt.decision.v1"


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def _score_from_decision(decision: dict[str, Any] | None) -> float | None:
    """Highest citation score scaled to 0–100. Mirrors the inbox handler's
    `_score_from_decision`, kept local so this module doesn't reach back
    into the plugin layer.

    Only rule-typed citations carry retrieval scores (ADR-0004). Legacy
    events pre-migration have no `type` key on their citation dicts; those
    are treated as rule citations for backward compat.
    """
    if not decision:
        return None
    scores = [
        c.get("score")
        for c in (decision.get("citations") or [])
        if c.get("type", "rule") == "rule" and c.get("score") is not None
    ]
    if not scores:
        return None
    return round(max(scores) * 100.0, 1)


def _decided_at(event: InboxEvent) -> str | None:
    """The timestamp of the `verdict_rendered` audit entry, when present.

    Falls back to `updated_at` for pre-audit-trail events so the verdict
    still carries a decision time (better than null for downstream systems
    that key on it).
    """
    for entry in reversed(list(event.review_history or [])):
        if entry.get("action") == "verdict_rendered":
            return entry.get("at")
    return _iso(event.updated_at)


def _artifacts(event: InboxEvent, *, assembly: DocPackageAssembly | None) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "kind": "reply_letter_pdf",
        "blob_key": event.reply_attachment_blob_key,
        # Admin-session-gated preview endpoint; downstream systems that
        # need long-lived access can add a signed-URL variant later.
        "preview_url": (
            f"/ui/inbox/{event.id}/reply-attachment"
            if event.reply_attachment_blob_key
            else None
        ),
        "sent_message_id": event.reply_message_id,
    }
    inbound = [
        {
            "filename": a.get("filename"),
            "content_type": a.get("content_type"),
            "size": a.get("size"),
            "blob_key": a.get("blob_key"),
        }
        for a in (event.inbound_attachments or [])
    ]
    out: dict[str, Any] = {"reply_letter": reply, "inbound": inbound}
    if assembly is not None:
        # Package artifact: exposes the merged bytes + a per-item ledger so
        # downstream systems (CRM, call center ops) can enumerate exactly
        # which templates and static docs the applicant received.
        out["package"] = {
            "assembly_id": str(assembly.id),
            "doc_package_id": str(assembly.doc_package_id),
            "outcome": assembly.outcome,
            "package_url": (
                f"/ui/inbox/{event.id}/reply-attachment"
                if event.reply_attachment_blob_key
                else None
            ),
            "blob_key": assembly.merged_blob_key,
            "page_count": assembly.merged_page_count,
            "byte_size": assembly.merged_byte_size,
            "items": list(assembly.resolved_items or []),
            "assembled_at": _iso(assembly.assembled_at),
        }
    return out


def _rule_ids(decision: dict[str, Any] | None) -> list[str]:
    """Rule ids from the decision's citations, in original order.

    Filters to rule-typed citations (ADR-0004). Missing `type` is treated
    as `"rule"` so legacy events keep resolving.
    """
    if not decision:
        return []
    return [
        c.get("rule_id")
        for c in (decision.get("citations") or [])
        if c.get("type", "rule") == "rule" and c.get("rule_id")
    ]


def build_verdict(
    event: InboxEvent,
    *,
    tenant: Tenant | None,
    program: str | None = None,
    assembly: DocPackageAssembly | None = None,
) -> dict[str, Any]:
    """Return the downstream-consumable verdict envelope for one event.

    Callers: (1) the pipeline detail page, to render the JSON alongside
    the reply letter, and (2) the `/verdict.json` download endpoint that
    downstream systems fetch.
    """
    decision = event.pap_decision or None
    intake = event.pap_intake or {}
    extraction = event.pap_extraction or {}
    # Program-specific slots aren't part of Intake; downstream systems
    # often key on them (e.g., `member_id`), so surface them explicitly.
    extras = {k: v for k, v in extraction.items() if k not in intake}

    return {
        "schema": VERDICT_SCHEMA,
        "case": {
            "id": event.case_id,
            "event_id": str(event.id),
            "tenant_id": str(event.tenant_id) if event.tenant_id else None,
            "tenant_name": tenant.name if tenant else None,
            "program": program,
            "received_at": _iso(event.received_at),
            "decided_at": _decided_at(event),
        },
        "decision": {
            "outcome": (decision or {}).get("outcome"),
            "rationale": (decision or {}).get("rationale", ""),
            "citations": (decision or {}).get("citations", []),
            "missing_fields": (decision or {}).get("missing_fields", []),
            "score": _score_from_decision(decision),
            "rules_cited": _rule_ids(decision),
        },
        "intake": intake,
        "extras": extras,
        "artifacts": _artifacts(event, assembly=assembly),
        "review": {
            "state": event.review_state,
            "reply_sent": event.reply_message_id is not None,
        },
    }


__all__ = ["VERDICT_SCHEMA", "build_verdict"]
