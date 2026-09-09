"""AI test-data planner — Nemotron (or any OpenAI-compat model) proposes
N varied payloads for a freshly-frozen template.

Format hints come from `schema["fields"][i]["format"]`, which was
populated once at freeze time by :mod:`docgen.designtime.format_infer`
from the blank template's widget captions. Runtime pays for zero PDF
parsing and zero format-inference calls — one Nebius call per inbox
event, not two.

Falls back to `synth_payload` variants when no AI provider is
configured, so tests + local dev never depend on network. Every
AI-proposed payload is schema-validated before being handed off; any
trial that fails is replaced with a synth_payload so the executor always
gets exactly `count` rows.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from docgen.core.schema_infer import TemplateSchema, validate_payload
from docgen.inbox.synth import synth_payload

log = structlog.get_logger(__name__)

DEFAULT_TRIAL_COUNT = 5


_PAYLOAD_SYSTEM_PROMPT = """You are a QA data engineer. You will receive:
  (a) the JSON schema of a template's payload,
  (b) a per-field FORMAT SPEC describing what each field expects
      (patterns, max lengths, radio group ids). The spec came from
      reading the printed captions on the blank template and is authoritative.

Produce EXACTLY these 5 trials, in this order, with these exact `name` labels:

  1. "happy path" — realistic values across every field; for each radio
     group pick the most typical option (e.g. sex=male).
  2. "alternate radio choices" — SAME realistic values as trial 1 for
     every non-boolean field, but for every radio group flip to a
     DIFFERENT true-selection than trial 1. Hunts for label->widget
     swaps in the AcroForm mapping.
  3. "unicode names" — realistic values with non-ASCII characters —
     Chinese/Japanese/Korean names, Devanagari, Cyrillic, or Arabic
     are all fine. The executor skips rendering when it detects
     characters outside the Latin-1 range and reports the trial as a
     passed skip; this trial is the probe that fires that skip.
  4. "boundary numbers" — realistic strings but push every numeric-like
     field (age, count, amount, dosage strength) to the edge of its
     plausible range.
  5. "adversarial formatting" — stress test with special characters in
     free-text fields (apostrophes in names like "O'Brien", hyphens,
     periods in initials), unusual but valid formats in phone/date
     fields. Do NOT use characters outside Latin-1 Supplement.

Return STRICT JSON:

{
  "trials": [
    {
      "name": "<exactly one of the 5 labels above>",
      "description": "<one sentence: what this trial exercises>",
      "payload": { <object matching the schema exactly> }
    }
  ]
}

Rules:
- Produce EXACTLY 5 trials in the order and with the names listed above.
- Every payload MUST include EVERY field from the schema, using the
  exact field names (case-sensitive). Missing fields cause validation
  failure and the trial is silently discarded.
- Every scalar value MUST honor its FORMAT SPEC — respect max_length,
  match the pattern (e.g. "5-digit US ZIP" -> "10001", not "New York").
- For boolean fields with a non-null `group`: exactly ONE field per
  group may be `true`; every other field in the same group MUST be
  `false`. This models mutually-exclusive checkbox / radio choices.
- Use realistic values. Never emit "test", "foo", "example".
- Do NOT include any text outside the JSON object.
"""


@dataclass
class Trial:
    """One planned test payload."""

    name: str
    description: str
    payload: dict[str, Any]


def _fallback_trials(schema_dict: dict, count: int) -> list[Trial]:
    """Deterministic variants when the AI path is unavailable."""
    base = synth_payload(schema_dict)
    names = [
        ("happy path", "typical values across all fields"),
        ("minimal values", "shortest valid values for each field"),
        ("long strings", "extended text values to test line-wrap and truncation"),
        ("unicode names", "non-ASCII characters and accented letters"),
        ("boundary values", "large numbers and long lists to test overflow"),
    ]
    trials: list[Trial] = []
    for i in range(count):
        name, desc = names[i % len(names)]
        trials.append(
            Trial(name=name, description=desc, payload=dict(base))
        )
    return trials


def _extract_persisted_formats(schema_dict: dict) -> dict[str, dict[str, Any]]:
    """Pull the `format` sub-dict off each field entry, keyed by field name.

    Populated by :mod:`docgen.designtime.format_infer` at freeze time.
    Absent for docx / older templates / freezes with no AI provider.
    """
    out: dict[str, dict[str, Any]] = {}
    for f in schema_dict.get("fields") or []:
        name = f.get("name")
        spec = f.get("format")
        if isinstance(name, str) and isinstance(spec, dict):
            out[name] = spec
    return out


def _coerce_llm_trials(
    raw: dict, schema_dict: dict, count: int
) -> list[Trial]:
    """Validate + normalize an LLM response into exactly `count` trials.

    Any trial whose payload doesn't validate against the schema is
    replaced with a synth fallback so the executor still runs the full
    matrix. Ordering is preserved so labels line up with the LLM's
    intent.
    """
    fallback_payload = synth_payload(schema_dict)
    llm_trials = raw.get("trials") or []
    out: list[Trial] = []
    for i in range(count):
        entry = llm_trials[i] if i < len(llm_trials) else {}
        name = str(entry.get("name") or "").strip() or f"Test {i + 1}"
        desc = str(entry.get("description") or "").strip()
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = dict(fallback_payload)
        ok, errors = validate_payload(schema_dict, payload)
        if not ok:
            log.info(
                "inbox_test_planner_trial_invalid_payload",
                ordinal=i + 1,
                name=name,
                errors=errors[:10],
                missing=sorted(
                    {f["name"] for f in (schema_dict.get("fields") or [])}
                    - set(payload.keys())
                )[:20],
            )
            payload = dict(fallback_payload)
            desc = desc or "payload rebuilt from synth fallback"
        out.append(Trial(name=name[:120], description=desc, payload=payload))
    return out


def plan_trials(
    schema_dict: dict,
    *,
    provider_config: dict[str, Any] | None,
    ai_base_url: str | None,
    ai_api_key: str | None,
    ai_model: str,
    timeout: float,
    ai_provider: str = "",
    count: int = DEFAULT_TRIAL_COUNT,
) -> list[Trial]:
    """Ask the design-time LLM for `count` varied payloads.

    Format hints are read off `schema_dict["fields"][i]["format"]`,
    which was populated once at freeze time. No PDF parsing, no
    format-inference call — runtime pays for one chat/completions call
    per inbox event.
    """
    TemplateSchema.model_validate(schema_dict)

    cfg = provider_config or {}
    provider_name = cfg.get("provider") or ai_provider or ""
    base_url = cfg.get("base_url") or ai_base_url
    api_key = cfg.get("api_key") or ai_api_key
    model = cfg.get("model") or ai_model
    timeout_s = float(cfg.get("timeout_seconds") or timeout)

    if provider_name == "nebius":
        from docgen.providers.nebius import DEFAULT_BASE_URL, DEFAULT_MODEL

        base_url = base_url or DEFAULT_BASE_URL
        model = model or DEFAULT_MODEL

    if not api_key or not base_url or provider_name not in ("nebius", "openai_compat"):
        log.info(
            "inbox_test_planner_using_fallback",
            reason="no_ai_provider",
            provider=provider_name or "stub",
        )
        return _fallback_trials(schema_dict, count)

    formats = _extract_persisted_formats(schema_dict)

    user_prompt = (
        f"Produce {count} test payloads for this template schema:\n\n"
        + json.dumps(schema_dict, indent=2)
        + "\n\nPer-field FORMAT SPEC (obey these — max_length, pattern, and radio groups):\n"
        + (json.dumps(formats, indent=2) if formats else "(none available — infer from schema)")
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PAYLOAD_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.7,  # variety across trials is the whole point
        # vLLM Nemotron reasoning-mode footgun — correct spelling is
        # `enable_thinking`. Without it the model burns the output-token
        # budget on a <think> preamble and returns empty content.
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
            "inbox_test_planner_llm_failed",
            error=f"{type(e).__name__}: {e}",
            provider=provider_name,
            model=model,
        )
        return _fallback_trials(schema_dict, count)

    return _coerce_llm_trials(parsed, schema_dict, count)
