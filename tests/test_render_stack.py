from __future__ import annotations

from docgen.core.render_stack import (
    render_stack_hash,
    render_stack_info,
)


def test_render_stack_info_has_expected_shape() -> None:
    info = render_stack_info()
    assert "libreoffice" in info
    assert "python" in info
    assert "packages" in info
    assert set(info["packages"]) >= {"docxtpl", "jinja2", "python-docx", "lxml"}
    for name, ver in info["packages"].items():
        assert isinstance(ver, str) and ver, f"missing version for {name}"


def test_render_stack_hash_is_stable_16_hex() -> None:
    h1 = render_stack_hash()
    h2 = render_stack_hash()
    assert h1 == h2
    assert len(h1) == 16
    int(h1, 16)  # is valid hex


def test_render_stack_hash_changes_when_info_changes(monkeypatch) -> None:
    import docgen.core.render_stack as rs

    rs.render_stack_info.cache_clear()
    rs.render_stack_hash.cache_clear()
    baseline = rs.render_stack_hash()

    rs.render_stack_info.cache_clear()
    rs.render_stack_hash.cache_clear()
    monkeypatch.setattr(rs, "_libreoffice_version", lambda: "different-version-x")
    changed = rs.render_stack_hash()

    assert baseline != changed
