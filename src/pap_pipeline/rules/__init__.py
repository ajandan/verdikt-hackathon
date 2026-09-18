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
    """Resolve the tenant's Program row for ``program``, then return every
    rule that applies to it — the union of two sources:

    1. **Library rules attached via `program_rule_links`** — the legacy
       path where a tenant-shared rule is linked to N programs. Ordered
       by the link's ``position`` field.
    2. **Program-scoped rules** with ``rules.program_id = program.id`` —
       introduced by 0037 as the "copy-and-remap" output of the
       copy-to-program wizard. Ordered by ``rule_id`` after the linked
       set. These use the program's @-field vocabulary directly.

    Deduplicated by ``rule_id`` in case an operator copied a rule AND
    also happened to link its library source — the program-scoped copy
    wins so the wizard-authored @-refs are honored.

    Returns None (not empty list) when no matching program row exists —
    signals the caller to fall back to YAML. Empty list means the
    program exists but has neither links nor scoped copies.
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

    # Legacy library-linked rules
    linked_rows = (
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

    # Program-scoped copies (0037). These carry the program's own
    # @-field vocabulary in criterion/exception so the reasoner
    # evaluates them against the raw extraction dict without needing a
    # canonical Intake bridge.
    scoped_rows = (
        session.query(RuleRow)
        .filter(
            RuleRow.program_id == prog.id,
            RuleRow.is_active.is_(True),
        )
        .order_by(RuleRow.rule_id)
        .all()
    )

    # Program-scoped copies win over library links on rule_id collision.
    # Order preserved: scoped rules first (they're the intentional
    # per-program vocabulary), then any non-colliding linked rules.
    out: list[Rule] = []
    seen: set[str] = set()
    for r in scoped_rows:
        if r.rule_id in seen:
            continue
        seen.add(r.rule_id)
        out.append(_row_to_rule(r, program))
    for rule_row, _link in linked_rows:
        if rule_row.rule_id in seen:
            continue
        seen.add(rule_row.rule_id)
        out.append(_row_to_rule(rule_row, program))
    return out


def _row_to_rule(rule_row: Any, program: str) -> Rule:
    """Convert a Rule ORM row into the pipeline's :class:`Rule` schema.

    Extracted so both the scoped-copy path and the library-linked path
    build the same shape. Deliberately drops ``program_id`` and
    ``field_refs`` from the pipeline schema — those are storage-layer
    concerns; the reasoner only needs the criterion text.
    """
    return Rule(
        id=rule_row.rule_id,
        program=program,
        kind=rule_row.kind,
        title=rule_row.title,
        criterion=rule_row.criterion,
        exception=rule_row.exception or "",
        fields=list(rule_row.fields or []),
        references_slots=list(rule_row.references_slots) if rule_row.references_slots else None,
        # Polarity — how matched=true influences the composed outcome.
        # Older rows before migration 0038 default to 'approve' at the
        # DB level; getattr fallback keeps the ORM-side round-trip safe
        # even if a caller hydrated a Rule from a raw dict missing the
        # column.
        on_match=getattr(rule_row, "on_match", "approve") or "approve",
    )


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
