"""Review-history audit helpers for the PAP inbox pipeline.

Extracted from :mod:`pap_pipeline.inbox_hook`. Owns everything that
lands entries on ``InboxEvent.review_history`` — the JSONB list the UI
renders as the case timeline (audit tab + pipeline detail page).

The write pattern is important: JSONB list mutation needs a **fresh
list assignment** to trip SQLAlchemy's dirty check. ``.append()`` on
the loaded list is invisible to the ORM without a MutableList wrapper,
so :func:`append_audit` reassigns the whole list. Kept in-sync with
the UI-side helper in ``docgen.ui.routes``.

Stage-audit rendering (:func:`append_stage_audit`) folds the per-stage
trace details produced by :func:`pap_pipeline.orchestrator._traced`
into one review-history entry per stage — so the audit doubles as an
at-a-glance pipeline scorecard without opening the pipeline detail tab.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from docgen.store.models import InboxEvent

# Actor identity written to review_history entries. Kept as constants so
# every emitter uses the same string — the UI groups timeline rows by
# actor and a typo would fork the AI/System badges in the review view.
AI_ACTOR = "AI: pap-pipeline"
SYSTEM_ACTOR = "System: mailer"


# Human-facing stage labels for the review-history "action" column. Kept
# in-sync with the trace stage identifiers emitted by
# :func:`pap_pipeline.orchestrator._traced` — one label per stage so the
# audit timeline reads like a story (extract → triage → retrieve → …)
# instead of a pile of raw function names.
_STAGE_LABELS = {
    "extract": "Extract — structured intake",
    "triage": "Triage — route classification",
    "retrieve": "Retrieve — rule shortlist",
    "mdm_lookup": "MDM lookup — prior enrollment history",
    "reason": "Reason — eligibility verdict",
}


def append_audit(
    event: InboxEvent,
    *,
    action: str,
    actor: str,
    note: str,
    at: str | None = None,
) -> None:
    """Append an audit entry to the event's review_history JSONB list.

    JSONB list mutation needs a fresh list assignment to trip the ORM's dirty
    check — .append() on the loaded list is invisible to SQLAlchemy without
    a MutableList wrapper. Kept in-sync with the UI-side helper in routes.py.

    ``at`` overrides the recorded timestamp — used by the per-stage
    tracer to backfill the actual stage completion time instead of
    the "when we got around to logging it" wall-clock.
    """
    entry = {
        "at": at or datetime.now(UTC).isoformat(),
        "action": action,
        "actor": actor,
        "note": note,
    }
    event.review_history = [*(event.review_history or []), entry]


def _fmt_stage_details(stage: str, details: dict[str, Any]) -> str:
    """Format a stage's ``details`` dict as a human-readable sentence.

    One rendering per stage keyed to the extras_fn payload shape in
    orchestrator.run_with_intake / run_triaged_from_intake /
    run_from_intake. Missing keys are silently skipped so a stage that
    grows a new field over time can be added to the summarizer without
    breaking the audit renderer.
    """
    if not details:
        return ""
    if stage == "extract":
        total = details.get("slots_total")
        filled = details.get("slots_filled")
        missing = details.get("slots_missing")
        att = details.get("attachments")
        parts: list[str] = []
        if total is not None:
            parts.append(
                f"{filled}/{total} slots filled ({missing} missing)"
                if filled is not None and missing is not None
                else f"{total} slots"
            )
        if att is not None:
            parts.append(f"{att} attachment{'' if att == 1 else 's'}")
        if details.get("verifier_findings"):
            parts.append(f"{details['verifier_findings']} verifier finding(s)")
        return "; ".join(parts)
    if stage == "triage":
        case_type = details.get("case_type") or "unknown"
        route = details.get("route") or "—"
        needed = details.get("decision_needed")
        conf = details.get("confidence")
        parts = [f"case_type={case_type}", f"route={route}"]
        if needed is not None:
            parts.append("decision_needed" if needed else "skip_decide")
        if conf is not None:
            try:
                parts.append(f"confidence={float(conf):.2f}")
            except (TypeError, ValueError):
                pass
        return "; ".join(parts)
    if stage == "retrieve":
        returned = details.get("rules_returned")
        considered = details.get("rules_considered")
        k = details.get("k_requested")
        if considered is not None:
            return f"{returned}/{considered} rules kept (top-k={k})"
        return f"{returned} rules returned (top-k={k})"
    if stage == "mdm_lookup":
        provider = details.get("provider") or "—"
        events = details.get("events")
        slots_ = details.get("slots")
        return f"provider={provider}; {events} event(s), {slots_} history slot(s)"
    if stage == "reason":
        outcome = details.get("outcome") or "—"
        matched = details.get("citations_matched")
        total = details.get("citations_total")
        missing = details.get("missing_fields")
        parts = [f"outcome={outcome}"]
        if total is not None:
            parts.append(f"{matched}/{total} rules matched")
        if missing:
            parts.append(f"{missing} missing field(s)")
        return "; ".join(parts)
    if "summary_error" in details:
        return f"(summary failed: {details['summary_error']})"
    # Unknown stage — fall back to k=v dump so nothing is silently lost.
    return "; ".join(f"{k}={v}" for k, v in details.items() if v is not None)


def append_stage_audit(event: InboxEvent, trace: list[dict[str, Any]]) -> None:
    """Emit one review_history entry per pipeline-stage trace record.

    Preserves stage completion order (the trace list is appended in that
    order by ``_traced``). ``actor`` carries the model id so the audit
    page's AI badge shows *which* model handled the step. ``at`` is the
    stage's own ``ended_at`` — not now() — so the timeline reflects when
    the work actually happened, in case the audit flush is delayed.

    Notes fold in the stage-specific ``details`` payload (extract slot
    counts, triage route, reason outcome, …) so the audit doubles as an
    at-a-glance pipeline scorecard without needing the pipeline detail
    tab.
    """
    for t in trace:
        stage = str(t.get("stage") or "")
        action = _STAGE_LABELS.get(stage, stage or "(pipeline stage)")
        model = t.get("model") or "n/a"
        ms = t.get("duration_ms")
        note_parts: list[str] = []
        summary = _fmt_stage_details(stage, t.get("details") or {})
        if summary:
            note_parts.append(summary + ".")
        if ms is not None:
            note_parts.append(f"Ran in {ms} ms.")
        if t.get("status") == "failed":
            err = t.get("error") or "unknown error"
            note_parts.append(f"Failed: {err}")
        append_audit(
            event,
            action=action,
            actor=f"AI: {stage} · {model}",
            note=" ".join(note_parts) or "(no timing recorded)",
            at=t.get("ended_at"),
        )


__all__ = [
    "AI_ACTOR",
    "SYSTEM_ACTOR",
    "append_audit",
    "append_stage_audit",
]
