"""Member history slice retrieved from an MDM source.

Snapshotted at case-open time and content-addressed so the ledger replays
deterministically even if MDM changes. Every input to the verdict is hashed
(rules pack, extraction, intake) — history follows the same rule.

See ADR-0004. The reason stage never sees the raw `events` list — only the
derived `slots`. Slot-mediation is the policy invariant that keeps the
rules pack, not the LLM, deciding how history matters.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "enrollment", "determination", "appeal", "termination", "renewal"
]
EventOutcome = Literal["approve", "deny", "withdrawn", "pending"]


class HistoryEvent(BaseModel):
    """One prior event pulled from MDM.

    `record_hash` covers the full source record — the slice only carries the
    fields the pipeline is allowed to see, but the hash pins the underlying
    truth so an audit can re-verify against the source system.
    """

    event_id: str
    event_type: EventType
    program_id: str
    program_family_id: str
    date: date
    outcome: EventOutcome | None = None
    reason_code: str | None = None
    source_system: str
    record_hash: str


class HistorySlot(BaseModel):
    """A named boolean / enum derived from HistoryEvents by the slot compiler.

    Slots are the *only* history-derived value the reason stage sees. Rules
    reference slot names in their `when` clauses; the LLM evaluates a rule
    against a boolean the same way it evaluates any intake field.

    `from_events` records which event ids produced the value — the audit
    needs this to answer *why* a slot was true.
    """

    name: str
    value: bool | str | int | None
    from_events: list[str] = Field(default_factory=list)


class HistorySlice(BaseModel):
    """Bounded, tenant-scoped, snapshotted view of member history.

    Bounded by the connector's scope (program_families, window, max_events).
    Never carries an entire member record — only what the connector was
    configured to expose. `snapshot_hash` is SHA-256 over the canonicalized
    slice payload (see :func:`canonical_snapshot_hash`).
    """

    member_id: str
    program_family_id: str
    events: list[HistoryEvent] = Field(default_factory=list)
    slots: list[HistorySlot] = Field(default_factory=list)
    snapshot_hash: str
    snapshot_at: datetime
    source_provider: str


def canonical_snapshot_hash(
    *,
    member_id: str,
    program_family_id: str,
    events: list[dict[str, Any]],
    slots: list[dict[str, Any]],
) -> str:
    """SHA-256 over a canonicalized slice payload.

    Same-content → same-hash across providers. Sorted keys, tight separators,
    dates rendered as ISO-8601 strings. Adapters call this on their projected
    slice so the ledger's `mdm_snapshot_hash` is provider-agnostic.
    """
    payload = {
        "member_id": member_id,
        "program_family_id": program_family_id,
        "events": events,
        "slots": slots,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "EventOutcome",
    "EventType",
    "HistoryEvent",
    "HistorySlice",
    "HistorySlot",
    "canonical_snapshot_hash",
]
