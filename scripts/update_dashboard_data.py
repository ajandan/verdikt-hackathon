"""Update reports/dashboard-data.json from a pytest JUnit XML file.

Reads .tmp/pytest-junit.xml (or path from arg 1) and rewrites the
dashboard's totals + per-suite + per-file counts to match. Existing suite
descriptions and ordering are preserved. New test files are appended to
the ``NEW_FILE_BUCKETS`` mapping — add entries there to route them into
the right suite.

Also appends a compact entry to reports/dashboard-history.json so the
run's totals persist for the sparkline. Idempotent: rerunning against
the same XML rewrites the head entry rather than duplicating.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).parent.parent
DASHBOARD = REPO / "reports" / "dashboard-data.json"
HISTORY = REPO / "reports" / "dashboard-history.json"
DEFAULT_XML = REPO / ".tmp" / "pytest-junit.xml"

# Route any new test file (added since the last dashboard snapshot) to its
# suite. Keys are the basename without the .py extension.
NEW_FILE_BUCKETS = {
    "test_pap_program_model_override": "pipeline",
    "test_pap_verify": "pipeline",
}


def _git_head_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _load_xml(path: Path) -> ET.Element:
    tree = ET.parse(path)
    root = tree.getroot()
    # JUnit XML from pytest wraps testsuites (top) around testsuite (per run).
    if root.tag == "testsuites":
        # Merge all inner testsuite children into one virtual element.
        return root
    return root


def _iter_testcases(root: ET.Element):
    if root.tag == "testsuites":
        for ts in root.findall("testsuite"):
            for tc in ts.findall("testcase"):
                yield tc
    else:
        for tc in root.findall("testcase"):
            yield tc


def _classify(tc: ET.Element) -> str:
    """Return 'passed' / 'failed' / 'errored' / 'skipped'."""
    if tc.find("failure") is not None:
        return "failed"
    if tc.find("error") is not None:
        return "errored"
    if tc.find("skipped") is not None:
        return "skipped"
    return "passed"


def _file_from_classname(classname: str) -> str:
    """Pytest classname is dotted; the last path component before the
    ClassName-or-nothing is the module. Handle both
    ``tests.test_foo`` and ``tests.test_foo.TestBar`` shapes."""
    if not classname:
        return "unknown"
    parts = classname.split(".")
    for p in reversed(parts):
        if p.startswith("test_"):
            return p
    return parts[-1]


def parse_junit(xml_path: Path) -> tuple[dict, float, str]:
    """Return (per-file bucket dict, total duration, timestamp).

    per-file bucket:
        {file_basename: {"tests": N, "passed": N, "failed": N, "errored": N,
                         "skipped": N, "duration": float}}
    """
    root = _load_xml(xml_path)
    by_file: dict[str, dict] = {}
    total_duration = 0.0
    timestamp = ""

    # Grab timestamp off the outer testsuite element if present.
    for ts in root.findall(".//testsuite"):
        ts_stamp = ts.attrib.get("timestamp")
        if ts_stamp and not timestamp:
            timestamp = ts_stamp

    for tc in _iter_testcases(root):
        classname = tc.attrib.get("classname", "")
        file_id = _file_from_classname(classname)
        duration = float(tc.attrib.get("time", 0.0))
        outcome = _classify(tc)

        bucket = by_file.setdefault(
            file_id,
            {"tests": 0, "passed": 0, "failed": 0, "errored": 0, "skipped": 0, "duration": 0.0},
        )
        bucket["tests"] += 1
        bucket[outcome] += 1
        bucket["duration"] += duration
        total_duration += duration

    return by_file, total_duration, timestamp


def rebuild_dashboard(xml_path: Path) -> dict:
    data = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    by_file, total_duration, timestamp = parse_junit(xml_path)

    # Build a lookup so we know which suite each existing file lives in.
    file_to_suite: dict[str, str] = {}
    for suite in data["suites"]:
        for f in suite["files"]:
            file_to_suite[f["file"]] = suite["id"]

    # Route new files via NEW_FILE_BUCKETS; anything else lands in "pipeline"
    # by default (opinionated — most new tests come from pipeline work).
    for fid in by_file:
        if fid not in file_to_suite:
            file_to_suite[fid] = NEW_FILE_BUCKETS.get(fid, "pipeline")

    # Rebuild per-suite files, preserving ordering: existing files first (in
    # their existing order), then newly-appended files sorted alphabetically.
    suites_by_id = {s["id"]: s for s in data["suites"]}
    seen_per_suite: dict[str, set[str]] = {sid: set() for sid in suites_by_id}

    for suite in data["suites"]:
        new_files = []
        for f in suite["files"]:
            fid = f["file"]
            if fid in by_file:
                # Update in place with new counts.
                new_files.append({"file": fid, **by_file[fid]})
                seen_per_suite[suite["id"]].add(fid)
            # If the file no longer exists in the run, drop it silently
            # (deleted / renamed test file).
        suite["files"] = new_files

    # Append newly-discovered files to their assigned suite.
    for fid, counts in sorted(by_file.items()):
        suite_id = file_to_suite[fid]
        if fid not in seen_per_suite[suite_id]:
            suites_by_id[suite_id]["files"].append({"file": fid, **counts})

    # Recompute per-suite totals.
    for suite in data["suites"]:
        totals = {"tests": 0, "passed": 0, "failed": 0, "errored": 0, "skipped": 0, "duration": 0.0}
        for f in suite["files"]:
            for k in totals:
                totals[k] += f[k]
        totals["duration"] = round(totals["duration"], 2)
        suite["totals"] = totals

    # Recompute overall totals.
    grand = {"tests": 0, "passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    for suite in data["suites"]:
        for k in grand:
            grand[k] += suite["totals"][k]
    grand["duration_seconds"] = round(total_duration, 2)
    data["totals"] = grand

    # Update run metadata.
    data["run"]["junit_timestamp"] = timestamp or datetime.now(UTC).isoformat()
    data["run"]["generated_at"] = datetime.now(UTC).isoformat()
    data["run"]["duration_seconds"] = round(total_duration, 2)
    data["run"]["commit"] = _git_head_short()

    return data


def update_history(data: dict) -> None:
    """Append a compact snapshot to dashboard-history.json's ``runs`` list.

    History file shape: ``{"generated_at": iso, "runs": [entry, ...]}``.
    Each entry is flat (timestamp/commit/tests/passed/failed/errored/skipped/duration)
    so the sparkline consumer stays trivial. Dedupe by (commit, generated_at)
    so reruns overwrite rather than grow the file.
    """
    if HISTORY.exists():
        history = json.loads(HISTORY.read_text(encoding="utf-8"))
        if not isinstance(history, dict) or "runs" not in history:
            history = {"generated_at": "", "runs": []}
    else:
        history = {"generated_at": "", "runs": []}

    totals = data["totals"]
    entry = {
        "timestamp": data["run"]["junit_timestamp"],
        "generated_at": data["run"]["generated_at"],
        "commit": data["run"]["commit"],
        "tests": totals["tests"],
        "passed": totals["passed"],
        "failed": totals["failed"],
        "errored": totals["errored"],
        "skipped": totals["skipped"],
        "duration": totals["duration_seconds"],
    }

    runs = history["runs"]
    if runs and runs[-1].get("commit") == entry["commit"] and \
       runs[-1].get("generated_at") == entry["generated_at"]:
        runs[-1] = entry
    else:
        runs.append(entry)

    history["generated_at"] = data["run"]["generated_at"]
    history["runs"] = runs
    HISTORY.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    xml_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_XML
    if not xml_path.exists():
        print(f"error: no such file: {xml_path}", file=sys.stderr)
        return 2

    data = rebuild_dashboard(xml_path)
    DASHBOARD.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    update_history(data)

    t = data["totals"]
    print(
        f"Dashboard updated: {t['tests']} tests · {t['passed']} passed · "
        f"{t['failed']} failed · {t['errored']} errored · {t['skipped']} skipped · "
        f"{t['duration_seconds']}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
