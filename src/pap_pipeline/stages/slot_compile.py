"""Slot compiler — pure function of ``(events, slot_defs) -> [HistorySlot]``.

The slot-mediation invariant in ADR-0004 says the reason stage never sees
raw ``HistoryEvent`` rows — only the derived slots that a rule referenced.
The compiler is the single place that projection lives, extracted out of
:mod:`pap_pipeline.stages.mdm_lookup` so:

- Connectors stay thin. An adapter's job is "pull the slice"; deriving
  named booleans / enums / counts from that slice is a policy step that
  belongs on our side of the boundary, not the vendor's.
- Slot logic is independently testable. Same event list + same slot defs
  → same slots. No network, no clock, no async. That's the shape callers
  need to trust replayability.
- The program pack owns the slot definitions. Programs declare their
  slots in ``programs.history_slots`` (migration 0033). Rules reference
  those slot names via :attr:`Rule.references_slots`. The publish gate
  refuses to activate a program whose linked rules reference undeclared
  slots — so the compiler never has to invent a slot definition on the
  fly.

Kinds supported today (each maps to one clause the rules pack can lean on):

- ``bool``               — true iff at least one matching event exists.
- ``latest_outcome``     — outcome of the newest matching event, or None.
- ``days_since_latest``  — integer days from the newest matching event
                           to ``as_of`` (defaults to today), or None.
- ``count``              — number of matching events.

The ``where`` filter is intentionally minimal — three fields, all optional
— because slots are *derived facts a rule can cite*, not a query DSL.
Adding a fourth filter is fine when a real rule needs it; adding a whole
expression language is not.

Errors are surfaced as :class:`SlotCompileError` with the offending slot
name so a bad program pack fails loud at publish time rather than
silently emitting an empty slot at run time.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pap_pipeline.schemas.history import HistoryEvent, HistorySlot

SlotKind = Literal["bool", "latest_outcome", "days_since_latest", "count"]


class SlotWhere(BaseModel):
    """Filter over a HistoryEvent list.

    All fields are optional and AND together. ``event_type`` and
    ``outcome`` are membership tests against a small allowlist so a slot
    def like ``{"event_type": ["determination", "enrollment"]}`` reads
    exactly the way a program editor would write it.

    ``within_days`` restricts to events dated within ``as_of - N days``.
    Compared against ``event.date`` — same field the connectors project —
    so slot values are stable across replays even when the compiler runs
    on a different day than the intake was received.
    """

    event_type: list[str] | None = None
    outcome: list[str] | None = None
    within_days: int | None = Field(default=None, ge=0)


class SlotDef(BaseModel):
    """One slot as declared in ``programs.history_slots``.

    Kept as a validating pydantic model so a malformed pack (missing
    ``name``, bogus ``kind``, negative ``within_days``) fails at publish
    time with a specific field pointer, not at run time with a cryptic
    KeyError.
    """

    name: str = Field(min_length=1)
    kind: SlotKind
    where: SlotWhere = Field(default_factory=SlotWhere)


class SlotCompileError(ValueError):
    """Raised when a slot def is malformed.

    Includes the slot ``name`` so callers can point at the exact row in
    the program pack that needs fixing. The compiler doesn't try to
    recover — if the pack is broken, the correct behavior is to refuse
    the case, not to silently degrade to an empty slot list.
    """

    def __init__(self, slot_name: str, reason: str) -> None:
        super().__init__(f"slot {slot_name!r}: {reason}")
        self.slot_name = slot_name
        self.reason = reason


def compile_slots(
    events: list[HistoryEvent],
    slot_defs: list[dict[str, Any]] | list[SlotDef],
    *,
    as_of: date | None = None,
) -> list[HistorySlot]:
    """Project ``events`` through ``slot_defs`` into a list of slots.

    Pure function. Output order matches ``slot_defs`` order — the program
    author controls the sequence, which matters for UIs that render the
    slot list verbatim next to the rules.

    ``slot_defs`` accepts already-validated :class:`SlotDef` objects or
    raw dicts from JSONB. Dicts are validated here so callers that read
    straight from ``programs.history_slots`` don't have to know about the
    pydantic layer.

    A slot always emits a :class:`HistorySlot`. When no events match,
    ``bool`` emits ``value=False``, ``count`` emits ``0``, and the
    latest-* kinds emit ``value=None``. That contract lets rules like
    "prior_enrollment_active is false" evaluate cleanly — a missing slot
    would drop the rule into ``missing_fields`` instead, which is the
    wrong branch for a rule that's asserting *absence*.
    """
    resolved_as_of = as_of or date.today()
    parsed_defs = _parse_defs(slot_defs)
    return [_compile_one(events, d, resolved_as_of) for d in parsed_defs]


def _parse_defs(
    slot_defs: list[dict[str, Any]] | list[SlotDef],
) -> list[SlotDef]:
    parsed: list[SlotDef] = []
    for i, d in enumerate(slot_defs):
        if isinstance(d, SlotDef):
            parsed.append(d)
            continue
        name = d.get("name", f"<unnamed@{i}>") if isinstance(d, dict) else f"<slot@{i}>"
        try:
            parsed.append(SlotDef.model_validate(d))
        except ValidationError as e:
            raise SlotCompileError(name, str(e)) from e
    return parsed


def _match(event: HistoryEvent, where: SlotWhere, *, as_of: date) -> bool:
    if where.event_type and event.event_type not in where.event_type:
        return False
    if where.outcome:
        # Membership on `outcome`. ``None`` is a valid outcome value
        # (pending, withdrawn synth) — only match when None is in the
        # explicit allowlist, so a filter "outcome: [approve]" doesn't
        # silently pass pending events through.
        if event.outcome is None or event.outcome not in where.outcome:
            return False
    if where.within_days is not None:
        earliest = as_of - timedelta(days=where.within_days)
        if event.date < earliest:
            return False
    return True


def _compile_one(
    events: list[HistoryEvent],
    slot: SlotDef,
    as_of: date,
) -> HistorySlot:
    matched = [e for e in events if _match(e, slot.where, as_of=as_of)]

    if slot.kind == "bool":
        return HistorySlot(
            name=slot.name,
            value=bool(matched),
            from_events=[e.event_id for e in matched],
        )

    if slot.kind == "count":
        return HistorySlot(
            name=slot.name,
            value=len(matched),
            from_events=[e.event_id for e in matched],
        )

    # Latest-* kinds share a "newest matching event" reduction — pick
    # once so the two kinds are consistent about which event they name
    # in ``from_events``.
    latest = max(matched, key=lambda e: e.date, default=None)

    if slot.kind == "latest_outcome":
        return HistorySlot(
            name=slot.name,
            value=latest.outcome if latest else None,
            from_events=[latest.event_id] if latest else [],
        )

    if slot.kind == "days_since_latest":
        value = (as_of - latest.date).days if latest else None
        return HistorySlot(
            name=slot.name,
            value=value,
            from_events=[latest.event_id] if latest else [],
        )

    # Guarded by the Literal on SlotDef.kind — reached only if a new
    # kind lands in the Literal without an accompanying branch here.
    raise SlotCompileError(slot.name, f"unhandled slot kind {slot.kind!r}")


__all__ = [
    "SlotCompileError",
    "SlotDef",
    "SlotKind",
    "SlotWhere",
    "compile_slots",
]
