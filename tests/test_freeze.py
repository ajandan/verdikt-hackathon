from __future__ import annotations

from docgen.designtime.freeze import _orphan_variables


def test_orphan_variables_empty_when_all_covered() -> None:
    schema = {"fields": [{"name": "a"}, {"name": "b"}]}
    assert _orphan_variables(["a", "b"], schema) == set()


def test_orphan_variables_reports_missing() -> None:
    schema = {"fields": [{"name": "a"}]}
    assert _orphan_variables(["a", "b", "c"], schema) == {"b", "c"}


def test_orphan_variables_ignores_extra_schema_fields() -> None:
    schema = {"fields": [{"name": "a"}, {"name": "unused"}]}
    assert _orphan_variables(["a"], schema) == set()


def test_orphan_variables_handles_empty_schema() -> None:
    assert _orphan_variables(["x"], {"fields": []}) == {"x"}
    assert _orphan_variables(["x"], {}) == {"x"}


def test_orphan_variables_ignores_malformed_field_entries() -> None:
    schema = {"fields": [{"name": "a"}, {}, {"name": None}]}
    assert _orphan_variables(["a"], schema) == set()
    assert _orphan_variables(["a", "b"], schema) == {"b"}
