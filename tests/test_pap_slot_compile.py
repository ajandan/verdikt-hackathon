"""Slot compiler — pure-function contract tests.

The compiler is the single place that projects ``HistoryEvent`` rows into
named booleans / enums / counts / days-since values. It runs inside the
mdm-lookup seam in production but the seam is fed through a pure function
so tests can pin behavior without any network / clock / async work.

We pin the four kinds the ADR names — ``bool``, ``latest_outcome``,
``days_since_latest``, ``count`` — plus the filter shape (``event_type``,
``outcome``, ``within_days``), the empty-events fallback contract, and
the "malformed def is a publish-time error, not a runtime silent-empty"
guarantee. Regressions in any of those change the fact set the reason
stage receives, and a rule that used to fire will silently start hitting
``missing_info``.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from pap_pipeline.schemas.history import HistoryEvent
from pap_pipeline.stages.slot_compile import (
    SlotCompileError,
    SlotDef,
    compile_slots,
)


AS_OF = date(2026, 9, 14)


def _event(
    *,
    event_id: str = "E1",
    event_type: str = "determination",
    outcome: str | None = "approve",
    days_ago: int = 100,
    program_family_id: str = "cardiovita",
    reason_code: str | None = None,
) -> HistoryEvent:
    """Minimal history event fixture. Defaults describe the most-common
    demo scenario (a determination-approve ~3 months ago) so each test
    can override just the field it's exercising."""
    return HistoryEvent(
        event_id=event_id,
        event_type=event_type,  # type: ignore[arg-type]
        program_id=program_family_id,
        program_family_id=program_family_id,
        date=AS_OF - timedelta(days=days_ago),
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason_code,
        source_system="test",
        record_hash="sha256:test",
    )


# ---------------------------------------------------------------------------
# kind: bool
# ---------------------------------------------------------------------------


def test_bool_slot_true_when_matching_event_present() -> None:
    """One matching event → value True, ``from_events`` names it. This is
    the base case history-referencing rules lean on the most."""
    slots = compile_slots(
        [_event(event_id="E1")],
        [{"name": "prior_active", "kind": "bool",
          "where": {"event_type": ["determination"], "outcome": ["approve"]}}],
        as_of=AS_OF,
    )
    assert slots[0].name == "prior_active"
    assert slots[0].value is True
    assert slots[0].from_events == ["E1"]


def test_bool_slot_false_when_no_events_match() -> None:
    """No matching events → value False (not None). Rules assert absence
    ("prior_active is false"); a None value would drop them into
    ``missing_info`` instead of firing the deny/approve branch."""
    slots = compile_slots(
        [_event(event_type="renewal")],
        [{"name": "prior_active", "kind": "bool",
          "where": {"event_type": ["determination"]}}],
        as_of=AS_OF,
    )
    assert slots[0].value is False
    assert slots[0].from_events == []


def test_bool_slot_ignores_pending_when_outcome_filter_is_explicit() -> None:
    """``outcome: [approve]`` must not silently pass a ``None`` outcome
    through. A pending event is not an approve — treating it as one
    would deliver false-positive citations to the letter."""
    events = [
        _event(event_id="E-pending", outcome=None),
        _event(event_id="E-approve", outcome="approve"),
    ]
    slots = compile_slots(
        events,
        [{"name": "any_approve", "kind": "bool",
          "where": {"outcome": ["approve"]}}],
        as_of=AS_OF,
    )
    assert slots[0].value is True
    assert slots[0].from_events == ["E-approve"]


# ---------------------------------------------------------------------------
# kind: latest_outcome
# ---------------------------------------------------------------------------


def test_latest_outcome_picks_newest_matching_event() -> None:
    """When multiple events match, the newest one's outcome wins — this
    is what a reviewer expects "the latest determination" to mean."""
    events = [
        _event(event_id="OLD", outcome="deny", days_ago=400),
        _event(event_id="NEW", outcome="approve", days_ago=30),
    ]
    slots = compile_slots(
        events,
        [{"name": "last", "kind": "latest_outcome",
          "where": {"event_type": ["determination"]}}],
        as_of=AS_OF,
    )
    assert slots[0].value == "approve"
    assert slots[0].from_events == ["NEW"]


def test_latest_outcome_is_none_when_no_matches() -> None:
    """No matching events → ``value=None``. Rules that need this field
    to be present drop into missing_info; that's the correct branch when
    we genuinely don't know what the last outcome was."""
    slots = compile_slots(
        [],
        [{"name": "last", "kind": "latest_outcome"}],
        as_of=AS_OF,
    )
    assert slots[0].value is None
    assert slots[0].from_events == []


# ---------------------------------------------------------------------------
# kind: days_since_latest
# ---------------------------------------------------------------------------


def test_days_since_latest_is_measured_against_as_of() -> None:
    """The compiler must key on the injected ``as_of`` — not ``date.today()`` —
    so replay is stable across days. Passing today would drift the value
    on every rerun, breaking the ledger's replayability guarantee."""
    slots = compile_slots(
        [_event(days_ago=42)],
        [{"name": "gap", "kind": "days_since_latest"}],
        as_of=AS_OF,
    )
    assert slots[0].value == 42


def test_days_since_latest_none_when_no_matches() -> None:
    """No matching events → None (not a large-number sentinel). Consumers
    check for None explicitly; a sentinel would smuggle a false fact
    through the rules pack."""
    slots = compile_slots(
        [],
        [{"name": "gap", "kind": "days_since_latest"}],
        as_of=AS_OF,
    )
    assert slots[0].value is None


# ---------------------------------------------------------------------------
# kind: count
# ---------------------------------------------------------------------------


def test_count_slot_counts_matching_events() -> None:
    """``count`` returns the number of matching events; ``from_events``
    lists them all so a "you have appealed N times" letter can enumerate."""
    events = [
        _event(event_id="A", event_type="appeal", outcome=None),
        _event(event_id="B", event_type="appeal", outcome=None),
        _event(event_id="C", event_type="determination"),
    ]
    slots = compile_slots(
        events,
        [{"name": "appeal_count", "kind": "count",
          "where": {"event_type": ["appeal"]}}],
        as_of=AS_OF,
    )
    assert slots[0].value == 2
    assert set(slots[0].from_events) == {"A", "B"}


def test_count_zero_when_no_matches() -> None:
    """``count`` never yields None. Rules can do "at least one" checks
    against a numeric 0; None would break the comparison."""
    slots = compile_slots(
        [_event(event_type="determination")],
        [{"name": "appeals", "kind": "count",
          "where": {"event_type": ["appeal"]}}],
        as_of=AS_OF,
    )
    assert slots[0].value == 0


# ---------------------------------------------------------------------------
# Filter shape
# ---------------------------------------------------------------------------


def test_within_days_filter_excludes_events_outside_window() -> None:
    """A 90-day window must reject the 200-day-old event. The compiler
    windows on ``event.date`` (same field connectors project) so replay
    stays stable."""
    events = [
        _event(event_id="RECENT", days_ago=30),
        _event(event_id="STALE", days_ago=200),
    ]
    slots = compile_slots(
        events,
        [{"name": "recent_approve", "kind": "bool",
          "where": {"outcome": ["approve"], "within_days": 90}}],
        as_of=AS_OF,
    )
    assert slots[0].value is True
    assert slots[0].from_events == ["RECENT"]


def test_output_order_matches_slot_defs_order() -> None:
    """Editors control the order slots render in — the compiler must not
    reorder them (e.g. alphabetically), or UIs that render the list
    verbatim next to rules will drift out of the author's intent."""
    slots = compile_slots(
        [],
        [
            {"name": "z_slot", "kind": "bool"},
            {"name": "a_slot", "kind": "bool"},
        ],
        as_of=AS_OF,
    )
    assert [s.name for s in slots] == ["z_slot", "a_slot"]


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------


def test_malformed_slot_def_raises_slot_compile_error() -> None:
    """A pack with a bogus ``kind`` fails loud at compile time. Silent
    empty-slot fallback would hide a broken program pack until a case
    actually landed under it."""
    with pytest.raises(SlotCompileError) as exc:
        compile_slots(
            [],
            [{"name": "broken", "kind": "not-a-real-kind"}],
            as_of=AS_OF,
        )
    assert exc.value.slot_name == "broken"


def test_accepts_prevalidated_slot_defs() -> None:
    """Callers that already have :class:`SlotDef` objects (e.g. from a
    cached pack) must not have to convert back to dicts. Exercises the
    ``isinstance(SlotDef)`` fast path."""
    d = SlotDef.model_validate({"name": "x", "kind": "bool"})
    slots = compile_slots([_event()], [d], as_of=AS_OF)
    assert slots[0].name == "x"
    assert slots[0].value is True
