"""Deterministic mapper — no network. Turns [PLACEHOLDER] / <<name>> / ___ into jinja tags.

Intentionally dumb. Useful in tests, air-gapped demos, and as the safe default.
Real intelligence lives in openai_compat.
"""
from __future__ import annotations

import re

from docgen.core.schema_infer import FieldSpec
from docgen.providers.base import MappingProposal, Suggestion

_BRACKETED = re.compile(r"\[([A-Z][A-Z0-9_ /-]{1,60})\]")
_ANGLE = re.compile(r"<<\s*([A-Za-z][A-Za-z0-9_ .-]{0,60})\s*>>")


def _slug(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return s or "field"


def _guess_type(name: str) -> str:
    lower = name.lower()
    if any(k in lower for k in ("date", "dob", "expires", "expiry")):
        return "date"
    if any(k in lower for k in ("amount", "balance", "total", "price", "cost", "fee")):
        return "number"
    if any(k in lower for k in ("count", "qty", "quantity", "number_of", "num_")):
        return "integer"
    if any(k in lower for k in ("is_", "has_", "flag")):
        return "boolean"
    return "string"


class StubProvider:
    name = "stub"

    async def propose_mapping(self, template_text: str) -> MappingProposal:
        suggestions: list[Suggestion] = []
        seen: set[str] = set()

        for m in _BRACKETED.finditer(template_text):
            raw = m.group(1)
            name = _slug(raw)
            if name in seen:
                continue
            seen.add(name)
            suggestions.append(
                Suggestion(
                    placeholder=m.group(0),
                    jinja="{{ " + name + " }}",
                    field=FieldSpec(name=name, type=_guess_type(name), description=raw),  # type: ignore[arg-type]
                )
            )

        for m in _ANGLE.finditer(template_text):
            raw = m.group(1)
            name = _slug(raw)
            if name in seen:
                continue
            seen.add(name)
            suggestions.append(
                Suggestion(
                    placeholder=m.group(0),
                    jinja="{{ " + name + " }}",
                    field=FieldSpec(name=name, type=_guess_type(name), description=raw),  # type: ignore[arg-type]
                )
            )

        return MappingProposal(
            suggestions=suggestions,
            notes=(
                "Stub provider: matched [BRACKETED] and <<angle>> placeholders. "
                "Set DOCGEN_AI_PROVIDER=openai_compat for LLM-driven mapping."
            ),
        )
