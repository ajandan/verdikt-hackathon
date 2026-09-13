"""Validate + normalize a JSON intake payload against a program's schema.

The JSON intake channel is the "web" lane of the tri-channel strategy: an
upstream system (CRM, call center app, MCP caller) already has the
applicant's data as structured fields, so it can skip the OCR+VLM extract
stage entirely and hand us a payload directly.

Two payload shapes are accepted per key:

- **Slot name** — the extraction slot's ``name`` from
  ``programs.extraction_schema``. This is what the extract stage would
  produce internally, so it's the "native" shape.
- **Source field** — the enrollment template field name
  (``slot.source_field``). Preferred for external callers because those
  are the labels they already see on the paper form they're digitizing.

A single payload can mix both. On collision (both a slot-name key and its
source-field key are present), the slot-name key wins — it's the more
specific identifier.

Every value is coerced to the slot's declared type (string, int, float,
date, bool, enum). Coercion failures don't abort the whole payload:
they're collected into ``errors`` so the caller can return a single 400
response that lists everything that needs fixing. Unknown keys are
returned in ``unknown_keys`` — surfaced to the caller so a typo in a field
name doesn't silently drop the value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class IntakeValidationError:
    """A single validation failure — one per bad field."""

    field: str
    message: str
    received: Any = None


@dataclass
class NormalizedIntake:
    """Outcome of validating a JSON payload against a program schema.

    ``normalized`` is keyed by slot ``name`` (never ``source_field``) so
    downstream code doesn't need to know which alias the caller used.
    Empty when validation fails — but ``errors`` is authoritative; check it
    first.
    """

    normalized: dict[str, Any] = field(default_factory=dict)
    errors: list[IntakeValidationError] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)


_TRUTHY = {"true", "yes", "y", "1", "on"}
_FALSY = {"false", "no", "n", "0", "off"}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUTHY:
            return True
        if s in _FALSY:
            return False
    raise ValueError(f"expected boolean, got {value!r}")


def _coerce_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        # ISO-8601 date or datetime — accept both since JSON callers vary.
        s = value.strip()
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError as e:
            raise ValueError(f"expected ISO-8601 date, got {value!r}") from e
    raise ValueError(f"expected ISO-8601 date, got {value!r}")


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        # bool is a subclass of int in Python — reject explicitly so a
        # caller who sends ``true`` for household_size gets a clear error.
        raise ValueError(f"expected integer, got boolean {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError as e:
            raise ValueError(f"expected integer, got {value!r}") from e
    raise ValueError(f"expected integer, got {value!r}")


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"expected number, got boolean {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError as e:
            raise ValueError(f"expected number, got {value!r}") from e
    raise ValueError(f"expected number, got {value!r}")


def _coerce_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise ValueError(f"expected string, got {value!r}")


def _coerce_value(value: Any, slot: dict[str, Any]) -> Any:
    """Coerce ``value`` to the slot's declared type; raise ValueError on failure.

    ``None`` passes through so callers can send explicit nulls to signal
    "we know this field is missing" — the pipeline treats those as
    missing_info candidates.
    """
    if value is None:
        return None

    declared = (slot.get("type") or "string").strip().lower()
    if declared == "boolean" or declared == "bool":
        return _coerce_bool(value)
    if declared == "date":
        return _coerce_date(value)
    if declared == "integer" or declared == "int":
        return _coerce_int(value)
    if declared in ("number", "float", "decimal"):
        return _coerce_float(value)
    if declared == "enum":
        coerced = _coerce_string(value)
        allowed = slot.get("enum_values") or []
        if allowed and coerced not in allowed:
            raise ValueError(
                f"expected one of {allowed!r}, got {coerced!r}"
            )
        return coerced
    # Default: string. Any explicit unknown type falls through here too —
    # a permissive default is friendlier for demo programs whose schemas
    # were sketched before the type vocabulary was finalized.
    return _coerce_string(value)


def _build_alias_index(
    schema: list[dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Map every legal key (slot ``name`` or ``source_field``) → slot dict,
    plus a secondary map from source_field → canonical slot name.

    The secondary map lets the normalizer emit under the canonical name
    regardless of which alias the caller sent.
    """
    by_alias: dict[str, dict[str, Any]] = {}
    source_to_name: dict[str, str] = {}
    for slot in schema or []:
        name = (slot.get("name") or "").strip()
        if not name:
            continue
        by_alias[name] = slot
        source = (slot.get("source_field") or "").strip()
        if source:
            source_to_name[source] = name
            by_alias.setdefault(source, slot)
    return by_alias, source_to_name


def validate_json_intake(
    payload: dict[str, Any] | None,
    schema: list[dict[str, Any]] | None,
) -> NormalizedIntake:
    """Validate ``payload`` against ``schema``; return a normalized dict + errors.

    ``payload`` keys may be either the slot ``name`` or ``source_field``.
    The returned ``normalized`` dict is always keyed by slot ``name`` so
    downstream consumers (Intake pydantic model + reason stage) see a
    single stable shape.

    ``schema`` is the ``programs.extraction_schema`` list as stored in the
    DB. ``None`` or empty means "no schema declared" — in that case every
    key is passed through as-is with no type coercion, and no
    ``missing_required`` is reported.
    """
    if not isinstance(payload, dict):
        return NormalizedIntake(
            errors=[
                IntakeValidationError(
                    field="(root)",
                    message="payload must be a JSON object",
                    received=type(payload).__name__,
                )
            ]
        )

    result = NormalizedIntake()

    # No schema: pass every key/value through untouched. The orchestrator
    # will still filter to Intake.model_fields and drop unknown keys, so
    # this is safe.
    if not schema:
        result.normalized = dict(payload)
        return result

    by_alias, source_to_name = _build_alias_index(schema)

    for key, value in payload.items():
        slot = by_alias.get(key)
        if slot is None:
            result.unknown_keys.append(key)
            continue

        canonical = slot.get("name") or source_to_name.get(key) or key
        # Collision policy: if this key is a source_field alias but the
        # payload also carries the canonical name, the canonical wins.
        if key != canonical and canonical in payload:
            continue
        try:
            coerced = _coerce_value(value, slot)
        except ValueError as e:
            result.errors.append(
                IntakeValidationError(
                    field=canonical,
                    message=str(e),
                    received=value,
                )
            )
            continue
        result.normalized[canonical] = coerced

    # Required-field check runs after normalization so missing keys report
    # against the canonical slot name — not whichever alias the caller
    # might have used.
    for slot in schema:
        name = (slot.get("name") or "").strip()
        if not name or not slot.get("required"):
            continue
        v = result.normalized.get(name)
        # None counts as missing here — that's the whole point of a
        # required flag. Empty string is also treated as missing so the
        # caller doesn't quietly hand us "" and think they submitted a
        # value.
        if v is None or (isinstance(v, str) and not v.strip()):
            result.missing_required.append(name)

    return result


__all__ = [
    "IntakeValidationError",
    "NormalizedIntake",
    "validate_json_intake",
]
