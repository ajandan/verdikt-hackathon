"""Fail CI if any installed dependency is under a license outside the allowlist (§12).

This project is MIT-licensed. Strong-copyleft upstream deps (GPL, AGPL) are
NOT compatible and must not be introduced. Weak-copyleft (LGPL) deps we
already ship are accepted under dynamic-linking rules and tracked
explicitly in `EXPLICIT_EXCEPTIONS`. Anything else has to land in the
permissive allowlist below.

Run: python scripts/license_check.py
Exit codes: 0 = clean, 1 = violation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable

ALLOWED_LICENSES: set[str] = {
    "MIT License",
    "MIT",
    "BSD License",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache Software License",
    "Apache License 2.0",
    "Apache-2.0",
    "Python Software Foundation License",
    "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "MPL-2.0",
    "The Unlicense (Unlicense)",
    "ISC License (ISCL)",
    "ISC",
    "Historical Permission Notice and Disclaimer (HPND)",
    "HPND",
    "Public Domain",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication",
    "Academic Free License (AFL)",
    "Zope Public License",
    "Apache Software License; MIT License",
    "Apache-2.0 OR BSD-2-Clause",
    "Apache-2.0 AND BSD-2-Clause",
    "BSD-3-Clause OR Apache-2.0",
    "Apache-2.0 OR MIT",
    "MIT AND Python-2.0",
    "MIT-CMU",
    "MIT-0",
    "MIT AND PSF-2.0",
    "MIT OR Apache-2.0",
    "MIT AND Apache-2.0",
    "Apache Software License; BSD License",
    "Apache Software License; MIT License",
    "Apache-2.0 OR BSD-3-Clause",
    "Apache-2.0 OR BSD-2-Clause",
    "Apache-2.0 OR MIT",
    "BSD-3-Clause OR Apache-2.0",
    "MIT AND Python-2.0",
}

# Packages we bundle deliberately even if their metadata is un-allowlistable.
# Every entry must state the license and the reason it's acceptable for our
# distribution model. LGPL is accepted under dynamic-linking rules (users may
# swap the shared library) and because the §4 stack picks (docxtpl, dramatiq,
# psycopg) are all LGPL. No AGPL/GPL entries — pymupdf was retired under
# ADR-0001 Option 2 in favour of pypdfium2 + pdfplumber.
EXPLICIT_EXCEPTIONS: dict[str, str] = {
    "docxtpl": "LGPL-2.1 — chosen in §4 as the render engine; dynamic import, no static linking.",
    "dramatiq": "LGPLv3+ — chosen in §4 as the job broker; dynamic import.",
    "psycopg": "LGPL-3.0 — standard PostgreSQL driver; dynamic import.",
    "psycopg-binary": "LGPL-3.0 — wheel bundle of psycopg; dynamic import.",
    "pypdfium2": (
        "Apache-2.0 / BSD-3-Clause — pip-licenses reports the atypical string "
        "'BSD-3-Clause, Apache-2.0, dependency licenses' because the wrapper "
        "is Apache-2.0/BSD-3-Clause dual-licensed and bundles PDFium (BSD-3-Clause). "
        "All components are permissive and compatible with our MIT distribution."
    ),
}

# Own package.
IGNORE_PACKAGES: set[str] = {"docgen"}


def _run_pip_licenses() -> list[dict[str, str]]:
    try:
        out = subprocess.check_output(
            [sys.executable, "-m", "piplicenses", "--format=json", "--with-urls"],
            text=True,
        )
    except FileNotFoundError:
        print("ERROR: pip-licenses not installed. `pip install pip-licenses`.", file=sys.stderr)
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: pip-licenses failed: {e}", file=sys.stderr)
        sys.exit(2)
    return json.loads(out)


def _classify(entries: Iterable[dict[str, str]]) -> tuple[list[dict], list[dict]]:
    violations: list[dict] = []
    warnings: list[dict] = []
    for e in entries:
        name = e.get("Name", "")
        if name.lower() in IGNORE_PACKAGES:
            continue
        lic = (e.get("License") or "").strip()
        if lic in ALLOWED_LICENSES:
            continue
        if name in EXPLICIT_EXCEPTIONS:
            warnings.append({**e, "note": EXPLICIT_EXCEPTIONS[name]})
            continue
        violations.append(e)
    return violations, warnings


def main() -> int:
    entries = _run_pip_licenses()
    violations, warnings = _classify(entries)

    if warnings:
        print("Allowed via explicit exception:")
        for w in warnings:
            print(f"  {w['Name']} ({w['License']}) — {w['note']}")
        print()

    if not violations:
        print(f"OK: {len(entries)} packages, all licenses on the allowlist.")
        return 0

    print(f"BLOCKED: {len(violations)} package(s) under un-allowlisted licenses:", file=sys.stderr)
    for v in violations:
        print(
            f"  - {v.get('Name'):<30} {v.get('Version'):<12} "
            f"license={v.get('License')!r}  url={v.get('URL')}",
            file=sys.stderr,
        )
    print(
        "\nEither: (a) confirm the license is truly compatible and add it to "
        "ALLOWED_LICENSES; (b) add the package to EXPLICIT_EXCEPTIONS with a URL "
        "and a one-line justification; or (c) drop the dependency.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
