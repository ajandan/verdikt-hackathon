"""Generate an xlsx summary of the LATEST pytest run from results.xml.

Reads reports/results.xml (JUnit XML from `pytest --junitxml=results.xml`) and
writes reports/test-report-latest.xlsx with 4 sheets: Summary, By file,
All tests, Slowest.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).parent
XML_IN = ROOT / "results.xml"
OUT = ROOT / "test-report-latest.xlsx"

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF")
PASS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
SKIP_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
SECTION_FILL = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
SECTION_FONT = Font(bold=True)

tree = ET.parse(XML_IN)
root = tree.getroot()
suite = root.find("testsuite")
assert suite is not None
suite_totals = {
    "tests": int(suite.get("tests", 0)),
    "failures": int(suite.get("failures", 0)),
    "errors": int(suite.get("errors", 0)),
    "skipped": int(suite.get("skipped", 0)),
    "time": float(suite.get("time", 0.0)),
    "timestamp": suite.get("timestamp", ""),
    "hostname": suite.get("hostname", ""),
}
passed = (
    suite_totals["tests"]
    - suite_totals["failures"]
    - suite_totals["errors"]
    - suite_totals["skipped"]
)

cases: list[dict] = []
for tc in suite.findall("testcase"):
    classname = tc.get("classname", "")
    name = tc.get("name", "")
    t = float(tc.get("time", 0.0))
    result = "PASS"
    detail = ""
    if tc.find("failure") is not None:
        result = "FAIL"
        detail = (tc.find("failure").get("message") or "").splitlines()[0][:200]
    elif tc.find("error") is not None:
        result = "ERROR"
        detail = (tc.find("error").get("message") or "").splitlines()[0][:200]
    elif tc.find("skipped") is not None:
        result = "SKIP"
        detail = (tc.find("skipped").get("message") or "").splitlines()[0][:200]
    cases.append({
        "file": classname,
        "name": name,
        "time": t,
        "result": result,
        "detail": detail,
    })

by_file: dict[str, dict] = defaultdict(
    lambda: {"pass": 0, "fail": 0, "error": 0, "skip": 0, "time": 0.0}
)
for c in cases:
    bucket = by_file[c["file"]]
    bucket["time"] += c["time"]
    if c["result"] == "PASS":
        bucket["pass"] += 1
    elif c["result"] == "FAIL":
        bucket["fail"] += 1
    elif c["result"] == "ERROR":
        bucket["error"] += 1
    else:
        bucket["skip"] += 1

wb = Workbook()

# --- Summary ---------------------------------------------------------------
ws = wb.active
ws.title = "Summary"

ws["A1"] = "Verdikt — Pytest Run Report (latest)"
ws["A1"].font = Font(bold=True, size=16)
ws.merge_cells("A1:D1")

ws["A3"] = "Run environment"
ws["A3"].font = SECTION_FONT
ws["A3"].fill = SECTION_FILL
ws.merge_cells("A3:D3")

env_rows = [
    ("Timestamp", suite_totals["timestamp"]),
    ("Hostname", suite_totals["hostname"]),
    ("Runner", "docker exec quire-hackathon-web-1 python -m pytest --forked --junitxml=results.xml"),
    ("Duration", f"{suite_totals['time']:.1f}s"),
    ("JUnit source", "reports/results.xml"),
]
for i, (k, v) in enumerate(env_rows, start=4):
    ws.cell(row=i, column=1, value=k).font = Font(bold=True)
    ws.cell(row=i, column=2, value=v)
    ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)

ws["A10"] = "Results"
ws["A10"].font = SECTION_FONT
ws["A10"].fill = SECTION_FILL
ws.merge_cells("A10:D10")

ws.cell(row=11, column=1, value="Bucket").font = HEADER_FONT
ws.cell(row=11, column=1).fill = HEADER_FILL
ws.cell(row=11, column=2, value="Count").font = HEADER_FONT
ws.cell(row=11, column=2).fill = HEADER_FILL
ws.cell(row=11, column=3, value="Share").font = HEADER_FONT
ws.cell(row=11, column=3).fill = HEADER_FILL

total = suite_totals["tests"]
result_rows = [
    ("Passed", passed, PASS_FILL),
    ("Failed", suite_totals["failures"], FAIL_FILL),
    ("Errors", suite_totals["errors"], FAIL_FILL),
    ("Skipped", suite_totals["skipped"], SKIP_FILL),
    ("Total", total, None),
]
for i, (label, count, fill) in enumerate(result_rows, start=12):
    ws.cell(row=i, column=1, value=label)
    c = ws.cell(row=i, column=2, value=count)
    if fill:
        c.fill = fill
    p = ws.cell(row=i, column=3, value=(count / total) if total else 0)
    p.number_format = "0.0%"

ws["A18"] = "Verdict"
ws["A18"].font = SECTION_FONT
ws["A18"].fill = SECTION_FILL
ws.merge_cells("A18:D18")
ws["A19"] = (
    f"All {total} tests pass in {suite_totals['time']:.1f}s. "
    "Zero failures, zero errors, zero skipped. Suite is green."
    if suite_totals["failures"] == 0 and suite_totals["errors"] == 0
    else f"{passed}/{total} passed, {suite_totals['failures']} failed, "
         f"{suite_totals['errors']} errored. See 'All tests' sheet."
)
ws["A19"].alignment = Alignment(wrap_text=True, vertical="top")
ws.merge_cells("A19:D21")

for col, width in [("A", 22), ("B", 40), ("C", 14), ("D", 14)]:
    ws.column_dimensions[col].width = width

# --- By file ---------------------------------------------------------------
ws = wb.create_sheet("By file")
for i, h in enumerate(
    ["Test file / class", "Pass", "Fail", "Error", "Skip", "Total", "Time (s)"], start=1
):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

sorted_files = sorted(by_file.items(), key=lambda kv: kv[0])
for r, (fname, stats) in enumerate(sorted_files, start=2):
    ws.cell(row=r, column=1, value=fname)
    pc = ws.cell(row=r, column=2, value=stats["pass"])
    fc = ws.cell(row=r, column=3, value=stats["fail"])
    ec = ws.cell(row=r, column=4, value=stats["error"])
    sc = ws.cell(row=r, column=5, value=stats["skip"])
    ws.cell(
        row=r, column=6,
        value=stats["pass"] + stats["fail"] + stats["error"] + stats["skip"],
    )
    ws.cell(row=r, column=7, value=round(stats["time"], 3))
    if stats["pass"]:
        pc.fill = PASS_FILL
    if stats["fail"]:
        fc.fill = FAIL_FILL
    if stats["error"]:
        ec.fill = FAIL_FILL
    if stats["skip"]:
        sc.fill = SKIP_FILL

for col, width in [("A", 52), ("B", 8), ("C", 8), ("D", 8), ("E", 8), ("F", 8), ("G", 10)]:
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A2"

# --- All tests -------------------------------------------------------------
ws = wb.create_sheet("All tests")
for i, h in enumerate(["#", "File / class", "Test", "Time (s)", "Result", "Detail"], start=1):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

fill_for = {"PASS": PASS_FILL, "FAIL": FAIL_FILL, "ERROR": FAIL_FILL, "SKIP": SKIP_FILL}
for i, c in enumerate(cases, start=2):
    ws.cell(row=i, column=1, value=i - 1)
    ws.cell(row=i, column=2, value=c["file"])
    ws.cell(row=i, column=3, value=c["name"])
    ws.cell(row=i, column=4, value=round(c["time"], 3))
    rc = ws.cell(row=i, column=5, value=c["result"])
    rc.fill = fill_for[c["result"]]
    ws.cell(row=i, column=6, value=c["detail"])

for col, width in [("A", 5), ("B", 46), ("C", 66), ("D", 10), ("E", 8), ("F", 60)]:
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A2"

# --- Slowest 25 ------------------------------------------------------------
ws = wb.create_sheet("Slowest")
for i, h in enumerate(["Rank", "File / class", "Test", "Time (s)"], start=1):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

slowest = sorted(cases, key=lambda c: c["time"], reverse=True)[:25]
for i, c in enumerate(slowest, start=2):
    ws.cell(row=i, column=1, value=i - 1)
    ws.cell(row=i, column=2, value=c["file"])
    ws.cell(row=i, column=3, value=c["name"])
    ws.cell(row=i, column=4, value=round(c["time"], 3))

for col, width in [("A", 6), ("B", 46), ("C", 66), ("D", 10)]:
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A2"

wb.save(OUT)
print(f"wrote {OUT}  ({total} tests, {passed} passed, "
      f"{suite_totals['failures']} failed, {suite_totals['time']:.1f}s)")
