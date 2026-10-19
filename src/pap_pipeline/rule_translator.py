"""Design-time translator: free-form rule criterion → structured Formula JSON.

Called from the rule-editor save path (rules_create + rule_update in
src/docgen/ui/routes.py). Takes the human-authored criterion + the
program's schema slot vocabulary + the rule's declared ``fields[]``,
prompts Nemotron with the full grammar spec, and returns a validated
:class:`pap_pipeline.rule_formula.Formula` payload.

Two return channels:

* ``formula`` — validated Formula dict on success; None when the LLM
  can't reach the whole criterion or the response fails validation.
* ``notes`` — free-form list of caveats (unrecognised clauses, dropped
  exception phrases, ambiguous predicates). The editor UI surfaces
  these so the author can revise the criterion before saving.

The "force-author" gate in Chunk 4 uses this pair: save only proceeds
when ``formula is not None`` (LLM produced a complete Formula) AND
either ``notes`` is empty OR the author explicitly acknowledged them.

Design decisions:

* **JSON-mode call** with response_format={"type":"json_object"} so
  the model can't smuggle prose into the reply.
* **Non-reasoning mode** (enable_thinking=False) — the reasoning
  variant of Nemotron burns output tokens on hidden chain-of-thought
  and returns empty content on strict JSON tasks.
* **Fail-open at the API boundary** — HTTP/JSON errors return
  ``(None, ["translator unavailable"])`` so the caller decides
  whether to block the save (Chunk 4 does) or proceed with just
  the text criterion.
* **Fail-closed at the schema boundary** — a formula that doesn't
  round-trip through Formula.model_validate() is not returned. The
  editor surfaces the validation error; the author fixes it (either
  by rewording the criterion or hand-editing the JSON in the editor's
  Formula tab).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from pap_pipeline.rule_formula import Formula


@dataclass(frozen=True)
class TranslatorConfig:
    """Client config for the Nemotron JSON-mode call.

    Kept separate from ReasonClientConfig so the design-time path can
    point at a different (cheaper / faster) model than the runtime
    Reason stage if we ever want to. Defaults share the runtime endpoint.
    """

    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0


@dataclass(frozen=True)
class TranslationResult:
    """Return channel for :func:`translate_criterion_to_formula`."""

    formula: dict[str, Any] | None
    notes: list[str]


_SYSTEM_PROMPT = """You are converting a human-authored eligibility-rule criterion
into a strict structured formula that a deterministic evaluator can
walk without any further natural-language reasoning.

The formula is a JSON tree. Every node has an ``op`` discriminator +
op-specific fields. Only the ops in the grammar below are allowed; do
NOT invent new ops. If the criterion contains a clause you cannot
express with the allowed ops, DROP that clause from the formula and
list it verbatim in the ``notes`` array so the author can revise.

Return STRICT JSON matching this exact schema — no extra keys, no prose:

{
  "formula": { <root Node> } | null,
  "notes":   [ "<verbatim dropped clause>", ... ]
}

Set ``formula`` to null ONLY when you cannot express ANY part of the
criterion in the allowed grammar; otherwise return the best-effort
translation of the covered clauses. Never return partial nonsense —
if you're not sure of an op's semantics, drop the clause instead of
guessing.

Allowed ops:

Composites:
  {"op": "AND", "terms": [<node>, <node>, ...]}
  {"op": "OR",  "terms": [<node>, <node>, ...]}
  {"op": "NOT", "term":  <node>}

Comparisons:
  {"op": "==", "field": "<name>", "value": <literal>}
  {"op": "!=", "field": "<name>", "value": <literal>}
  {"op": "<",  "field": "<name>", "value": <number>}
  {"op": "<=", "field": "<name>", "value": <number>}
  {"op": ">",  "field": "<name>", "value": <number>}
  {"op": ">=", "field": "<name>", "value": <number>}
  {"op": "field_eq_field", "left": "<name>", "right": "<name>"}

Set membership:
  {"op": "in",     "field": "<name>", "values": [<lit>, ...]}
  {"op": "not_in", "field": "<name>", "values": [<lit>, ...]}

Date recency (compared against today):
  {"op": "within_days",   "field": "<name>", "n": <int>}
  {"op": "within_weeks",  "field": "<name>", "n": <int>}
  {"op": "within_months", "field": "<name>", "n": <int>}
  {"op": "within_years",  "field": "<name>", "n": <int>}

Text / pattern:
  {"op": "matches_regex", "field": "<name>", "pattern": "<regex>"}
  {"op": "starts_with",   "field": "<name>", "value": "<substring>"}
  {"op": "ends_with",     "field": "<name>", "value": "<substring>"}
  {"op": "contains",      "field": "<name>", "value": "<substring>"}

Null checks:
  {"op": "is_null",     "field": "<name>"}
  {"op": "is_not_null", "field": "<name>"}

Digit-count check (for identifier shapes like NPI):
  {"op": "digit_count_equals", "field": "<name>", "n": <int>}

Rules of engagement:
- Field names refer to slots that exist in the program's extraction
  schema (list provided in the user message). Do NOT invent new field
  names; if the criterion references a field that isn't in the schema,
  drop that clause and note it.
- Boolean values are literal ``true``/``false``, not strings.
- For "within the last N months/years/days of today" phrases, use the
  matching ``within_*`` op. For arithmetic like "less than 30 days
  from prescription date", express with within_days or drop + note.
- For "must have a valid 10-digit NPI" use ``digit_count_equals`` with
  n=10 (or matches_regex with ^\\d{10}$).
- For enum-like set membership ("status is one of X, Y, Z") use
  ``in`` with the string values.
- Wrap multi-clause criteria in AND / OR as appropriate. If the
  precedence is ambiguous ("A and B or C") drop the clause and note.
- Prose outcome directives ("If X, deny." / "..., approve on the
  renewal track") are OUTSIDE the criterion boolean — do not include
  them in the formula. Just translate the boolean core.
- Exception clauses (rule.exception field, given below) are usually
  edge-case escape hatches. Translate them ONLY if they express a
  concrete boolean condition on named fields; otherwise drop + note.
"""


def _build_user_payload(
    criterion: str, exception: str, fields: list[str], schema_slots: list[str]
) -> str:
    return json.dumps({
        "criterion": criterion or "",
        "exception": exception or "",
        "rule_declared_fields": fields,
        "program_schema_slots": schema_slots,
    })


async def translate_criterion_to_formula(
    *,
    criterion: str,
    exception: str = "",
    fields: list[str] | None = None,
    schema_slots: list[str] | None = None,
    config: TranslatorConfig,
) -> TranslationResult:
    """Convert a natural-language rule criterion into a validated formula.

    On success returns ``(formula, notes)`` where ``formula`` is a dict
    ready to persist on ``rules.formula`` and ``notes`` is a list of
    caveats the author should review before saving. On failure returns
    ``(None, [reason])`` so the caller (the rule editor) can surface
    exactly why the translation didn't complete.
    """
    fields_list = fields or []
    schema_list = schema_slots or []

    body: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_user_payload(criterion, exception, fields_list, schema_list),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        # Non-reasoning mode: reasoning-variant Nemotrons spend the
        # output budget on hidden chain-of-thought and return empty
        # content on strict-JSON tasks.
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            resp = await client.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return TranslationResult(
            formula=None,
            notes=[f"Translator unreachable: {type(e).__name__}: {e}"],
        )

    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return TranslationResult(
            formula=None,
            notes=["Translator returned unexpected response shape."],
        )

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return TranslationResult(
            formula=None,
            notes=[f"Translator returned non-JSON content: {content[:200]!r}"],
        )

    raw_formula = parsed.get("formula") if isinstance(parsed, dict) else None
    raw_notes = parsed.get("notes") if isinstance(parsed, dict) else None
    notes: list[str] = []
    if isinstance(raw_notes, list):
        notes = [str(n)[:400] for n in raw_notes if n]

    if raw_formula is None:
        # LLM couldn't cover any clause. Signal "complete failure" —
        # caller (Chunk 4 save gate) blocks the save.
        if not notes:
            notes.append("Translator could not express any clause in the allowed grammar.")
        return TranslationResult(formula=None, notes=notes)

    # Validate the formula shape. Any pydantic error is an authoring
    # bug — the LLM produced JSON but it doesn't match the DSL. Return
    # the error verbatim so the editor can surface it under the
    # Formula tab for the author to fix.
    try:
        Formula.model_validate({"root": raw_formula})
    except ValidationError as e:
        notes.append(f"Translator formula failed validation: {e.errors()[0]['msg']}")
        return TranslationResult(formula=None, notes=notes)

    return TranslationResult(formula=raw_formula, notes=notes)


__all__ = ["TranslationResult", "TranslatorConfig", "translate_criterion_to_formula"]
