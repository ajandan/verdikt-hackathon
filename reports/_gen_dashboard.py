"""Generate reports/dashboard-data.json from reports/results.xml.

Parses the JUnit XML from `pytest --junitxml=results.xml` and buckets each
test file into a hand-curated suite (Auth, Pipeline, Inbox, …). The
resulting JSON is the sole data source for `reports/dashboard.html`.

Re-run after every test run:
    docker exec quire-hackathon-web-1 python -m pytest --forked --junitxml=results.xml
    docker cp quire-hackathon-web-1:/app/results.xml reports/results.xml
    python reports/_gen_dashboard.py

To add a new test file to a suite, edit SUITES below and re-run. Unknown
files land in the "Other" suite so nothing is silently dropped.
"""
from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
XML_IN = ROOT / "results.xml"
JSON_OUT = ROOT / "dashboard-data.json"
HISTORY_OUT = ROOT / "dashboard-history.json"
HISTORY_KEEP = 50  # keep the last N runs on the dashboard


# Suite mapping — order here is the order shown in the dashboard.
# Each entry: (suite_id, display_name, one-line description, [test files]).
# Files are matched by prefix against pytest classnames like
# "tests.test_admin_login" -> "test_admin_login".
SUITES: list[tuple[str, str, str, list[str]]] = [
    (
        "auth",
        "Auth & session",
        "Login, CSRF, session cookies, rate limits, DB-backed admins.",
        [
            "test_admin_login",
            "test_auth",
            "test_csrf",
            "test_login_ratelimit",
            "test_rbac",
            "test_security_headers",
        ],
    ),
    (
        "pipeline",
        "PAP pipeline",
        "Intake, extraction, MDM lookup, retrieval, reasoning, orchestration.",
        [
            "test_pap_extract",
            "test_pap_reason",
            "test_pap_retrieve",
            "test_pap_orchestrator",
            "test_pap_rules",
            "test_pap_mdm_lookup",
            "test_pap_mdm_tenant_config",
            "test_pap_demo_data",
            "test_pap_history_rules_yaml",
            "test_pap_history_slot_flatten",
            "test_pap_history_citations",
            "test_pap_json_intake_aliases",
            "test_pap_slot_compile",
            "test_pap_inbox_hook",
        ],
    ),
    (
        "inbox",
        "Inbox agent",
        "IMAP parsing, resolver, reply mailer, replies, audit, review executor.",
        [
            "test_inbox_audit",
            "test_inbox_imap_parse",
            "test_inbox_mailer",
            "test_inbox_pap_route",
            "test_inbox_replies",
            "test_inbox_resolver",
            "test_inbox_score",
            "test_inbox_synth",
            "test_inbox_test_executor",
            "test_inbox_test_planner",
            "test_inbox_test_verdict",
        ],
    ),
    (
        "templates",
        "Templates & rendering",
        "DOCX/PDF preflight, publish/rollback, locale variants, supersede, realign.",
        [
            "test_pdf",
            "test_pdf_template",
            "test_preflight",
            "test_preflight_dispatch",
            "test_preflight_job",
            "test_publish",
            "test_supersede",
            "test_locale_variant",
            "test_realign_pdf",
            "test_render_enqueue_failure",
            "test_render_mapped",
            "test_render_stack",
            "test_docx_sanitize",
            "test_merge",
            "test_freeze",
            "test_diff",
            "test_golden",
            "test_sample_eob",
            "test_publish_gate_slot_refs",
        ],
    ),
    (
        "boundaries",
        "Boundaries & policy",
        "Multi-tenancy isolation, SSRF/IDOR, network safety, rate limiting, "
        "per-tenant quotas.",
        [
            "test_boundaries",
            "test_tenancy",
            "test_ssrf_and_idor",
            "test_net_safety",
            "test_rate_limit",
            "test_quotas",
        ],
    ),
    (
        "mcp",
        "MCP surface",
        "Model Context Protocol auth + idempotent tool calls.",
        [
            "test_mcp_auth",
            "test_mcp_idempotent",
        ],
    ),
    (
        "data",
        "Mapping & schema",
        "Payload coercion, mapping transforms, schema/pagination invariants.",
        [
            "test_mapping",
            "test_mapper_backfill",
            "test_coerce_payload",
            "test_schema",
            "test_schema_groups",
            "test_pagination",
            "test_geometry",
        ],
    ),
    (
        "infra",
        "Workers & providers",
        "Idempotency, watchdog reaper, webhooks, provider config, "
        "metrics endpoint, E2E pipeline.",
        [
            "test_worker_idempotency",
            "test_watchdog",
            "test_webhooks",
            "test_idempotency",
            "test_provider_config",
            "test_provider_nebius",
            "test_stub_provider",
            "test_metrics",
            "test_e2e_pipeline",
            "test_ui_smoke",
        ],
    ),
]


def _short_file(classname: str) -> str:
    """`tests.test_admin_login` -> `test_admin_login` (drop nested class)."""
    parts = classname.split(".")
    # classnames look like tests.test_foo or tests.test_foo.TestBar
    for p in parts:
        if p.startswith("test_"):
            return p
    return classname


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT.parent, text=True,
        ).strip()
    except Exception:
        return ""


def main() -> None:
    tree = ET.parse(XML_IN)
    suite_el = tree.find("testsuite")
    if suite_el is None:
        raise SystemExit("No <testsuite> element in results.xml")

    totals = {
        "tests": int(suite_el.get("tests", 0)),
        "failed": int(suite_el.get("failures", 0)),
        "errored": int(suite_el.get("errors", 0)),
        "skipped": int(suite_el.get("skipped", 0)),
        "duration_seconds": round(float(suite_el.get("time", 0.0)), 1),
    }
    totals["passed"] = (
        totals["tests"] - totals["failed"] - totals["errored"] - totals["skipped"]
    )

    # Aggregate per-file counts.
    per_file: dict[str, dict] = defaultdict(
        lambda: {"tests": 0, "passed": 0, "failed": 0, "errored": 0,
                 "skipped": 0, "duration": 0.0}
    )
    for tc in suite_el.findall("testcase"):
        file_ = _short_file(tc.get("classname", ""))
        b = per_file[file_]
        b["tests"] += 1
        b["duration"] += float(tc.get("time", 0.0))
        if tc.find("failure") is not None:
            b["failed"] += 1
        elif tc.find("error") is not None:
            b["errored"] += 1
        elif tc.find("skipped") is not None:
            b["skipped"] += 1
        else:
            b["passed"] += 1

    # Bucket files into suites.
    seen: set[str] = set()
    suites_out = []
    for sid, name, desc, files in SUITES:
        entries = []
        s_tests = s_pass = s_fail = s_err = s_skip = 0
        s_dur = 0.0
        for f in files:
            if f not in per_file:
                continue
            seen.add(f)
            b = per_file[f]
            entries.append({
                "file": f,
                "tests": b["tests"],
                "passed": b["passed"],
                "failed": b["failed"],
                "errored": b["errored"],
                "skipped": b["skipped"],
                "duration": round(b["duration"], 2),
            })
            s_tests += b["tests"]
            s_pass += b["passed"]
            s_fail += b["failed"]
            s_err += b["errored"]
            s_skip += b["skipped"]
            s_dur += b["duration"]
        suites_out.append({
            "id": sid,
            "name": name,
            "description": desc,
            "totals": {
                "tests": s_tests,
                "passed": s_pass,
                "failed": s_fail,
                "errored": s_err,
                "skipped": s_skip,
                "duration": round(s_dur, 2),
            },
            "files": entries,
        })

    # Anything unmapped lands in "Other" so nothing is silently lost.
    other_files = sorted(set(per_file.keys()) - seen)
    if other_files:
        entries = []
        s_tests = s_pass = s_fail = s_err = s_skip = 0
        s_dur = 0.0
        for f in other_files:
            b = per_file[f]
            entries.append({
                "file": f,
                "tests": b["tests"],
                "passed": b["passed"],
                "failed": b["failed"],
                "errored": b["errored"],
                "skipped": b["skipped"],
                "duration": round(b["duration"], 2),
            })
            s_tests += b["tests"]
            s_pass += b["passed"]
            s_fail += b["failed"]
            s_err += b["errored"]
            s_skip += b["skipped"]
            s_dur += b["duration"]
        suites_out.append({
            "id": "other",
            "name": "Other",
            "description": "Test files not yet mapped to a suite. Add them to "
                           "SUITES in _gen_dashboard.py.",
            "totals": {
                "tests": s_tests,
                "passed": s_pass,
                "failed": s_fail,
                "errored": s_err,
                "skipped": s_skip,
                "duration": round(s_dur, 2),
            },
            "files": entries,
        })

    payload = {
        "product": "Verdikt",
        "run": {
            "junit_timestamp": suite_el.get("timestamp", ""),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": totals["duration_seconds"],
            "commit": _git_commit(),
            "framework": "pytest --forked",
            "python": "3.12",
            "environment": "docker (quire-hackathon-web-1)",
        },
        "totals": totals,
        "suites": suites_out,
    }
    JSON_OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        f"wrote {JSON_OUT.name}  "
        f"({totals['tests']} tests, {totals['passed']} passed, "
        f"{totals['failed']} failed, {len(suites_out)} suites)"
    )

    # Append this run to the rolling history so the dashboard can show a
    # trend. Dedupe by JUnit timestamp: a single pytest run has exactly one
    # <testsuite timestamp="…">, so that's the natural identity of the run.
    # The commit stamp is contextual metadata (HEAD at generation time),
    # which can differ if the same results.xml is regenerated after a new
    # commit — we want that to be an update, not a duplicate.
    entry = {
        "timestamp": suite_el.get("timestamp", ""),
        "generated_at": payload["run"]["generated_at"],
        "commit": payload["run"]["commit"],
        "tests": totals["tests"],
        "passed": totals["passed"],
        "failed": totals["failed"],
        "errored": totals["errored"],
        "skipped": totals["skipped"],
        "duration": totals["duration_seconds"],
    }
    history: list[dict] = []
    if HISTORY_OUT.exists():
        try:
            existing = json.loads(HISTORY_OUT.read_text(encoding="utf-8"))
            history = existing.get("runs", [])
        except json.JSONDecodeError:
            history = []
    history = [r for r in history if r.get("timestamp") != entry["timestamp"]]
    history.insert(0, entry)
    history.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    history = history[:HISTORY_KEEP]
    HISTORY_OUT.write_text(
        json.dumps({"generated_at": entry["generated_at"], "runs": history}, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {HISTORY_OUT.name}  ({len(history)} runs)")


if __name__ == "__main__":
    main()
