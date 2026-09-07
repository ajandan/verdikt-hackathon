from __future__ import annotations

import pytest

from docgen.core.mapping import (
    MappingError,
    apply_mapping,
    apply_transform,
    resolve_path,
    suggest_mapping,
)


class TestResolvePath:
    def test_simple_field(self) -> None:
        assert resolve_path({"a": 1}, "$.a") == 1

    def test_nested(self) -> None:
        assert resolve_path({"a": {"b": {"c": "x"}}}, "$.a.b.c") == "x"

    def test_array_index(self) -> None:
        assert resolve_path({"xs": [10, 20, 30]}, "$.xs[1]") == 20

    def test_mixed(self) -> None:
        assert resolve_path({"a": [{"b": "hit"}]}, "$.a[0].b") == "hit"

    def test_missing_returns_none(self) -> None:
        assert resolve_path({"a": 1}, "$.b") is None
        assert resolve_path({}, "$.a.b.c") is None
        assert resolve_path({"xs": [1]}, "$.xs[5]") is None

    def test_type_mismatch_returns_none(self) -> None:
        assert resolve_path({"a": 1}, "$.a.b") is None
        assert resolve_path({"a": {"b": 1}}, "$.a[0]") is None

    def test_invalid_path_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_path({}, "a.b")


class TestTransforms:
    def test_number_from_string(self) -> None:
        assert apply_transform("42", "number") == 42
        assert apply_transform("3.14", "number") == 3.14

    def test_number_pass_through(self) -> None:
        assert apply_transform(42, "number") == 42
        assert apply_transform(3.14, "number") == 3.14

    def test_number_empty_yields_none(self) -> None:
        assert apply_transform(None, "number") is None
        assert apply_transform("", "number") is None

    def test_number_bad_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_transform("nope", "number")

    def test_date_reformat(self) -> None:
        assert apply_transform("2026-08-21", "date:MM/DD/YYYY") == "08/21/2026"
        assert apply_transform("2026-08-21", "date:DD Mon YYYY") == "21 Aug 2026"

    def test_date_default_fmt(self) -> None:
        assert apply_transform("2026-08-21T10:30:00", "date") == "2026-08-21"

    def test_join(self) -> None:
        assert apply_transform(["a", "b", "c"], "join:, ") == "a, b, c"

    def test_join_skips_empty(self) -> None:
        assert apply_transform(["a", None, "", "c"], "join:-") == "a-c"

    def test_split(self) -> None:
        assert apply_transform("first last", "split: [1]") == "last"
        assert apply_transform("a,b,c", "split:,[2]") == "c"

    def test_split_out_of_range(self) -> None:
        assert apply_transform("a,b", "split:,[5]") is None

    def test_upper_lower(self) -> None:
        assert apply_transform("Hi", "upper") == "HI"
        assert apply_transform("HI", "lower") == "hi"

    def test_default_fills_missing(self) -> None:
        assert apply_transform(None, "default:EMEA") == "EMEA"
        assert apply_transform("", "default:EMEA") == "EMEA"
        assert apply_transform("APAC", "default:EMEA") == "APAC"

    def test_none_transform_is_identity(self) -> None:
        assert apply_transform("x", None) == "x"
        assert apply_transform("x", "") == "x"

    def test_unknown_transform_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_transform("x", "nope")


class TestApplyMapping:
    def test_simple(self) -> None:
        mapping = {"name": {"source": "$.account.name"}}
        assert apply_mapping(mapping, {"account": {"name": "Acme"}}) == {"name": "Acme"}

    def test_transform(self) -> None:
        mapping = {"amount": {"source": "$.opp.value", "transform": "number"}}
        assert apply_mapping(mapping, {"opp": {"value": "50000"}}) == {"amount": 50000}

    def test_multi_source_join(self) -> None:
        mapping = {"full": {"source": ["$.first", "$.last"], "transform": "join: "}}
        got = apply_mapping(mapping, {"first": "Ada", "last": "Lovelace"})
        assert got == {"full": "Ada Lovelace"}

    def test_constant(self) -> None:
        assert apply_mapping({"region": {"constant": "EMEA"}}, {}) == {"region": "EMEA"}

    def test_missing_optional_omitted(self) -> None:
        assert apply_mapping({"note": {"source": "$.notes"}}, {}) == {}

    def test_missing_required_raises(self) -> None:
        mapping = {"name": {"source": "$.who"}}
        with pytest.raises(MappingError) as exc_info:
            apply_mapping(mapping, {}, required={"name"})
        assert exc_info.value.errors[0]["target"] == "name"

    def test_transform_error_captured(self) -> None:
        mapping = {"amount": {"source": "$.v", "transform": "number"}}
        with pytest.raises(MappingError) as exc_info:
            apply_mapping(mapping, {"v": "not-a-number"})
        assert exc_info.value.errors[0]["target"] == "amount"
        assert "cannot parse" in exc_info.value.errors[0]["error"]

    def test_default_transform_saves_required(self) -> None:
        mapping = {"region": {"source": "$.r", "transform": "default:EMEA"}}
        assert apply_mapping(mapping, {}, required={"region"}) == {"region": "EMEA"}


class TestSuggestMapping:
    def test_exact_name_match(self) -> None:
        fields = [{"name": "customer_name"}, {"name": "amount"}]
        sample = {"customer_name": "Acme", "amount": 100}
        got = suggest_mapping(fields, sample)
        assert got == {
            "customer_name": {"source": "$.customer_name"},
            "amount": {"source": "$.amount"},
        }

    def test_nested_leaf_match(self) -> None:
        fields = [{"name": "name"}]
        sample = {"account": {"name": "Acme"}}
        assert suggest_mapping(fields, sample) == {"name": {"source": "$.account.name"}}

    def test_case_and_underscore_insensitive(self) -> None:
        fields = [{"name": "customer_name"}]
        sample = {"CustomerName": "Acme"}
        assert suggest_mapping(fields, sample) == {
            "customer_name": {"source": "$.CustomerName"}
        }

    def test_unmatched_omitted(self) -> None:
        fields = [{"name": "customer_name"}, {"name": "region"}]
        sample = {"customer_name": "Acme"}
        assert suggest_mapping(fields, sample) == {
            "customer_name": {"source": "$.customer_name"}
        }
