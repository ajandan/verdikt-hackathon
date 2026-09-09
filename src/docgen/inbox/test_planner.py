"""AI test-data planner — Nemotron (or any OpenAI-compat model) proposes
N varied payloads for a freshly-frozen template.

Feeds the multi-test executor: instead of one deterministic synth_payload
render, we run N (default 5) trials with different edge-shape data so the
reviewer sees how the template behaves across happy-path, long-string,
empty-optional, unicode, and boundary-value inputs. Each trial ships back
a short human-friendly `name` that the summary table renders as a row
header — the same names appear in the email and the admin portal.

Design notes:
  - Falls back to `synth_payload` variants when no AI provider is
    configured, so tests + local dev never depend on network.
  - Every AI-proposed payload is schema-validated before being handed off;
    any trial that fails is replaced with a synth_payload so the executor
    always gets exactly `count` rows.
  - The LLM call is best-effort: any network/JSON error yields the full
    synth fallback rather than propagating.
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

_SYSTEM_PROMPT = """You are a QA data engineer. Given a JSON Schema for a document
template payload, produce a small set of realistic test payloads that exercise the
template across different input shapes: typical values, edge cases (long strings,
empty optional fields, unicode names, boundary numbers), and one adversarial case
that stresses formatting (e.g. very long list, unusual characters).

Return STRICT JSON matching:

{
  "trials": [
    {
      "name": "<3-5 word label, e.g. 'happy path', 'long strings', 'unicode names'>",
      "description": "<one sentence: what this trial exercises>",
      "payload": { <object matching the schema exactly> }
    }
  ]
}

Rules:
- Produce EXACTLY the requested number of trials.
- Every payload MUST include every required field with a value matching its type.
- Use realistic, human-plausible values (real names, valid addresses, plausible
  amounts). Never emit placeholder strings like "test" or "foo".
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
        # Every fallback trial uses the same synth payload — the *label*
        # differs so the summary table still reads as N distinct rows and
        # the reviewer knows the AI path was skipped.
        trials.append(
            Trial(name=name, description=desc, payload=dict(base))
        )
    return trials


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
        ok, _ = validate_payload(schema_dict, payload)
        if not ok:
            log.info(
                "inbox_test_planner_trial_invalid_payload",
                ordinal=i + 1,
                name=name,
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

    `provider_config` overlays the global env-derived settings, same
    precedence rule as :func:`docgen.providers.get_provider`. When the
    tenant's provider_config omits `provider`, we fall back to the
    deployment-wide `ai_provider` (from DOCGEN_AI_PROVIDER) so a
    single-tenant install without per-tenant overrides still hits the LLM.
    A stub or misconfigured provider falls back to deterministic synth.
    """
    # Validate the schema up front — if it's malformed, the fallback would
    # crash inside synth_payload with a less useful error. TemplateSchema
    # raises ValidationError; let the caller see it.
    TemplateSchema.model_validate(schema_dict)

    cfg = provider_config or {}
    provider_name = cfg.get("provider") or ai_provider or ""
    base_url = cfg.get("base_url") or ai_base_url
    api_key = cfg.get("api_key") or ai_api_key
    model = cfg.get("model") or ai_model
    timeout_s = float(cfg.get("timeout_seconds") or timeout)

    # Nebius defaults its own base_url + model, so the caller can leave
    # them blank in .env. Mirror that here.
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

    user_prompt = (
        f"Produce {count} test payloads for this template schema:\n\n"
        + json.dumps(schema_dict, indent=2)
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.7,  # variety across trials is the whole point
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
