"""Rewrite the Proof-slide test counts in the judges deck, one-pager and one-slide.

The three documents each hardcode the same four numbers. They were correct
when written (they reconcile exactly against reports/bucket-a..d.xml from
2026-09-22) and then aged as the suite grew, which is how a slide headed
"Evidence, not adjectives" ends up with a stale figure. This reads a JUnit XML
and puts the same numbers in all three, so they can never drift apart again.

    python scripts/update_slide_stats.py .tmp/vtest-junit.xml

Pass --check to report drift without writing, for a pre-submission sanity pass.

The PDFs beside these HTML files are exported by hand -- there is no renderer
in this repo -- so re-export them after running this, or the PDFs a judge
actually opens will still carry the old numbers.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# (file, [(regex with the number as group 2, totals key)])
# Each pattern is anchored on its own <span> label so the four numbers can
# never be swapped by accident.
TARGETS: dict[str, list[tuple[str, str]]] = {
    "docs/verdikt-judges-deck.html": [
        (r'(<div class="st"><b>)(\d+)(</b><span>tests in the suite</span>)', "tests"),
        (r'(<b style="color:var\(--seal-700\)">)(\d+)(</b><span>passing</span>)', "passed"),
        (r'(<div class="st"><b>)(\d+)(</b><span>failing</span>)', "failed"),
        (r'(<b style="color:var\(--amber-ink\)">)(\d+)(</b><span>skipped</span>)', "skipped"),
    ],
    "docs/verdikt-one-pager.html": [
        (r'(<div class="card num"><b>)(\d+)(</b><span>tests</span>)', "tests"),
        (r'(<b style="color:var\(--seal-700\)">)(\d+)(</b><span>passing</span>)', "passed"),
        (r'(<div class="card num"><b>)(\d+)(</b><span>failing</span>)', "failed"),
        (r'(<b style="color:var\(--ink-500\)">)(\d+)(</b><span>skipped</span>)', "skipped"),
    ],
    "docs/verdikt-one-slide.html": [
        # One sentence carries three of the numbers.
        (r'(<div class="st"><b>)(\d+)(</b><span>tests, )(\d+)( passing, )(\d+)( failing</span>)',
         "_one_slide_triple"),
    ],
}


def totals(xml: Path) -> dict[str, int]:
    root = ET.parse(xml).getroot()
    ts = root if root.tag == "testsuite" else root.find("testsuite")
    a = ts.attrib
    t = int(a.get("tests", 0))
    f, e, s = (int(a.get(k, 0)) for k in ("failures", "errors", "skipped"))
    # "failing" on the slides means anything that did not pass: a collection
    # error is a failure to a reader, whatever pytest calls it internally.
    return {"tests": t, "failed": f + e, "skipped": s, "passed": t - f - e - s}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("xml", type=Path, help="JUnit XML from the run to publish")
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = ap.parse_args()

    if not args.xml.exists():
        sys.exit(f"no such JUnit XML: {args.xml}")
    tot = totals(args.xml)
    print(f"[slides] {args.xml}: {tot['tests']} tests, {tot['passed']} passing, "
          f"{tot['failed']} failing, {tot['skipped']} skipped")

    drift = 0
    for rel, patterns in TARGETS.items():
        path = Path(rel)
        if not path.exists():
            print(f"[slides] skip {rel} — missing")
            continue
        text = original = path.read_text(encoding="utf-8")
        for pattern, key in patterns:
            if key == "_one_slide_triple":
                def sub(m: re.Match) -> str:
                    return (f"{m.group(1)}{tot['tests']}{m.group(3)}{tot['passed']}"
                            f"{m.group(5)}{tot['failed']}{m.group(7)}")
                text, n = re.subn(pattern, sub, text)
            else:
                text, n = re.subn(pattern, lambda m: f"{m.group(1)}{tot[key]}{m.group(3)}", text)
            if n == 0:
                print(f"[slides] WARN {rel}: pattern for '{key}' matched nothing — markup moved?")
        if text != original:
            drift += 1
            if not args.check:
                path.write_text(text, encoding="utf-8", newline="\n")
            print(f"[slides] {'would update' if args.check else 'updated'} {rel}")
        else:
            print(f"[slides] {rel} already current")

    if args.check and drift:
        sys.exit(1)


if __name__ == "__main__":
    main()
