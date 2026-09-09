"""One-shot format inference at freeze time.

For AcroForm PDFs the placement rows already carry every hint the LLM needs:
`label` is the printed caption, `section` groups fields visually, `field_type`
is text/checkbox, and page + rect let the LLM spot radio groups (checkboxes
sharing a section + row). We hand that structured hint list to Nemotron once
per freeze; the resulting per-field spec is persisted inside `TemplateVersion.schema`.

Runtime test-data generation reads the persisted spec instead of re-parsing
the PDF on every inbox event — one design-time call amortized across every
future email.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a document analyst. You will receive:
  (a) the JSON schema of a template's payload,
  (b) a per-widget hint list: for every schema field, the printed caption
      on the form, the widget type (text or checkbox), section heading,
      page number, and normalized rect.

Infer the FORMAT each field expects — what a valid value looks like — by
reading the caption and any inline hints (e.g. "MM/DD/YYYY", "5-digit ZIP",
"MI", "(   )   -    "). Spot mutually-exclusive checkbox groups where
captions are alternatives to each other (e.g. "Yes / No" pairs, "Male /
Female / Other", "Medicare Part A / B / C / D"). Checkboxes in the same
section on the same visual row are almost always one group.

Return STRICT JSON, nothing else:

{
  "formats": {
    "<field_name>": {
      "caption": "<printed caption text>",
      "section": "<section heading this field sits under, verbatim from the hint>",
      "example": "<one plausible value>",
      "pattern": "<short human hint, e.g. 'single uppercase letter', 'MM/DD/YYYY', '5-digit US ZIP'>",
      "max_length": <int or null>,
      "group": "<radio-group id, or null if this field is independent>",
      "group_role": "<what selecting THIS field means, e.g. 'sex_male'>"
    }
  }
}

Rules:
- Include an entry for EVERY field in the schema.
- Use `group` to tie fields together whose captions are printed as
  alternatives on the form. Pick a stable ID per group (e.g. "sex",
  "delivery_location"). Fields in the same group are mutually exclusive:
  exactly one may be `true` per payload.
- Leave `group` null for independent booleans / scalars.
- For text fields, prefer a `max_length` derived from the caption when
  obvious (e.g. "MI" -> 1, "SSN (LAST 4)" -> 4, "5-digit ZIP" -> 5).
- Do NOT include any text outside the JSON object.
"""


def _placement_hint(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": p.get("name"),
        "caption": (p.get("label") or "").strip(),
        "widget_type": p.get("field_type") or "text",
        "section": (p.get("section") or "").strip(),
        "page": p.get("page"),
        "x_pct": p.get("x_pct"),
        "y_pct": p.get("y_pct"),
        "w_pct": p.get("w_pct"),
        "h_pct": p.get("h_pct"),
    }


def infer_formats(
    schema_dict: dict[str, Any],
    placements: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_s: float,
    provider_name: str,
) -> dict[str, dict[str, Any]]:
    """Return a `{field_name: format_spec}` dict, or empty on any failure.

    Soft-fail everywhere: freeze must not break on a network hiccup or a
    reasoning-model empty-content quirk. When this returns {}, downstream
    stays on schema-only prompting.
    """
    if not placements:
        return {}
    # Dedupe placements by field name so radio-group widgets sharing a name
    # collapse to one hint row (matches how the schema was built).
    seen: set[str] = set()
    hints: list[dict[str, Any]] = []
    for p in placements:
        name = p.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        hints.append(_placement_hint(p))

    user_prompt = (
        "Schema (JSON):\n"
        + json.dumps(schema_dict, indent=2, default=str)
        + "\n\nPer-widget hints (visual order):\n"
        + json.dumps(hints, indent=2, default=str)
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        # vLLM Nemotron reasoning-mode footgun — the correct key is
        # `enable_thinking`; the wrong spelling silently no-ops and the
        # model returns empty content.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not content:
            raise ValueError("empty content in LLM response")
        parsed = json.loads(content)
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        log.warning(
            "designtime_format_infer_failed",
            error=f"{type(e).__name__}: {e}",
            provider=provider_name,
            model=model,
        )
        return {}
    formats = parsed.get("formats") or {}
    if not isinstance(formats, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in formats.items():
        if isinstance(name, str) and isinstance(spec, dict):
            out[name] = spec
    return out


def merge_formats_into_schema(
    schema: dict[str, Any],
    formats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return schema with `format` populated on each matching field entry.

    Non-destructive: fields not present in `formats` keep `format=None`.
    """
    if not formats:
        return schema
    fields = schema.get("fields") or []
    for f in fields:
        name = f.get("name")
        spec = formats.get(name) if name else None
        if isinstance(spec, dict):
            f["format"] = spec
    return schema
