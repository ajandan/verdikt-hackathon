"""Open-redirect guard on ``?next=`` params (CWE-601).

Round-8 review flagged that ``dest.startswith('/')`` accepted
``//evil.example`` — protocol-relative URLs that browsers resolve to
external hosts. The fix landed a shared ``_is_safe_local_redirect``
helper used by the login handler and the tenant-picker submit. This
file locks the helper's contract so a future refactor doesn't
reintroduce the accept-anything-that-starts-with-slash bug.
"""
from __future__ import annotations

import pytest

from docgen.ui.routes import _is_safe_local_redirect


@pytest.mark.parametrize("dest", [
    "/",
    "/ui/dashboard",
    "/ui/inbox",
    "/ui/tenants/abc-123/keys",
    "/pap/cases/QC-0042",
    "/?q=1",
    "/ui/dashboard?tenant=x",
])
def test_same_origin_paths_accepted(dest: str):
    assert _is_safe_local_redirect(dest) is True


@pytest.mark.parametrize("dest", [
    "//evil.example",
    "//evil.example/path",
    "//evil.example/ui/dashboard",
])
def test_protocol_relative_rejected(dest: str):
    """The core round-8 finding — `//host` resolves to an external URL
    in every browser even though it starts with a slash."""
    assert _is_safe_local_redirect(dest) is False


@pytest.mark.parametrize("dest", [
    "/\\evil.example",
    "/\\\\evil.example/path",
])
def test_backslash_prefix_rejected(dest: str):
    r"""Some browsers treat `/\host` similarly to `//host` — a defensive
    reject on the backslash prefix closes that quirk."""
    assert _is_safe_local_redirect(dest) is False


@pytest.mark.parametrize("dest", [
    "https://evil.example/",
    "http://evil.example/",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "ftp://evil.example/",
])
def test_scheme_url_rejected(dest: str):
    """Any fully-qualified URL is rejected — only local paths pass."""
    assert _is_safe_local_redirect(dest) is False


@pytest.mark.parametrize("dest", [
    "",
    "ui/dashboard",         # relative path, no leading slash
    "dashboard",
    "?next=x",              # query only
    "#hash",                # fragment only
])
def test_non_absolute_paths_rejected(dest: str):
    assert _is_safe_local_redirect(dest) is False


def test_none_input_rejected():
    """The helper accepts ``str | None`` because callers read from a
    query param that may not be present; None must not slip through."""
    assert _is_safe_local_redirect(None) is False
