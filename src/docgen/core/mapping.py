"""Caller-profile mapping engine.

Translates a caller's native JSON into the flat {field: value} shape the frozen
template schema expects. Kept intentionally small: JSONPath-lite (dot + [idx])
and a closed set of value transforms - no full JSONPath, no expression language.

Wire format for one mapping entry (target field on the left):
    {"source": "$.a.b"}                  # single-source
    {"source": ["$.first", "$.last"]}    # multi-source (list value)
    {"constant": "EMEA"}                 # ignore source, use literal
optionally combined with:
    "transform": "<name>[:arg]"
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def resolve_path(data: Any, path: str) -> Any:
    """Resolve a JSONPath-lite expression against `data`.

    Grammar: $ ( .name | [N] )*
    Returns None on any missing segment or type mismatch; never raises for
    missing keys (only for malformed paths).
    """
    if not path or not path.startswith("$"):
        raise ValueError(f"path must start with '$': {path!r}")
    cur: Any = data
    for m in _PATH_TOKEN.finditer(path[1:]):
        if cur is None:
            return None
        name, idx = m.group(1), m.group(2)
        if name is not None:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(name)
        else:
            i = int(idx)
            if not isinstance(cur, list) or i < 0 or i >= len(cur):
                return None
            cur = cur[i]
    return cur


_DATE_FMT_MAP = {
    "YYYY": "%Y",
    "MM": "%m",
    "DD": "%d",
    "Mon": "%b",
    "HH": "%H",
    "mm": "%M",
}
_DATE_FMT_RE = re.compile("|".join(sorted(_DATE_FMT_MAP.keys(), key=len, reverse=True)))


def _to_strftime(pattern: str) -> str:
    return _DATE_FMT_RE.sub(lambda m: _DATE_FMT_MAP[m.group(0)], pattern)


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value)
    if len(s) > 10:
        return datetime.fromisoformat(s).date()
    return date.fromisoformat(s)


def apply_transform(value: Any, transform: str | None) -> Any:
    """Apply a single named transform. Unknown name -> ValueError."""
    if not transform:
        return value
    name, _, arg = transform.partition(":")
    if name == "number":
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        try:
            f = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"transform 'number' cannot parse {value!r}") from e
        return int(f) if f.is_integer() else f
    if name == "date":
        if value is None or value == "":
            return None
        try:
            dt = _parse_date(value)
        except ValueError as e:
            raise ValueError(f"transform 'date' cannot parse {value!r}") from e
        return dt.strftime(_to_strftime(arg or "YYYY-MM-DD"))
    if name == "join":
        if value is None:
            return ""
        if not isinstance(value, list):
            return str(value)
        return arg.join(str(v) for v in value if v is not None and v != "")
    if name == "split":
        m = re.match(r"^(.*)\[(\d+)\]$", arg)
        if not m:
            raise ValueError(f"transform 'split' needs 'sep[i]' arg, got {arg!r}")
        sep, i = m.group(1), int(m.group(2))
        if value is None:
            return None
        parts = str(value).split(sep)
        return parts[i] if 0 <= i < len(parts) else None
    if name == "upper":
        return str(value).upper() if value is not None else None
    if name == "lower":
        return str(value).lower() if value is not None else None
    if name == "default":
        return arg if value is None or value == "" else value
    raise ValueError(f"unknown transform: {name!r}")


class MappingError(Exception):
    """Raised when applying a mapping fails for one or more targets.

    `errors` is a list of {"target", "error", "source"} dicts, suitable for
    returning as-is in a 422 response body.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} mapping error(s)")


def apply_mapping(
    mapping: dict[str, dict[str, Any]],
    data: dict[str, Any],
    required: set[str] | None = None,
) -> dict[str, Any]:
    """Translate caller-native `data` into {target_field: value} using `mapping`.

    Non-required targets that resolve to None/"" are omitted from the output.
    Required targets that resolve to None/"" produce a MappingError.
    """
    required = required or set()
    out: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for target, spec in mapping.items():
        try:
            if "constant" in spec:
                value: Any = spec["constant"]
            else:
                src = spec.get("source")
                if isinstance(src, list):
                    value = [resolve_path(data, s) for s in src]
                elif isinstance(src, str):
                    value = resolve_path(data, src)
                else:
                    value = None
            value = apply_transform(value, spec.get("transform"))
        except ValueError as e:
            errors.append(
                {"target": target, "error": str(e), "source": spec.get("source")}
            )
            continue

        if value is None or value == "":
            if target in required:
                errors.append(
                    {
                        "target": target,
                        "error": "required source resolved to empty",
                        "source": spec.get("source"),
                    }
                )
            continue
        out[target] = value

    if errors:
        raise MappingError(errors)
    return out


def suggest_mapping(
    template_fields: list[dict[str, Any]],
    sample: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Best-effort auto-suggest: match template field names to leaf paths in `sample`.

    Prefers exact case-insensitive matches, then substring, then shortest path.
    Returns partial mapping (unmatched fields omitted). The wizard shows this as
    the starting point for the human to confirm or override.
    """
    leaves = list(walk_leaves(sample, "$"))
    used: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for field in template_fields:
        fname = field["name"].lower().replace("_", "").replace("-", "")
        best: tuple[int, int, str] | None = None
        for path, _leaf in leaves:
            if path in used:
                continue
            last = path.rsplit(".", 1)[-1].rstrip("]").split("[")[0].lower()
            last_norm = last.replace("_", "").replace("-", "")
            if last_norm == fname:
                score = 0
            elif fname in last_norm or last_norm in fname:
                score = 1
            else:
                continue
            key = (score, len(path), path)
            if best is None or key < best:
                best = key
        if best is not None:
            out[field["name"]] = {"source": best[2]}
            used.add(best[2])
    return out


def walk_leaves(node: Any, path: str = "$") -> Any:
    """Yield (jsonpath, value) tuples for every leaf in `node`.

    Used by the wizard UI to show the caller's sample JSON as a picker of
    JSONPath expressions the human can bind to template fields.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk_leaves(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_leaves(v, f"{path}[{i}]")
    else:
        yield (path, node)
