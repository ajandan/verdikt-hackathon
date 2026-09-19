"""Page-number pagination helpers — the single source of truth for every
admin list view. Locking the invariants here so a refactor of the module
can't silently break every list at once.

Not covered here: the Jinja macro rendering (visual regression). The
paginate_query DB integration is covered indirectly by the UI smoke +
browser-E2E runs; this file focuses on the pure helpers where the sharp
edges live (bad input coercion, off-by-one on windows, empty results).
"""
from __future__ import annotations

import pytest

from docgen.pagination import (
    LIST_PAGE_SIZE,
    PageParams,
    PageResult,
    _clamp_list_size,
)

# ---------------------------------------------------------------------------
# PageParams.from_request — the bad-input safety net
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_page, expected", [
    (None, 1),
    ("", 1),
    ("1", 1),
    ("7", 7),
    (7, 7),
    ("0", 1),      # 0 is nonsense → default to page 1
    ("-5", 1),     # negatives too
    ("abc", 1),    # garbage → default
    (None, 1),
])
def test_from_request_coerces_page(raw_page, expected):
    p = PageParams.from_request(raw_page)
    assert p.page == expected
    assert p.size == LIST_PAGE_SIZE  # default when not supplied


@pytest.mark.parametrize("raw_size, expected", [
    (None, LIST_PAGE_SIZE),
    ("", LIST_PAGE_SIZE),
    ("10", 10),
    ("50", 50),
    ("0", LIST_PAGE_SIZE),   # 0 nonsense → default
    ("-1", LIST_PAGE_SIZE),
    ("9999", 100),           # clamped to LIST_MAX_PAGE_SIZE
    ("abc", LIST_PAGE_SIZE),
])
def test_clamp_list_size_bounds(raw_size, expected):
    assert _clamp_list_size(raw_size) == expected


def test_offset_matches_page_and_size():
    """PageParams.offset must be (page-1)*size — this is what the OFFSET
    clause in paginate_query uses, and an off-by-one here would silently
    skip or repeat rows on every list view."""
    assert PageParams(page=1, size=20).offset == 0
    assert PageParams(page=2, size=20).offset == 20
    assert PageParams(page=5, size=20).offset == 80
    assert PageParams(page=3, size=50).offset == 100


# ---------------------------------------------------------------------------
# PageResult properties — start/end index, has_prev/has_next, window
# ---------------------------------------------------------------------------

def _pr(page: int, size: int, total: int, items: list | None = None) -> PageResult:
    total_pages = max(1, (total + size - 1) // size) if total else 1
    return PageResult(
        items=items or [], page=page, size=size, total=total, total_pages=total_pages,
    )


def test_empty_result_reports_zero_start_index():
    p = _pr(page=1, size=20, total=0)
    assert p.start_index == 0
    assert p.end_index == 0
    assert p.total_pages == 1
    assert p.has_prev is False
    assert p.has_next is False
    assert p.page_window() == [1]


def test_start_end_index_for_full_pages():
    """Page 1 covers rows 1-20; page 3 covers rows 41-60."""
    p = _pr(page=1, size=20, total=100)
    assert p.start_index == 1
    assert p.end_index == 20
    p = _pr(page=3, size=20, total=100)
    assert p.start_index == 41
    assert p.end_index == 60


def test_end_index_clamps_on_last_partial_page():
    """Last page of a non-multiple total shouldn't overshoot."""
    p = _pr(page=3, size=20, total=45)
    assert p.start_index == 41
    assert p.end_index == 45


def test_has_prev_has_next_at_boundaries():
    p = _pr(page=1, size=20, total=100)
    assert p.has_prev is False
    assert p.has_next is True
    p = _pr(page=5, size=20, total=100)
    assert p.has_prev is True
    assert p.has_next is False
    p = _pr(page=3, size=20, total=100)
    assert p.has_prev is True and p.has_next is True


def test_page_window_short_list():
    """Total pages <= window * 2 + 1 → every page appears."""
    p = _pr(page=1, size=20, total=60)  # 3 pages
    assert p.page_window() == [1, 2, 3]


def test_page_window_middle_of_long_list():
    """Long list, page in the middle → window + first + last, no dup."""
    p = _pr(page=10, size=20, total=400)  # 20 pages
    # span=2 default: [8, 9, 10, 11, 12] + {1, 20}
    assert p.page_window() == [1, 8, 9, 10, 11, 12, 20]


def test_page_window_at_edges_no_duplicates():
    """Window near page 1 shouldn't double-count page 1; same at the end."""
    p = _pr(page=1, size=20, total=400)
    win = p.page_window()
    assert win == sorted(set(win))
    assert win[0] == 1
    assert p.total_pages in win
    p = _pr(page=20, size=20, total=400)
    win = p.page_window()
    assert win == sorted(set(win))
    assert win[0] == 1
    assert p.total_pages == win[-1]


def test_prev_next_page_clamped():
    p = _pr(page=1, size=20, total=100)
    assert p.prev_page == 1  # can't go below 1
    assert p.next_page == 2
    p = _pr(page=5, size=20, total=100)
    assert p.prev_page == 4
    assert p.next_page == 5  # can't overshoot total_pages
