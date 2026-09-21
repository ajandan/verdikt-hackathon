"""Merge multiple JUnit XML files into one for the parallel-bucket runner.

pytest emits a single ``<testsuite>`` per invocation. When the suite is
split across N buckets for parallelism (see ``run_parallel.sh`` /
CI docs), each bucket writes its own JUnit XML — but
``reports/_gen_dashboard.py`` only reads one file. This script
concatenates the per-bucket ``<testcase>`` elements into one
``<testsuite>``, summing the counts + durations, so the dashboard
generator sees the full run.

Usage::

    python reports/_merge_junit.py --inputs /tmp/buckets/*.xml --out reports/results.xml
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def merge(inputs: list[Path], out: Path) -> None:
    merged_cases: list[ET.Element] = []
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    total_time = 0.0
    hostname = ""
    timestamp = ""
    name = "pytest"
    for src in inputs:
        tree = ET.parse(src)
        root = tree.getroot()
        # JUnit produces either <testsuites><testsuite>...</testsuites> or a
        # bare <testsuite>. Handle both.
        suites = list(root.iter("testsuite"))
        for suite in suites:
            totals["tests"] += int(suite.get("tests", "0") or 0)
            totals["failures"] += int(suite.get("failures", "0") or 0)
            totals["errors"] += int(suite.get("errors", "0") or 0)
            totals["skipped"] += int(suite.get("skipped", "0") or 0)
            try:
                total_time += float(suite.get("time", "0") or 0)
            except ValueError:
                pass
            if not hostname:
                hostname = suite.get("hostname", "") or ""
            if not timestamp:
                timestamp = suite.get("timestamp", "") or ""
            name = suite.get("name", name) or name
            for case in suite.findall("testcase"):
                merged_cases.append(case)

    root_out = ET.Element(
        "testsuites",
        attrib={
            "tests": str(totals["tests"]),
            "failures": str(totals["failures"]),
            "errors": str(totals["errors"]),
            "skipped": str(totals["skipped"]),
            "time": f"{total_time:.3f}",
        },
    )
    suite_out = ET.SubElement(
        root_out,
        "testsuite",
        attrib={
            "name": name,
            "tests": str(totals["tests"]),
            "failures": str(totals["failures"]),
            "errors": str(totals["errors"]),
            "skipped": str(totals["skipped"]),
            "time": f"{total_time:.3f}",
            "hostname": hostname,
            "timestamp": timestamp,
        },
    )
    for case in merged_cases:
        suite_out.append(case)

    out.write_bytes(ET.tostring(root_out, encoding="utf-8", xml_declaration=True))
    print(
        f"merged {len(inputs)} XMLs -> {out}  "
        f"({totals['tests']} tests, {totals['failures']} failed, "
        f"{totals['errors']} errors, {totals['skipped']} skipped, "
        f"{total_time:.1f}s summed)"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    merge([Path(p) for p in args.inputs], Path(args.out))


if __name__ == "__main__":
    main()
