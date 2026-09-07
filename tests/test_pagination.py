"""Roadmap §5: cursor-based pagination for /ui/ templates and /ui/renders.

Two layers:
- Unit tests on the `Cursor` / `Page` helpers (encoding round-trip,
  fallback on malformed cursors, page-size clamp).
- Integration tests hit the real /ui/ + /ui/renders routes with a signed
  admin session, seed N templates or render jobs, and walk every page —
  proving no duplicates, no gaps, and that filters (state=, tenant=)
  survive across page boundaries.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from docgen.auth import SESSION_COOKIE, sign_session
from docgen.main import app
from docgen.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    Page,
    clamp_page_size,
    keyset_where,
)
from docgen.store.db import sync_session
from docgen.store.models import Provenance, RenderJob, Template, TemplateVersion, Tenant

# ---------------------------------------------------------------------------
# Unit tests — Cursor / Page / clamp_page_size
# ---------------------------------------------------------------------------


def test_cursor_round_trip_preserves_ts_and_id() -> None:
    ts = datetime(2026, 8, 24, 12, 30, 45, 123456, tzinfo=UTC)
    tid = uuid.uuid4()
    encoded = Cursor(ts=ts, id=tid).encode()
    decoded = Cursor.decode(encoded)
    assert decoded is not None
    assert decoded.ts == ts
    assert decoded.id == tid


def test_cursor_decode_returns_none_for_empty_string() -> None:
    assert Cursor.decode("") is None
    assert Cursor.decode(None) is None


def test_cursor_decode_returns_none_for_garbage() -> None:
    """A stale bookmark shouldn't 400 — the caller falls back to page 1."""
    assert Cursor.decode("!!!not-base64!!!") is None
    assert Cursor.decode("aGVsbG8") is None  # base64 but no '|' separator
    assert Cursor.decode("YmFkfG5vdC1hLXV1aWQ") is None  # bad UUID after decode


def test_clamp_page_size_defaults_and_bounds() -> None:
    assert clamp_page_size(None) == DEFAULT_PAGE_SIZE
    assert clamp_page_size("") == DEFAULT_PAGE_SIZE
    assert clamp_page_size("not-a-number") == DEFAULT_PAGE_SIZE
    assert clamp_page_size(0) == DEFAULT_PAGE_SIZE
    assert clamp_page_size(-5) == DEFAULT_PAGE_SIZE
    assert clamp_page_size(25) == 25
    assert clamp_page_size(MAX_PAGE_SIZE + 500) == MAX_PAGE_SIZE
    assert clamp_page_size("10") == 10


def test_page_from_rows_no_next_when_under_limit() -> None:
    items = [{"id": i} for i in range(5)]
    page = Page.from_rows(items, page_size=10, cursor_from_row=lambda r: None)
    assert len(page.items) == 5
    assert page.next_cursor is None


def test_page_from_rows_slices_sentinel_and_yields_cursor() -> None:
    """When rows == page_size + 1, we know there's a next page and use the
    last kept row (index page_size - 1) to build the cursor."""
    ts = datetime(2026, 8, 24, tzinfo=UTC)

    class R:
        def __init__(self, i: int) -> None:
            self.id = uuid.UUID(int=i)
            self.ts = ts + timedelta(seconds=i)

    rows = [R(i) for i in range(11)]  # page_size 10 + 1 sentinel
    page = Page.from_rows(
        rows, page_size=10, cursor_from_row=lambda r: Cursor(ts=r.ts, id=r.id)
    )
    assert len(page.items) == 10
    assert page.next_cursor is not None
    decoded = Cursor.decode(page.next_cursor)
    assert decoded is not None
    assert decoded.id == uuid.UUID(int=9)  # 10th kept row


def test_keyset_where_none_cursor_returns_none() -> None:
    """No cursor → no WHERE clause added; caller can skip the .where() call."""
    assert keyset_where(Template.created_at, Template.id, None) is None


# ---------------------------------------------------------------------------
# Integration — /ui/ templates pagination
# ---------------------------------------------------------------------------


def _admin_client() -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(SESSION_COOKIE, sign_session("admin"))
    return c


@pytest.fixture
def isolated_tenant():
    """A fresh tenant per test so pagination assertions aren't polluted by
    templates or render jobs left behind by earlier tests."""
    tid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"pg-{tid.hex[:6]}", ai_provider_config={}))
        s.commit()
    yield tid
    with sync_session() as s:
        s.execute(delete(Tenant).where(Tenant.id == tid))
        s.commit()


@pytest.fixture
def seeded_templates(isolated_tenant):
    """Seed 12 templates with distinct created_at (1 second apart, newest first).

    Lives in its own tenant so ?tenant=<isolated> is enough to see exactly
    these rows and nothing else — no dependency on DB cleanliness.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    tag = f"pg-{uuid.uuid4().hex[:6]}"
    ids: list[uuid.UUID] = []
    with sync_session() as s:
        for i in range(12):
            t = Template(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                name=f"{tag}-{i:02d}",
                owner="pagination-test",
                created_at=now - timedelta(seconds=i),  # newest first
            )
            s.add(t)
            ids.append(t.id)
        s.commit()
    yield {"ids": ids, "tag": tag, "tenant_id": isolated_tenant}
    with sync_session() as s:
        s.execute(delete(Template).where(Template.id.in_(ids)))
        s.commit()


def test_templates_page_one_returns_page_size_rows(seeded_templates) -> None:
    """With ?limit=5 we get 5 rows and a Next link."""
    tag = seeded_templates["tag"]
    tid = seeded_templates["tenant_id"]
    with _admin_client() as client:
        r = client.get(f"/ui/?tenant={tid}&limit=5")
    assert r.status_code == 200
    html = r.text
    hits = [name for name in (f"{tag}-{i:02d}" for i in range(12)) if name in html]
    assert len(hits) == 5, f"expected 5 seeded rows on page 1, got {len(hits)}: {hits}"
    assert "Next →" in html


def test_templates_pagination_walks_all_pages_without_gaps(seeded_templates) -> None:
    """Follow Next until it disappears; every seeded template shows up exactly
    once across all pages (no dupes, no drops)."""
    tag = seeded_templates["tag"]
    tid = seeded_templates["tenant_id"]
    seen: set[str] = set()
    with _admin_client() as client:
        url = f"/ui/?tenant={tid}&limit=5"
        pages_walked = 0
        while url and pages_walked < 20:  # safety fuse
            r = client.get(url)
            assert r.status_code == 200, r.text[:200]
            for i in range(12):
                name = f"{tag}-{i:02d}"
                if name in r.text:
                    assert name not in seen, f"{name} appeared on two pages"
                    seen.add(name)
            url = _extract_next(r.text, "/ui/?cursor=")
            pages_walked += 1
    assert len(seen) == 12, f"expected all 12 seeded templates, saw {len(seen)}"
    assert pages_walked >= 3  # 12 rows @ 5/page → at least 3 pages


def test_templates_invalid_cursor_falls_back_to_first_page(seeded_templates) -> None:
    """Malformed cursor renders page 1 rather than 400 — bookmarks shouldn't error."""
    tag = seeded_templates["tag"]
    tid = seeded_templates["tenant_id"]
    with _admin_client() as client:
        r = client.get(f"/ui/?cursor=!!!garbage!!!&tenant={tid}&limit=5")
    assert r.status_code == 200
    hits = [name for name in (f"{tag}-{i:02d}" for i in range(12)) if name in r.text]
    assert len(hits) == 5


# ---------------------------------------------------------------------------
# Integration — /ui/renders pagination + state filter
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_render_jobs(isolated_tenant):
    """Seed one template + frozen version + 12 render jobs (mixed states)
    in an isolated tenant so the ?tenant=<isolated> filter proves we only
    see this batch."""
    now = datetime.now(UTC).replace(microsecond=0)
    tag = f"rj-{uuid.uuid4().hex[:6]}"
    template_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_ids: list[uuid.UUID] = []
    with sync_session() as s:
        s.add(
            Template(
                id=template_id, tenant_id=isolated_tenant,
                name=tag, owner="pagination-test",
            )
        )
        s.add(
            TemplateVersion(
                id=version_id,
                template_id=template_id,
                version=1,
                locale="default",
                blob_key="templates/none.docx",
                version_hash="deadbeef",
                is_frozen=True,
                status="frozen",
                schema={"fields": []},
            )
        )
        # Flush so the RenderJob inserts below see the version FK target.
        s.flush()
        for i in range(12):
            state = "failed" if i % 3 == 0 else "succeeded"
            j = RenderJob(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                template_version_id=version_id,
                caller=f"{tag}-caller-{i:02d}",
                state=state,
                payload={},
                error=f"boom-{i}" if state == "failed" else None,
                submitted_at=now - timedelta(seconds=i),
            )
            s.add(j)
            job_ids.append(j.id)
        s.commit()
    yield {
        "template_id": template_id,
        "version_id": version_id,
        "job_ids": job_ids,
        "tag": tag,
        "tenant_id": isolated_tenant,
    }
    with sync_session() as s:
        s.execute(delete(Provenance).where(Provenance.render_job_id.in_(job_ids)))
        s.execute(delete(RenderJob).where(RenderJob.id.in_(job_ids)))
        s.execute(delete(TemplateVersion).where(TemplateVersion.id == version_id))
        s.execute(delete(Template).where(Template.id == template_id))
        s.commit()


def test_renders_page_shows_seeded_jobs_and_offers_next(seeded_render_jobs) -> None:
    tag = seeded_render_jobs["tag"]
    tid = seeded_render_jobs["tenant_id"]
    with _admin_client() as client:
        r = client.get(f"/ui/renders?tenant={tid}&limit=5")
    assert r.status_code == 200
    hits = [c for c in (f"{tag}-caller-{i:02d}" for i in range(12)) if c in r.text]
    assert len(hits) == 5
    assert "Next →" in r.text


def test_renders_pagination_walks_all_seeded_jobs(seeded_render_jobs) -> None:
    tag = seeded_render_jobs["tag"]
    tid = seeded_render_jobs["tenant_id"]
    seen: set[str] = set()
    with _admin_client() as client:
        url = f"/ui/renders?tenant={tid}&limit=5"
        walked = 0
        while url and walked < 20:
            r = client.get(url)
            assert r.status_code == 200
            for i in range(12):
                caller = f"{tag}-caller-{i:02d}"
                if caller in r.text:
                    assert caller not in seen
                    seen.add(caller)
            url = _extract_next(r.text, "/ui/renders?cursor=")
            walked += 1
    assert len(seen) == 12
    assert walked >= 3


def test_renders_state_filter_only_returns_matching_state(seeded_render_jobs) -> None:
    """?state=failed should only show the failed callers (i % 3 == 0), and the
    filter must survive across page boundaries via the Next link."""
    tag = seeded_render_jobs["tag"]
    tid = seeded_render_jobs["tenant_id"]
    failed_callers = {f"{tag}-caller-{i:02d}" for i in range(12) if i % 3 == 0}
    succeeded_callers = {f"{tag}-caller-{i:02d}" for i in range(12) if i % 3 != 0}
    seen: set[str] = set()
    with _admin_client() as client:
        url = f"/ui/renders?state=failed&tenant={tid}&limit=2"
        walked = 0
        while url and walked < 20:
            r = client.get(url)
            assert r.status_code == 200
            for c in failed_callers | succeeded_callers:
                if c in r.text:
                    seen.add(c)
            url = _extract_next(r.text, "/ui/renders?cursor=")
            walked += 1
    assert failed_callers <= seen, f"missing failed callers: {failed_callers - seen}"
    assert not (succeeded_callers & seen), \
        f"succeeded jobs leaked into state=failed view: {succeeded_callers & seen}"


def test_renders_unknown_state_filter_is_ignored(seeded_render_jobs) -> None:
    """?state=bogus should behave the same as no filter — not 400."""
    tag = seeded_render_jobs["tag"]
    tid = seeded_render_jobs["tenant_id"]
    with _admin_client() as client:
        r = client.get(f"/ui/renders?state=bogus&tenant={tid}&limit=50")
    assert r.status_code == 200
    for i in range(12):
        assert f"{tag}-caller-{i:02d}" in r.text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _extract_next(html_source: str, url_prefix: str) -> str | None:
    """Pull the href out of the 'Next →' link. Returns None if there is no
    next page. HTML-decodes entities (`&amp;` → `&`) so query strings survive
    — a real browser does this on link click; httpx does not.
    """
    import html as _html

    marker = 'class="btn sm primary">Next'
    idx = html_source.find(marker)
    if idx == -1:
        return None
    href_start = html_source.rfind('href="', 0, idx)
    if href_start == -1:
        return None
    href_start += len('href="')
    href_end = html_source.find('"', href_start)
    if href_end == -1:
        return None
    href = _html.unescape(html_source[href_start:href_end])
    if not href.startswith(url_prefix.split("?", 1)[0]):
        return None
    return href
