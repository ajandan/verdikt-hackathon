"""Human-authored eligibility rules for the pipeline.

Two sources, in priority order:

1. **DB (`rules` library + `program_rule_links`)** — editable per tenant via
   the admin UI. Each tenant has its own rule library; programs attach rules
   from that library through a many-to-many join. When the caller passes a
   live sync session and a program slug that resolves to at least one active
   link, that set wins.
2. **YAML packs (this directory)** — the seed corpus and the fallback for
   programs that haven't been migrated into the DB (or for callers with no
   session — tests, offline tooling). See ../schemas/rule.py for the shape.

The DB rows are seeded from the YAML packs at migration time (0025): every
tenant gets a copy of the library, and every existing Program is linked to
the rules for its slug. Ops edits then diverge the DB copy while YAML stays
as a historical/reference baseline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pap_pipeline.schemas import Rule

_RULES_DIR = Path(__file__).parent


def _load_yaml_rules(program: str | None) -> list[Rule]:
    """Return YAML-backed rules, optionally filtered by program slug."""
    rules: list[Rule] = []
    for path in sorted(_RULES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        rule = Rule.model_validate(data)
        if program is None or rule.program == program:
            rules.append(rule)
    return rules


def _load_db_rules(session: Any, program: str, tenant_id: Any | None) -> list[Rule] | None:
    """Resolve the tenant's Program row for `program`, then walk its
    `ProgramRuleLink` rows into the tenant's `Rule` library. Both the link
    and the library rule must be active. Ordered by the link's `position`.

    Returns None (not empty list) when no matching program row exists —
    signals the caller to fall back to YAML. Empty list means the program
    exists but has no active links.
    """
    from docgen.store.models import Program, ProgramRuleLink
    from docgen.store.models import Rule as RuleRow

    q = session.query(Program).filter(
        Program.slug == program, Program.is_active.is_(True)
    )
    if tenant_id is not None:
        q = q.filter(Program.tenant_id == tenant_id)
    prog = q.first()
    if prog is None:
        return None
    rows = (
        session.query(RuleRow, ProgramRuleLink)
        .join(ProgramRuleLink, ProgramRuleLink.rule_id == RuleRow.id)
        .filter(
            ProgramRuleLink.program_id == prog.id,
            ProgramRuleLink.is_active.is_(True),
            RuleRow.is_active.is_(True),
        )
        .order_by(ProgramRuleLink.position, RuleRow.rule_id)
        .all()
    )
    return [
        Rule(
            id=rule_row.rule_id,
            program=program,
            kind=rule_row.kind,
            title=rule_row.title,
            criterion=rule_row.criterion,
            exception=rule_row.exception or "",
            fields=list(rule_row.fields or []),
            references_slots=list(rule_row.references_slots) if rule_row.references_slots else None,
        )
        for rule_row, _link in rows
    ]


def load_rules(
    program: str | None = None,
    *,
    session: Any | None = None,
    tenant_id: Any | None = None,
) -> list[Rule]:
    """Return every rule for a program as a validated Rule list.

    Priority:
      - When `session` and `program` are both provided and a Program row
        matches (optionally scoped to `tenant_id`), return the rules linked
        to that program via `program_rule_links`, in position order. This
        is the runtime path used by the pipeline once a tenant has editable
        rules.
      - Otherwise, fall through to the YAML packs on disk. This is the path
        for tests, offline tooling, and pre-migration deployments.

    Rules are read on every call. That's fine at demo scale; a Phase-3
    pgvector rewrite can cache the embedded chunks in the retriever store.
    """
    if session is not None and program:
        db_rules = _load_db_rules(session, program, tenant_id)
        if db_rules is not None:
            return db_rules
    return _load_yaml_rules(program)


__all__ = ["load_rules"]
