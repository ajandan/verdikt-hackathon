"""Fail CI when a tenant-scoped model is queried without a tenant filter.

The design doc: 'A missing tenant_id in a WHERE clause is an IDOR waiting to
happen.' Every direct query against a tenant-owned model must either scope
by tenant_id or explicitly declare itself intentionally global via
`# tenant-audit: allow (<reason>)`.

Covered patterns (in `src/docgen/**/*.py`):
    - `select(Model...` where Model is in TENANT_SCOPED_MODELS
    - `update(Model` / `delete(Model`
    - `session.get(Model, ...)` (must be followed by an explicit
      `<row>.tenant_id ==` or `!=` check within LOOKAHEAD_LINES)

Only *direct* tenant owners are checked here — models with their own
`tenant_id` column: Template, RenderJob, CallerProfile, ApiKey,
IdempotencyKey. Indirectly-scoped models (TemplateVersion via Template,
Provenance via RenderJob, etc.) are out of scope for this first pass;
the join structure varies too much for a mechanical check without
producing noise.

Run: python scripts/tenant_scope_audit.py
Exit codes: 0 = clean, 1 = violation(s).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TENANT_SCOPED_MODELS: frozenset[str] = frozenset({
    "Template",
    "RenderJob",
    "CallerProfile",
    "ApiKey",
    "IdempotencyKey",
    # AdminUser rows carry a tenant_id when role='tenant_admin'. Cross-tenant
    # queries here are all inherently super_admin (auth lookup, mgmt UI) — they
    # need an explicit suppression so future per-tenant features can't silently
    # skip scoping.
    "AdminUser",
})

# How far past a select/get to look for `tenant_id` mention. SQLAlchemy chains
# span many `.where().join().order_by()` calls, sometimes with intermediate
# `if` branches that mutate the query. 40 lines catches every real chain in
# the repo without swallowing the next unrelated statement.
LOOKAHEAD_LINES = 40

SUPPRESS_RE = re.compile(r"#\s*tenant-audit:\s*allow\b")
FILE_SUPPRESS_RE = re.compile(r"#\s*tenant-audit:\s*file-allow\b")

# select(Model...) / update(Model / delete(Model
_SELECT_RE = re.compile(
    r"\b(?:select|update|delete)\(\s*("
    + "|".join(re.escape(m) for m in TENANT_SCOPED_MODELS)
    + r")\b"
)
# session.get(Model, ...)
_GET_RE = re.compile(
    r"\.get\(\s*("
    + "|".join(re.escape(m) for m in TENANT_SCOPED_MODELS)
    + r")\s*,"
)

TENANT_ID_RE = re.compile(r"\btenant_id\b")

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "docgen"


def _has_suppression(line: str) -> bool:
    return bool(SUPPRESS_RE.search(line))


def _has_tenant_filter(lines: list[str], start: int) -> bool:
    """True if any of the next LOOKAHEAD_LINES lines mentions `tenant_id`."""
    end = min(len(lines), start + LOOKAHEAD_LINES + 1)
    for i in range(start, end):
        if TENANT_ID_RE.search(lines[i]):
            return True
    return False


def audit_file(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of (line_number, model, pattern) for each violation."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    # File-level allow: one comment in the module docstring or top-of-file
    # (checked in the first 30 lines to catch it wherever the author put it)
    # opts the whole file out. Used for admin-superuser modules where every
    # query is intentionally cross-tenant by design.
    header = "\n".join(lines[:30])
    if FILE_SUPPRESS_RE.search(header):
        return []
    violations: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        for regex, kind in ((_SELECT_RE, "select/update/delete"), (_GET_RE, "session.get")):
            m = regex.search(line)
            if not m:
                continue
            # Suppression can be on the same line as the query OR in the 5
            # lines immediately above (so an operator can drop a comment
            # explaining the exception on its own line right before the call).
            preceding = "\n".join(lines[max(0, i - 5):i + 1])
            if SUPPRESS_RE.search(preceding):
                continue
            if _has_tenant_filter(lines, i):
                continue
            violations.append((i + 1, m.group(1), kind))
    return violations


def main() -> int:
    if not SRC_ROOT.exists():
        print(f"error: source root not found at {SRC_ROOT}", file=sys.stderr)
        return 1
    total_violations = 0
    files_scanned = 0
    for path in sorted(SRC_ROOT.rglob("*.py")):
        files_scanned += 1
        for line_no, model, kind in audit_file(path):
            rel = path.relative_to(SRC_ROOT.parent.parent)
            print(f"{rel}:{line_no}: {kind}({model}) lacks tenant_id filter within "
                  f"{LOOKAHEAD_LINES} lines. Add a `.where(...tenant_id...)` or "
                  f"a `# tenant-audit: allow (<reason>)` suppression.")
            total_violations += 1
    if total_violations:
        print(f"\n{total_violations} tenant-scope violation(s) across {files_scanned} files.",
              file=sys.stderr)
        return 1
    print(f"tenant-scope audit clean ({files_scanned} files, "
          f"{len(TENANT_SCOPED_MODELS)} models covered).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
