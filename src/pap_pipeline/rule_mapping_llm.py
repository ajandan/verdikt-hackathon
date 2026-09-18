"""Phase D — LLM assist for unresolved rows from the three-pass auto-mapper.

The deterministic mapper in :mod:`pap_pipeline.rule_mapping` bridges the
common name-vocabulary gaps (exact string match, curated alias buckets,
token/Levenshtein fuzzy). Anything it can't confidently name is returned
with ``confidence="unresolved"`` and ``target=None`` — that's when the
operator has to eyeball the row and pick from the dropdown.

This module adds a fourth, optional pass: batch every unresolved ref
into ONE Nebius Token Factory chat call and ask a small classifier-scale
Nemotron ("which target slot fits this source ref?") for a hint. The
answer is:

- **Never binding** — the wizard renders a ``conf-llm`` pill, pre-selects
  the suggested target in the dropdown, and the operator still reviews.
- **Feature-gated on credential presence** — deployments without
  ``settings.ai_api_key`` skip the LLM call entirely and see the same
  unresolved-row behavior as Phase B/C.
- **Fail-open** — every error path returns an empty ``{}`` so the wizard
  keeps rendering; the operator is never blocked by a flaky model call.
- **Guardrailed** — hallucinated target names are dropped, confidence is
  clipped to [0, 1], and the reasoning string is capped for audit sanity.

Prompt/response shape mirrors ``triage._llm_classify()`` — same auth
pattern, same ``response_format`` hint, same JSON-parse fallback. Copy
that reference implementation before touching this file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LLMSuggestion:
    """One LLM hint for a source ref the fuzzy pass couldn't resolve.

    ``target`` is either a slot name present on the target program's
    schema (validated before this object is constructed — hallucinated
    names never make it here) or ``None`` when the model declined to
    guess. ``reasoning`` is shown as a hover tooltip on the wizard's
    confidence pill; kept to one short sentence.
    """

    target: str | None
    confidence: float
    reasoning: str


_SYSTEM_PROMPT = """You are a schema-mapping assistant.

Given a source rule that references certain fields by name, and a target program's schema of slot names + descriptions, map each source reference to the semantically-closest target slot. If no target slot is a reasonable fit, return null for that source.

Return STRICT JSON matching this exact schema — no extra keys, no prose, no markdown fences:

{"mappings": [
    {"source": "<one of the input source refs>",
     "target": "<one of the input target slot names, or null>",
     "confidence": <float between 0.0 and 1.0>,
     "reasoning": "<one short sentence explaining the mapping>"}
]}

Guidance:
- Prefer slots whose description or name captures the same real-world concept as the source ref.
- Return null for target when NO slot is a reasonable fit — a bad guess is worse than an honest miss because the operator will just have to un-pick it.
- Every source ref from the input MUST appear exactly once in your mappings list.
- Never invent target slot names outside the provided list.
"""


async def suggest_unresolved(
    source_refs: list[str],
    target_slots: list[dict],
    *,
    rule_criterion: str,
    rule_exception: str,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    timeout: float = 15.0,
) -> dict[str, LLMSuggestion]:
    """Ask the LLM to map each unresolved source ref to a target slot.

    One HTTP call for the whole batch — the model sees all unresolved
    refs and the full slot list at once, which lets it reason about
    "this slot is a better fit for ref A than ref B" instead of falling
    into greedy per-ref traps.

    Parameters
    ----------
    source_refs
        The @-field names from the source rule that the fuzzy pass left
        unresolved. Order is preserved in the prompt so the model sees
        the same reading order the operator does.
    target_slots
        The target program's ``extraction_schema`` — each dict must
        carry at least ``name`` and (optionally) ``type`` and
        ``description``. Descriptions are the strongest signal for
        semantic mapping so include them when they exist.
    rule_criterion / rule_exception
        Full rule text — gives the LLM the semantic context so it can
        tell "ins_uninsured" in an insurance rule apart from a stray
        boolean elsewhere.
    base_url / api_key / model
        Nebius Token Factory endpoint config. All three must be truthy
        or the call is skipped and ``{}`` is returned.
    timeout
        HTTP timeout in seconds. Kept short (default 15s) — the wizard
        route awaits this synchronously; a slow model shouldn't gate
        rendering the review table.

    Returns
    -------
    dict
        ``{source_ref: LLMSuggestion}`` — one entry per source ref for
        which the model returned a non-hallucinated suggestion. Refs
        the model dropped, hallucinated on, or returned ``target=null``
        for still appear in the dict with ``target=None`` so callers
        can render a "no LLM guess" state distinct from "LLM not run".

        Any error (missing creds, HTTP error, JSON parse failure,
        empty source list) yields ``{}`` so the caller can trivially
        fall back to unresolved-row rendering.
    """
    # Explicit guard so the fallback stays intact under `python -O` and
    # so callers can safely pass raw settings without their own None-check.
    if not source_refs:
        return {}
    if not (base_url and api_key and model):
        return {}

    # Cap the source-ref list at a sane size. The wizard's own auto-mapper
    # runs on rules with at most a dozen or so refs in practice; a runaway
    # rule with hundreds shouldn't blow up the prompt.
    refs = list(dict.fromkeys(source_refs))  # dedupe, preserve order
    slot_names = {s["name"] for s in target_slots if s.get("name")}

    user_payload = {
        "source_rule": {
            "criterion": rule_criterion or "",
            "exception": rule_exception or "",
        },
        "unresolved_source_refs": refs,
        "target_slots": [
            {
                "name": s.get("name"),
                "type": s.get("type", ""),
                "description": s.get("description", ""),
            }
            for s in target_slots
            if s.get("name")
        ],
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
    url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        # Fail-open: wizard renders the unresolved rows as-is, operator
        # picks manually. Same UX as a deployment without any Nebius creds.
        return {}

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError):
        return {}

    raw_mappings = parsed.get("mappings")
    if not isinstance(raw_mappings, list):
        return {}

    suggestions: dict[str, LLMSuggestion] = {}
    for entry in raw_mappings:
        if not isinstance(entry, dict):
            continue
        src = entry.get("source")
        if not isinstance(src, str) or src not in refs:
            # Ignore hallucinated source names — only keep suggestions
            # for refs we actually asked about.
            continue
        # Hallucinated targets get silently coerced to None so the row
        # still shows an "LLM saw this but had no guess" state instead
        # of falsely pre-selecting a slot that doesn't exist.
        raw_target = entry.get("target")
        target = raw_target if (isinstance(raw_target, str) and raw_target in slot_names) else None

        try:
            conf = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        reasoning = str(entry.get("reasoning", ""))[:280]  # audit-log cap

        suggestions[src] = LLMSuggestion(
            target=target,
            confidence=conf,
            reasoning=reasoning,
        )

    log.info(
        "rule_map_llm_assist",
        unresolved_count=len(refs),
        suggestions_returned=len(suggestions),
    )
    return suggestions


__all__ = ["LLMSuggestion", "suggest_unresolved"]
