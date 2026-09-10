"""Human-authored eligibility rules for the demo program.

Rules are stored as YAML files in this directory, loaded and embedded into
the pgvector store at startup. See ../schemas/rule.py for the shape; Phase 1
authors the four rules for the demo program here.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from pap_pipeline.schemas import Rule

_RULES_DIR = Path(__file__).parent


def load_rules(program: str | None = None) -> list[Rule]:
    """Return every rule under this directory as a validated Rule list.

    When `program` is given, only rules whose `program` field matches are
    returned — Stage 2 uses this to namespace retrieval so multi-program
    deployments don't leak rules across programs. When None, every rule from
    every program is returned; useful for admin views and tests.

    Rules are read from disk on every call. That's fine at demo scale (single
    digits of YAML files); a Phase-3 pgvector rewrite will do this once at
    startup and cache the embedded chunks in the retriever store.
    """
    rules: list[Rule] = []
    for path in sorted(_RULES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        rule = Rule.model_validate(data)
        if program is None or rule.program == program:
            rules.append(rule)
    return rules


__all__ = ["load_rules"]
