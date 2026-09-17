"""Opaque cursor-based (keyset) pagination for admin list pages.

Keyset pagination beats OFFSET/LIMIT for two reasons the roadmap calls out:

- Constant query cost regardless of page depth. A tenant on page 200 of their
  render history pays the same DB work as page 1; OFFSET has to scan and
  discard the preceding rows.
- Stable across concurrent inserts. If a new render lands while the user is
  paging, they don't see a row shift down onto the next page (or skip one).

The cursor is a base64-encoded `(timestamp, uuid)` pair — the timestamp is the
primary sort key, uuid the tiebreaker. It's opaque to the client; we don't
promise any structure, so we're free to change the encoding later.

Forward-only. The templates and renders lists don't need "Previous" — users
navigate back with the browser's Back button (which restores the earlier
cursor from the URL). Adding a symmetric back-cursor would need every page
to also carry its own start cursor, and the UX win is small.
"""
from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Admin list views (tenants, programs, rules, keys, packages, static docs,
# users, provenance, inbox) use classic page-number pagination — smaller
# data sets, familiar "Page N of M · ‹ 1 2 3 4 ›" UX. The keyset/cursor
# style above is reserved for the two lists that can hit thousands of rows
# (templates, renders). Change LIST_PAGE_SIZE here to shift every admin
# list at once.
LIST_PAGE_SIZE = 10
LIST_MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class Cursor:
    """Composite sort position: (timestamp desc, id desc)."""

    ts: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.ts.isoformat()}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @classmethod
    def decode(cls, value: str | None) -> Cursor | None:
        """Return None for empty / malformed cursors — the caller falls back
        to page 1 rather than 400ing. A stale bookmark shouldn't error."""
        if not value:
            return None
        try:
            pad = "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(value + pad).decode("ascii")
            ts_str, id_str = raw.rsplit("|", 1)
            return cls(ts=datetime.fromisoformat(ts_str), id=uuid.UUID(id_str))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None


def clamp_page_size(raw: int | str | None) -> int:
    """Coerce a user-supplied limit into `[1, MAX_PAGE_SIZE]`, default 50."""
    if raw is None or raw == "":
        return DEFAULT_PAGE_SIZE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if n < 1:
        return DEFAULT_PAGE_SIZE
    return min(n, MAX_PAGE_SIZE)


def keyset_where(ts_col: Any, id_col: Any, cursor: Cursor | None) -> Any | None:
    """Return a WHERE clause that selects rows strictly *after* `cursor` in
    (ts desc, id desc) order — i.e. `(ts, id) < (cursor.ts, cursor.id)`.

    Returns None if there's no cursor (caller should skip the .where()).
    """
    if cursor is None:
        return None
    return or_(
        ts_col < cursor.ts,
        and_(ts_col == cursor.ts, id_col < cursor.id),
    )


@dataclass
class Page:
    """One page of results plus the cursor to fetch the next page (or None
    if this is the last page)."""

    items: list[Any]
    next_cursor: str | None

    @classmethod
    def from_rows(
        cls,
        rows: list[Any],
        page_size: int,
        cursor_from_row,
    ) -> Page:
        """Slice off the +1 sentinel we fetched to detect a next page.

        `cursor_from_row(row)` must return a `Cursor` for the last row in the
        current page — that's what the "Next" link encodes.
        """
        has_next = len(rows) > page_size
        items = rows[:page_size]
        next_cursor: str | None = None
        if has_next and items:
            next_cursor = cursor_from_row(items[-1]).encode()
        return cls(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Page-number pagination — for the admin list views.
#
# One helper + one Jinja macro + one query wrapper. Wiring a new list view
# is 3 lines in the route (parse params, run paginate_query, thread the
# result into the template) plus one macro call in the template. Change
# LIST_PAGE_SIZE above to shift every admin list at once.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageParams:
    """Parsed ``?page=N&size=M`` from a request. Both fields are always
    clamped to safe values so downstream code can trust them."""

    page: int
    size: int

    @classmethod
    def from_request(
        cls,
        page: int | str | None,
        size: int | str | None = None,
    ) -> PageParams:
        """Coerce raw query-string values into a safe ``(page, size)``.

        Bad / missing values fall back to ``page=1`` and the module-wide
        default size — a stale bookmark shouldn't 400."""
        try:
            p = int(page) if page not in (None, "") else 1
        except (TypeError, ValueError):
            p = 1
        if p < 1:
            p = 1
        s = _clamp_list_size(size)
        return cls(page=p, size=s)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def _clamp_list_size(raw: int | str | None) -> int:
    if raw is None or raw == "":
        return LIST_PAGE_SIZE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return LIST_PAGE_SIZE
    if n < 1:
        return LIST_PAGE_SIZE
    return min(n, LIST_MAX_PAGE_SIZE)


@dataclass(frozen=True)
class PageResult:
    """One page of items + everything the pagination macro needs to render
    controls. Deliberately dumb — no callbacks, no lazy loading. The route
    computes this once and hands it to the template."""

    items: list[Any]
    page: int
    size: int
    total: int
    total_pages: int

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def prev_page(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_page(self) -> int:
        return min(self.total_pages, self.page + 1)

    @property
    def start_index(self) -> int:
        """1-indexed row number of the first item on this page. 0 when
        the result set is empty — templates can use `{% if start_index %}`
        to hide the range readout on empty pages."""
        return 0 if self.total == 0 else (self.page - 1) * self.size + 1

    @property
    def end_index(self) -> int:
        """1-indexed row number of the last item on this page."""
        return min(self.total, self.page * self.size)

    def page_window(self, span: int = 2) -> list[int]:
        """Compact page-number window around the current page — capped by
        ``span`` on either side. Templates iterate this to render the
        numbered links. Always includes 1 and total_pages if there's room."""
        if self.total_pages <= 1:
            return [1] if self.total_pages else []
        lo = max(1, self.page - span)
        hi = min(self.total_pages, self.page + span)
        # Always show first + last page so the range is visible even
        # when the user is in the middle of a long list.
        pages: set[int] = {1, self.total_pages, *range(lo, hi + 1)}
        return sorted(pages)


async def paginate_query(
    session: Any,
    stmt: Select,
    params: PageParams,
    *,
    scalars: bool = True,
) -> PageResult:
    """Run ``COUNT`` + ``SELECT ... LIMIT ... OFFSET`` for the given
    statement and return a :class:`PageResult`.

    OFFSET/LIMIT is chosen deliberately over keyset here — admin lists
    (tenants, programs, rules, users, keys) rarely exceed a few hundred
    rows and users expect ``?page=3`` UX with total-page hints. If a list
    grows into the thousands, migrate that specific route to cursor
    pagination via ``Cursor`` / ``keyset_where`` above.

    The caller passes a fully-composed ``stmt`` (WHERE clauses, ORDER BY,
    joins, all applied). This helper only adds ``LIMIT`` + ``OFFSET`` and
    wraps a COUNT query around the same criteria.

    ``scalars`` — set to ``False`` when the SELECT names multiple columns
    (e.g. ``select(Program, Tenant.name)``) so callers get raw Row tuples
    instead of the first column only. Default True matches the common
    single-entity case.
    """
    # COUNT(*) over the same criteria — subquery preserves any ORDER BY /
    # DISTINCT / GROUP BY the caller applied. Cheap enough at admin-list
    # cardinality; if it becomes a bottleneck we can move to a windowed
    # count over the SELECT.
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    total_pages = max(1, (total + params.size - 1) // params.size) if total else 1
    # If the caller landed on a page past the last one (e.g. deleted a row
    # and the URL is stale), snap back to the last page so they see rows
    # instead of an empty table.
    page = min(params.page, total_pages) if total else 1
    offset = (page - 1) * params.size

    result = await session.execute(stmt.limit(params.size).offset(offset))
    if scalars:
        items = list(result.scalars().all())
    else:
        items = list(result.all())

    return PageResult(
        items=items,
        page=page,
        size=params.size,
        total=total,
        total_pages=total_pages,
    )
