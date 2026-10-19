"""One-shot backfill: translate every existing rule's criterion into a
structured formula via the design-time LLM translator and persist it on
``rules.formula`` + a fresh RuleVersion snapshot.

Idempotent — a rule that already has a non-null formula is skipped
unless ``--force`` is passed. On per-rule translation failure the
script logs the rule_id + notes and continues; those rules stay on
the text-pattern evaluator at runtime until someone re-authors them
in the editor.

Usage:

    # Backfill only rules that don't yet have a formula
    docker exec quire-hackathon-web-1 python scripts/backfill_rule_formulas.py

    # Force re-translation of ALL rules (including ones that already
    # have a formula — useful after a translator prompt improvement)
    docker exec quire-hackathon-web-1 python scripts/backfill_rule_formulas.py --force

    # Limit to a single tenant / program (rule_id substring match)
    docker exec quire-hackathon-web-1 python scripts/backfill_rule_formulas.py --program cardiovita-pap-2026

Design decisions:

* **Sync SQLAlchemy** (not async) — this is a one-shot ops script,
  not a hot path; simpler to reason about.
* **Sequential** — one translator call at a time. Nemotron rate limits
  are gentle; parallelism would need concurrency-tuning per Nebius
  quota. A rule library of 100 rules takes ~2 minutes.
* **RuleVersion snapshot per successful update** — so the audit trail
  captures the "we backfilled a formula" transition alongside the
  formula itself. actor_username="backfill" flags it in history views.
* **Never modify text criterion** — only ``rules.formula`` is written.
  If a rule was authored with a criterion the translator can't cover,
  it stays exactly as it was.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from docgen.config import get_settings
from docgen.store.db import sync_session
from docgen.store.models import Program, Rule, RuleVersion
from pap_pipeline.rule_translator import TranslatorConfig, translate_criterion_to_formula


def _schema_slots_for(rule: Rule, session) -> list[str]:
    """Resolve the schema slot vocabulary for the rule.

    Priority: the rule's own program (program-scoped rules) → the first
    linked program (library rules) → empty (rule authored generic).
    Empty vocab means the translator can't validate field names but
    can still attempt — usually acceptable for library rules.
    """
    from docgen.store.models import ProgramRuleLink

    pid = rule.program_id
    if pid is None:
        row = (
            session.query(ProgramRuleLink.program_id)
            .filter_by(rule_id=rule.id)
            .order_by(ProgramRuleLink.position)
            .first()
        )
        pid = row[0] if row else None
    if pid is None:
        return []
    prog = session.get(Program, pid)
    if prog is None or not prog.extraction_schema:
        return []
    return [s.get("name") for s in prog.extraction_schema if s.get("name")]


def _snapshot_version(session, rule: Rule) -> None:
    """Append a RuleVersion row capturing the current rule state.

    Same shape as _snapshot_rule_version in routes.py, minus the
    request/actor context — this script runs unattended so actor is
    stamped as ``backfill`` for later ops-audit filtering.
    """
    next_version = (
        session.query(RuleVersion)
        .filter_by(rule_id=rule.id)
        .count() + 1
    )
    session.add(RuleVersion(
        rule_id=rule.id,
        version=next_version,
        public_rule_id=rule.rule_id,
        kind=rule.kind,
        title=rule.title,
        criterion=rule.criterion,
        exception=rule.exception or "",
        fields=list(rule.fields or []),
        references_slots=(list(rule.references_slots) if rule.references_slots else None),
        is_active=rule.is_active,
        on_match=rule.on_match or "approve",
        formula=(dict(rule.formula) if rule.formula else None),
        actor_user_id=None,
        actor_username="backfill",
        change_note="Formula backfilled via scripts/backfill_rule_formulas.py",
    ))


async def _translate_one(rule: Rule, schema_slots: list[str], config: TranslatorConfig) -> tuple[dict | None, list[str]]:
    result = await translate_criterion_to_formula(
        criterion=rule.criterion,
        exception=rule.exception or "",
        fields=list(rule.fields or []),
        schema_slots=schema_slots,
        config=config,
    )
    return result.formula, list(result.notes)


async def _run(force: bool, program_filter: str | None, dry_run: bool) -> int:
    settings = get_settings()
    # Match the runtime pipeline's fallback — inbox_hook defaults to
    # Nebius Token Factory when ai_base_url isn't overridden per-deploy.
    base_url = (settings.ai_base_url or "https://api.studio.nebius.com/v1").strip().rstrip("/")
    api_key = (settings.ai_api_key or "").strip()
    if not base_url or not api_key:
        print("[fatal] AI credentials not configured (DOCGEN_AI_API_KEY required).", file=sys.stderr)
        return 2
    config = TranslatorConfig(
        base_url=base_url,
        api_key=api_key,
        model=settings.ai_verdict_model,
        timeout=float(getattr(settings, "ai_timeout_seconds", 60.0)),
    )

    with sync_session() as session:
        q = session.query(Rule).order_by(Rule.tenant_id, Rule.rule_id)
        rules = q.all()

        # Optional program filter (matches program_id → slug OR
        # rule_id substring — pick whichever is easier).
        if program_filter:
            filtered: list[Rule] = []
            for r in rules:
                if r.program_id is not None:
                    prog = session.get(Program, r.program_id)
                    if prog and prog.slug == program_filter:
                        filtered.append(r)
                elif program_filter in (r.rule_id or ""):
                    filtered.append(r)
            rules = filtered

        total = len(rules)
        skipped_has_formula = 0
        translated = 0
        failed = 0

        print(f"[backfill] {total} rule(s) in scope. force={force} dry_run={dry_run}")
        started = datetime.now(UTC)

        for i, rule in enumerate(rules, 1):
            if rule.formula is not None and not force:
                skipped_has_formula += 1
                continue

            schema_slots = _schema_slots_for(rule, session)
            print(f"[backfill] ({i}/{total}) {rule.rule_id} → translating "
                  f"(criterion len={len(rule.criterion or '')}, schema slots={len(schema_slots)})")
            try:
                formula, notes = await _translate_one(rule, schema_slots, config)
            except Exception as e:
                print(f"    ✕ error: {type(e).__name__}: {e}")
                failed += 1
                continue

            if formula is None:
                print(f"    ✕ no formula produced. Notes: {notes}")
                failed += 1
                continue

            if dry_run:
                print(f"    ✓ would save formula (dry run). Notes: {notes}")
                translated += 1
                continue

            rule.formula = formula
            _snapshot_version(session, rule)
            session.commit()
            translated += 1
            note_summary = f" ({len(notes)} note(s))" if notes else ""
            print(f"    ✓ saved formula{note_summary}")

        elapsed = (datetime.now(UTC) - started).total_seconds()
        print(f"\n[backfill] done in {elapsed:.1f}s: "
              f"{translated} translated, {skipped_has_formula} already had formula, {failed} failed.")
        return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true",
                        help="Re-translate rules that already have a formula.")
    parser.add_argument("--program", type=str, default=None,
                        help="Only rules attached to this program slug.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Translate + log, but don't write to DB.")
    args = parser.parse_args()
    return asyncio.run(_run(args.force, args.program, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
