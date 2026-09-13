"""Orchestrator flattens MDM history slots into extras keys.

Rules that reference history slots (e.g. ``fields: [prior_enrollment_active]``)
have no way to reach into a nested ``history_slots`` list at reason time. The
orchestrator flattens each slot's ``name -> value`` onto extras so the reason
stage evaluates the rule against a top-level key the same way it evaluates an
intake field. Regressions here silently downgrade history-typed rules to
``missing_info`` for every case — a demo-breaking change worth pinning.

The nested ``history_slots`` list must stay too — downstream verdict + letter
consumers use it for the ``from_events`` audit trail. Flatten *in addition to*,
not instead of.
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone

from pap_pipeline.orchestrator import _merge_history_slots
from pap_pipeline.schemas.history import HistorySlice, HistorySlot


def _slice(*slots: HistorySlot) -> HistorySlice:
    """Minimal slice — the orchestrator flatten step only touches ``slots``.

    events / member_id / snapshot fields don't matter for this shape check,
    but Pydantic requires them so we pass through the smallest valid shape.
    """
    return HistorySlice(
        member_id="M-1",
        program_family_id="cardiovita",
        events=[],
        slots=list(slots),
        snapshot_hash="sha256:test",
        snapshot_at=datetime.now(UTC),
        source_provider="simulated",
    )


def test_flatten_lifts_slot_name_to_top_level_key() -> None:
    """A slot named ``prior_enrollment_active`` becomes an extras key with
    the same name so ``fields: [prior_enrollment_active]`` on a rule hits it
    like any intake field."""
    merged = _merge_history_slots(
        {},
        _slice(HistorySlot(name="prior_enrollment_active", value=True, from_events=["E1"])),
    )
    assert merged["prior_enrollment_active"] is True


def test_flatten_preserves_nested_list_for_audit() -> None:
    """The nested ``history_slots`` list stays alongside the flat keys —
    letter / verdict renderers rely on ``from_events`` to name which event
    made a slot true."""
    merged = _merge_history_slots(
        {},
        _slice(HistorySlot(name="prior_enrollment_active", value=True, from_events=["E1"])),
    )
    assert merged["history_slots"] == [
        {"name": "prior_enrollment_active", "value": True, "from_events": ["E1"]}
    ]


def test_flatten_does_not_override_existing_extras_key() -> None:
    """Program-specific extraction fields land in extras first. A slot that
    collides on name must not silently overwrite what the extractor lifted —
    the program author's field wins because it was declared closer to the
    intake shape."""
    merged = _merge_history_slots(
        {"prior_enrollment_active": "from-extractor"},
        _slice(HistorySlot(name="prior_enrollment_active", value=True, from_events=[])),
    )
    assert merged["prior_enrollment_active"] == "from-extractor"


def test_flatten_handles_empty_slots() -> None:
    """No slots (e.g. the null MDM adapter) → no history_slots key, no flat
    keys. The reason stage should see a plain extras dict, not a stub."""
    merged = _merge_history_slots({"foo": "bar"}, _slice())
    assert "history_slots" not in merged
    assert merged == {"foo": "bar"}


def test_flatten_handles_none_extras() -> None:
    """Callers with no program-specific extras pass extras=None. The flatten
    step must upgrade that to an empty dict before writing slot keys so the
    reason stage never receives None."""
    merged = _merge_history_slots(
        None,
        _slice(HistorySlot(name="prior_enrollment_active", value=False, from_events=[])),
    )
    assert merged["prior_enrollment_active"] is False
    assert merged["history_slots"] == [
        {"name": "prior_enrollment_active", "value": False, "from_events": []}
    ]
