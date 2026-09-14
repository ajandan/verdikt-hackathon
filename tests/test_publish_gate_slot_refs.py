"""Publish gate — a program can't activate while a linked rule references
an undeclared history slot.

The gate lives in the /ui admin route because slot defs (on the program)
and slot references (on the rule) land through two separate write paths.
Activation is the point where both must be consistent — this test drives
the helper directly with a real DB so the gate's SQL (join + is_active
filters) is exercised end-to-end.

Regressions here let a program go live with dangling slot references,
so the slot compiler emits nothing for those names and rules that
should fire silently drop into ``missing_info``.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete

from docgen.store.db import async_session, sync_session
from docgen.store.models import (
    Program,
    ProgramRuleLink,
    Rule,
    Tenant,
)
from docgen.ui.routes import _publish_gate_slot_refs


def _make_fixture(
    *,
    history_slots: list[dict],
    rule_refs: list[str] | None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed one tenant, one program (inactive), one rule referencing slots,
    and one active link. Returns (tenant_id, program_id, rule_id) so the
    caller can look them up and later tear the graph back down."""
    tid = uuid.uuid4()
    prog_id = uuid.uuid4()
    rule_id = uuid.uuid4()
    slug = f"gate-{tid.hex[:6]}"
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"gate-{tid.hex[:6]}"))
        s.flush()
        s.add(
            Program(
                id=prog_id, tenant_id=tid, slug=slug,
                name=f"gate-{slug}", is_active=False,
                history_slots=history_slots,
                extraction_schema=[],
            )
        )
        s.add(
            Rule(
                id=rule_id, tenant_id=tid,
                rule_id=f"CV-GATE-{tid.hex[:4].upper()}",
                kind="history", title="test", criterion="test",
                exception="", fields=[],
                references_slots=rule_refs,
                is_active=True,
            )
        )
        s.flush()
        s.add(
            ProgramRuleLink(
                program_id=prog_id, rule_id=rule_id,
                position=0, is_active=True,
            )
        )
        s.commit()
    return tid, prog_id, rule_id


def _teardown(tid: uuid.UUID) -> None:
    with sync_session() as s:
        s.execute(delete(ProgramRuleLink).where(
            ProgramRuleLink.program_id.in_(
                s.query(Program.id).filter(Program.tenant_id == tid)
            )
        ))
        s.execute(delete(Rule).where(Rule.tenant_id == tid))
        s.execute(delete(Program).where(Program.tenant_id == tid))
        s.execute(delete(Tenant).where(Tenant.id == tid))
        s.commit()


@pytest.mark.asyncio
async def test_gate_passes_when_referenced_slot_is_declared() -> None:
    """Consistent pack: rule references ``prior_active``, program declares
    ``prior_active`` → gate returns None (activation allowed)."""
    tid, prog_id, _ = _make_fixture(
        history_slots=[{"name": "prior_active", "kind": "bool"}],
        rule_refs=["prior_active"],
    )
    try:
        async with async_session() as s:
            prog = await s.get(Program, prog_id)
            assert prog is not None
            result = await _publish_gate_slot_refs(s, prog)
        assert result is None
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_gate_refuses_when_referenced_slot_is_undeclared() -> None:
    """Rule references ``renewal_due`` but the program only declares
    ``prior_active`` → gate returns an error message naming the missing
    slot AND the rule that referenced it (so the admin knows both which
    slot to declare and where the dependency came from)."""
    tid, prog_id, _ = _make_fixture(
        history_slots=[{"name": "prior_active", "kind": "bool"}],
        rule_refs=["prior_active", "renewal_due"],
    )
    try:
        async with async_session() as s:
            prog = await s.get(Program, prog_id)
            assert prog is not None
            result = await _publish_gate_slot_refs(s, prog)
        assert result is not None
        assert "renewal_due" in result
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_gate_ignores_rules_that_declare_no_slots() -> None:
    """Non-history rules have ``references_slots=None`` — the gate must
    treat that as "no slot dependency" and allow activation even when
    the program declares no slots. Otherwise every program would be
    forced to declare a stub history_slots list."""
    tid, prog_id, _ = _make_fixture(
        history_slots=[],
        rule_refs=None,
    )
    try:
        async with async_session() as s:
            prog = await s.get(Program, prog_id)
            assert prog is not None
            result = await _publish_gate_slot_refs(s, prog)
        assert result is None
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_gate_ignores_inactive_link() -> None:
    """A rule linked with ``is_active=False`` is paused — it won't fire in
    production. The gate must not block activation over a paused
    dependency, or reviewers couldn't temporarily park a rule while
    keeping the program live."""
    tid, prog_id, _ = _make_fixture(
        history_slots=[],
        rule_refs=["undeclared"],
    )
    # Flip the link inactive after seed.
    with sync_session() as s:
        link = s.query(ProgramRuleLink).filter(
            ProgramRuleLink.program_id == prog_id
        ).one()
        link.is_active = False
        s.commit()
    try:
        async with async_session() as s:
            prog = await s.get(Program, prog_id)
            assert prog is not None
            result = await _publish_gate_slot_refs(s, prog)
        assert result is None
    finally:
        _teardown(tid)
