"""Unit tests for the coordinate primitives introduced under ADR-0001 Option 2.

`docgen.designtime.geometry.Rect` replaces `pymupdf.Rect` construction sites in
`pdf_field_detector.py` so subsequent steps can swap the pymupdf backend without
churning geometry math.
"""
from docgen.designtime.geometry import Rect


def test_basic_construction_and_accessors() -> None:
    r = Rect(1.0, 2.0, 5.0, 7.0)
    assert r.x0 == 1.0
    assert r.y0 == 2.0
    assert r.x1 == 5.0
    assert r.y1 == 7.0
    assert r.width == 4.0
    assert r.height == 5.0


def test_construction_from_bbox_tuple() -> None:
    bbox = (10.0, 20.0, 30.0, 40.0)
    r = Rect(*bbox)
    assert (r.x0, r.y0, r.x1, r.y1) == bbox


def test_intersects_overlap() -> None:
    a = Rect(0, 0, 10, 10)
    b = Rect(5, 5, 15, 15)
    assert a.intersects(b)
    assert b.intersects(a)


def test_intersects_disjoint() -> None:
    a = Rect(0, 0, 10, 10)
    b = Rect(20, 20, 30, 30)
    assert not a.intersects(b)


def test_intersects_edge_touch_is_not_overlap() -> None:
    """Matches pymupdf's `.intersects()` semantics: non-empty area required."""
    a = Rect(0, 0, 10, 10)
    b = Rect(10, 0, 20, 10)  # shares the right edge of `a`
    assert not a.intersects(b)


def test_intersects_containment() -> None:
    outer = Rect(0, 0, 100, 100)
    inner = Rect(25, 25, 75, 75)
    assert outer.intersects(inner)
    assert inner.intersects(outer)


def test_frozen_dataclass_is_hashable() -> None:
    r = Rect(1, 2, 3, 4)
    assert {r} == {Rect(1, 2, 3, 4)}
