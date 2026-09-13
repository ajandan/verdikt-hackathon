"""Stage 3 — eligibility reasoning.

Calls Nemotron-Super on Nebius Token Factory with the extracted intake and
the retrieved rules and returns a structured Decision with citations back to
the rules that fired.

JSON-mode call so the output validates against Decision without a free-text
parse step. Nemotron-Super is invoked in non-reasoning mode
(`chat_template_kwargs.enable_thinking=False`) — the reasoning variant burns
output tokens on a hidden trace and returns empty content on JSON tasks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from pap_pipeline.schemas.decision import Decision
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule

_SYSTEM_PROMPT = """You are an eligibility adjudicator for a patient-assistance program.

You receive a JSON intake and a list of eligibility rules. For each rule decide
whether the intake satisfies it (matched=true), fails it (matched=false), or
cannot be evaluated because a required intake field is missing.

Return STRICT JSON matching this exact schema — no extra keys, no prose:

{
  "outcome": "approve" | "deny" | "missing_info",
  "citations": [
    {"rule_id": "<rule id>", "matched": true | false, "score": null}
  ],
  "missing_fields": ["<intake field name>", ...],
  "rationale": "<one sentence>"
}

Decision procedure:
- If every rule is evaluable and every citation has matched=true → "approve".
- Else if any rule is evaluable and its citation has matched=false → "deny".
- Otherwise (at least one rule cannot be evaluated because a required intake
  field is null) → "missing_info". List the null intake field names in
  "missing_fields", drawn from the rule's declared `fields`.
- Emit exactly one citation per rule in the input, preserving input order.
- For rules that cannot be evaluated, use matched=false and rely on the
  presence of missing_fields to distinguish "deny" from "missing_info".
- Leave "score" as null; retrieval-time reranker scores are attached by the
  pipeline after this stage, not by you.
- "rationale" is a single human-readable sentence naming the deciding rule(s).
"""


@dataclass(frozen=True)
class ReasonClientConfig:
    """Minimal client config for the Nebius Token Factory chat endpoint.

    Kept as an explicit argument to :func:`reason` so the stage stays
    independently testable — the pipeline orchestrator constructs one from
    docgen settings; unit tests construct one inline.
    """

    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0


async def reason(
    intake: Intake,
    rules: list[Rule],
    *,
    config: ReasonClientConfig,
    extras: dict[str, Any] | None = None,
) -> Decision:
    """Reason over the intake against the retrieved rules; emit a Decision.

    `extras` carries program-specific extraction fields that don't map onto
    the canonical :class:`Intake` schema. They're merged into the intake JSON
    payload sent to the model so a rule that references (e.g.) a program's
    `member_id` slot can still be evaluated.

    Slot-mediation invariant (ADR-0004): history reaches this stage only as
    derived slot values under `history_slots`. Raw member-history events are
    not permitted in `extras` — the rules pack, not the LLM, decides how
    history matters. A caller that violates this boundary fails here, not
    silently in the prompt.
    """
    _forbid_raw_history_events(extras)

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(intake, rules, extras)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        # Non-reasoning mode: reasoning-variant Nemotrons spend the output
        # budget on a hidden trace and return empty content on JSON tasks.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        resp = await client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Nemotron response shape: {data}") from e

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Nemotron returned non-JSON content: {content[:400]!r}"
        ) from e

    try:
        return Decision.model_validate(parsed)
    except ValidationError as e:
        raise RuntimeError(f"Nemotron JSON did not match Decision schema: {e}") from e


def _forbid_raw_history_events(extras: dict[str, Any] | None) -> None:
    """Enforce the ADR-0004 slot-mediation invariant at the stage boundary.

    History reaches the reason stage as derived slot values (`history_slots`),
    never as raw events. This guard is intentionally strict: it rejects the
    key regardless of value shape so a well-meaning "pass an empty list for
    now" doesn't erode the invariant over time.
    """
    if extras is None:
        return
    if "history_events" in extras:
        raise ValueError(
            "reason() extras must not contain 'history_events' — "
            "raw member-history events are forbidden by the slot-mediation "
            "invariant (see ADR-0004). Pass derived slot values under "
            "'history_slots' instead."
        )


def _build_user_message(
    intake: Intake,
    rules: list[Rule],
    extras: dict[str, Any] | None = None,
) -> str:
    """Serialize intake + rules as a single JSON blob for the model.

    Uses `mode="json"` so dates/enums serialize as strings the model can
    reason about textually. Rules include only the fields the model needs to
    cite and evaluate — `program` is elided because retrieval already filtered
    by program. `extras` fields are merged onto the intake payload; Intake
    keys win on collision so canonical values remain authoritative.
    """
    intake_payload: dict[str, Any] = {}
    if extras:
        intake_payload.update(extras)
    intake_payload.update(intake.model_dump(mode="json"))
    return json.dumps(
        {
            "intake": intake_payload,
            "rules": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "fields": r.fields,
                    "title": r.title,
                    "criterion": r.criterion,
                    "exception": r.exception,
                }
                for r in rules
            ],
        },
        indent=2,
    )
