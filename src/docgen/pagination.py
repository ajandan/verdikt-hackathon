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

from sqlalchemy import and_, or_

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


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
