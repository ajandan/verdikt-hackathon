"""Multi-test executor: plans + renders + persists trials for one event.

These tests drive the executor with in-memory stubs so no DB, no S3, no
network is required. The point is to verify:

  - one InboxTestResult row per trial makes it into session.add_all
  - render failures still land a row (blob_key=None, verdict names failure)
  - each ExecutedTrial gets its persisted row id back so the caller can
    build a signed download URL without a second SELECT
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from docgen.inbox import test_executor
from docgen.inbox.test_executor import execute_test_matrix
from docgen.inbox.test_planner import Trial
from docgen.inbox.test_verdict import Verdict
from docgen.store.models import InboxTestResult


class _StubSession:
    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj) -> None:  # pragma: no cover — executor uses add_all
        self.added.append(obj)

    def add_all(self, objs) -> None:
        for o in objs:
            if isinstance(o, InboxTestResult) and getattr(o, "id", None) is None:
                o.id = uuid.uuid4()
            self.added.append(o)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, obj) -> None:
        return None


def _settings():
    return SimpleNamespace(
        s3_bucket_documents="docgen-documents",
        ai_base_url=None,
        ai_api_key=None,
        ai_model="",
        ai_provider="stub",
        ai_timeout_seconds=1.0,
    )


def _tv(schema=None, pdf_flavor=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        schema=schema or {"fields": [{"name": "member_name", "type": "string"}]},
        pdf_flavor=pdf_flavor,
    )


@pytest.fixture
def _no_blob(monkeypatch):
    """Neutralize the S3 put — we're not testing MinIO here."""
    monkeypatch.setattr(test_executor, "put_object", lambda *a, **kw: None)


@pytest.fixture
def _canned_trials(monkeypatch):
    """Force plan_trials to return a fixed 2-trial matrix."""
    trials = [
        Trial(name="happy", description="ok", payload={"member_name": "Alice"}),
        Trial(name="edge",  description="edge", payload={"member_name": "Bob"}),
    ]
    monkeypatch.setattr(
        test_executor, "plan_trials",
        lambda schema_dict, **kwargs: trials,
    )
    return trials


def test_persists_one_row_per_trial(monkeypatch, _no_blob, _canned_trials):
    monkeypatch.setattr(
        test_executor, "execute_merge",
        lambda tv, data, output_format, settings: b"fake-docx-bytes",
    )
    monkeypatch.setattr(
        test_executor, "extract_rendered_text",
        lambda rendered, ext: "Dear Alice, ...",
    )
    monkeypatch.setattr(
        test_executor, "score_trial",
        lambda **kw: Verdict(passed=True, verdict="Looks good."),
    )

    session = _StubSession()
    event_id = uuid.uuid4()
    tv = _tv()

    result = execute_test_matrix(
        session,
        event_id=event_id,
        tv=tv,
        tenant_ai_provider_config=None,
        settings=_settings(),
    )

    rows = [o for o in session.added if isinstance(o, InboxTestResult)]
    assert len(rows) == 2
    assert [r.ordinal for r in rows] == [1, 2]
    assert [r.name for r in rows] == ["happy", "edge"]
    assert all(r.blob_key for r in rows)  # renders succeeded
    assert all(r.passed for r in rows)
    # Executor must expose ids so the caller can build signed URLs.
    assert all(t.test_result_id is not None for t in result)
    assert [t.test_result_id for t in result] == [r.id for r in rows]


def test_render_failure_still_lands_row(monkeypatch, _no_blob, _canned_trials):
    def _boom(tv, data, output_format, settings):
        raise RuntimeError("libreoffice crashed")

    monkeypatch.setattr(test_executor, "execute_merge", _boom)
    monkeypatch.setattr(
        test_executor, "score_trial",
        lambda **kw: Verdict(passed=False, verdict="Render failed: RuntimeError"),
    )

    session = _StubSession()
    result = execute_test_matrix(
        session,
        event_id=uuid.uuid4(),
        tv=_tv(),
        tenant_ai_provider_config=None,
        settings=_settings(),
    )

    rows = [o for o in session.added if isinstance(o, InboxTestResult)]
    assert len(rows) == 2
    for r in rows:
        assert r.blob_key is None
        assert r.ext is None
        assert r.passed is False
    # Executed trials mirror the failure so the reply builder sees it.
    assert all(t.rendered is None for t in result)
    assert all(t.render_error and "render_failed" in t.render_error for t in result)


def test_planner_exception_returns_empty(monkeypatch):
    """A schema-invalid template makes the planner raise — the executor
    must swallow and return [] so the caller can fall back to the legacy
    single-render path without a stack trace propagating out."""

    def _raise(*a, **kw):
        raise ValueError("bogus schema")

    monkeypatch.setattr(test_executor, "plan_trials", _raise)

    session = _StubSession()
    result = execute_test_matrix(
        session,
        event_id=uuid.uuid4(),
        tv=_tv(),
        tenant_ai_provider_config=None,
        settings=_settings(),
    )

    assert result == []
    assert session.added == []
    assert session.commits == 0
