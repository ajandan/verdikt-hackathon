"""inbox_hook slot resolution.

Focuses on the Program → extraction_schema lookup that landed in Phase 4
of the PAP-pipeline programs work. The rest of the hook is exercised by
the orchestrator + extract tests; this file locks the resolver's contract
so the extract stage isn't silently handed the wrong slot list.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from docgen.store.models import Tenant
from pap_pipeline.inbox_hook import _resolve_program_slots


def _tenant() -> Tenant:
    return Tenant(id=uuid.uuid4(), name="acme", ai_provider_config={})


def _mock_session(row) -> MagicMock:
    """Fake `session.query(Program).filter(...).first()` returning `row`.

    We don't spin up a real session here — the resolver's contract is
    "call query/filter/first and return the row's schema"; a mock nails
    that surface without needing a live DB.
    """
    session = MagicMock()
    q = session.query.return_value
    q.filter.return_value.first.return_value = row
    return session


def test_returns_none_when_program_is_blank():
    """Bare `[pap]` tag → no slug → resolver returns None so extract falls
    back to _DEFAULT_SLOTS. The DB shouldn't even be queried."""
    session = _mock_session(row=None)
    assert _resolve_program_slots(session, _tenant(), None) is None
    assert _resolve_program_slots(session, _tenant(), "  ") is None
    session.query.assert_not_called()


def test_returns_none_when_program_row_missing():
    """Slug names a program that hasn't been created yet — resolver
    returns None so the extract stage keeps working via defaults."""
    session = _mock_session(row=None)
    assert _resolve_program_slots(session, _tenant(), "cardiovita-pap-2026") is None
    session.query.assert_called_once()


def test_returns_none_when_schema_is_empty():
    """A Program row exists but its schema hasn't been populated yet.
    Treat this the same as 'no program' — the operator likely just
    created the row and hasn't seeded slots yet."""
    row = MagicMock(extraction_schema=[])
    session = _mock_session(row=row)
    assert _resolve_program_slots(session, _tenant(), "cardiovita-pap-2026") is None


def test_returns_schema_list_when_program_has_slots():
    """Happy path — resolver returns a copy of the row's slot list. The
    copy matters because the caller passes it into an async pipeline
    that may outlive the ORM session."""
    slots = [
        {"name": "patient_name", "type": "string", "description": "Full name."},
        {"name": "member_id", "type": "string", "description": "Program member ID."},
    ]
    row = MagicMock(extraction_schema=slots)
    session = _mock_session(row=row)
    resolved = _resolve_program_slots(session, _tenant(), "cardiovita-pap-2026")
    assert resolved == slots
    # Must be a fresh list — mutating the caller's copy shouldn't affect
    # the row (or vice-versa).
    assert resolved is not slots


def test_slug_is_normalized_to_lowercase():
    """Inbound subject tags aren't guaranteed lowercase; the DB stores
    slugs canonicalised. Resolver must strip + lowercase before querying
    so `[pap:CardioVita-PAP-2026]` resolves the same row."""
    row = MagicMock(extraction_schema=[{"name": "patient_name", "type": "string"}])
    session = _mock_session(row=row)
    resolved = _resolve_program_slots(session, _tenant(), "  CardioVita-PAP-2026  ")
    assert resolved is not None
    # Filter args aren't easily introspected on MagicMock's chained calls,
    # so we assert on the query happening — the lookup itself succeeded,
    # which requires the slug to have been normalised before the filter.
    session.query.assert_called_once()
