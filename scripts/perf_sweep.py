"""Read-only Lighthouse-style perf + hygiene sweep for public Verdikt pages.

Runs sequentially against http://localhost:8100 with fresh context per URL
(cold) then a warm second hit. Writes report + json + screenshots into reports/.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS = REPO_ROOT / "reports"
SHOTS = REPORTS / "perf-shots"
SHOTS.mkdir(parents=True, exist_ok=True)

BASE = "http://localhost:8100"

PAGES = [
    ("/", "landing"),
    ("/engineering/about.html", "about"),
    ("/engineering/domain.html", "domain"),
    ("/engineering/thesis.html", "thesis"),
    ("/engineering/index.html", "overview"),
    ("/engineering/features.html", "features"),
    ("/engineering/dashboard.html", "dashboard"),
    ("/engineering/code-review.html", "code-review"),
    ("/engineering/security-review.html", "security-review"),
    ("/engineering/production.html", "production"),
    ("/engineering/hackathon.html", "hackathon"),
]

SECURITY_HEADERS = [
    "content-security-policy",
    "strict-transport-security",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
]

# noise filter — google fonts preconnect etc.
NOISE_HOSTS = re.compile(r"(fonts\.googleapis\.com|fonts\.gstatic\.com|google-analytics|googletagmanager)")


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    except Exception:
        return "unknown"


async def measure_page(context, url_path: str, slug: str, idx: int) -> dict:
    """Cold + warm measurement for a single URL. Returns record."""
    full_url = BASE + url_path

    # ---- COLD ----
    await context.clear_cookies()
    # New page each cold run so we get a clean paint/resource timing baseline.
    page = await context.new_page()

    console_errors: list[str] = []
    failed_requests: list[dict] = []
    doc_headers: dict[str, str] | None = None
    doc_status: int | None = None

    def on_console(msg):
        if msg.type == "error":
            console_errors.append(msg.text[:500])

    def on_requestfailed(req):
        if NOISE_HOSTS.search(req.url):
            return
        failed_requests.append({
            "url": req.url,
            "failure": (req.failure or "unknown"),
            "method": req.method,
        })

    async def on_response(resp):
        nonlocal doc_headers, doc_status
        if resp.url == full_url and doc_headers is None:
            doc_status = resp.status
            # lower-case header keys
            doc_headers = {k.lower(): v for k, v in (await resp.all_headers()).items()}
        if resp.status >= 400 and not NOISE_HOSTS.search(resp.url):
            # only count same-origin
            if resp.url.startswith(BASE):
                failed_requests.append({
                    "url": resp.url,
                    "status": resp.status,
                    "method": resp.request.method,
                })

    page.on("console", on_console)
    page.on("requestfailed", on_requestfailed)
    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    t0 = time.perf_counter()
    try:
        await page.goto(full_url, wait_until="load", timeout=15000)
    except Exception as e:
        console_errors.append(f"goto-exception: {e}")
    goto_ms_cold = int((time.perf_counter() - t0) * 1000)

    # Let async response handlers drain briefly.
    await asyncio.sleep(0.2)

    timing = await page.evaluate(
        """() => {
            const t = performance.timing;
            const res = performance.getEntriesByType('resource');
            const bytes = res.reduce((a,r)=>a+(r.transferSize||0),0);
            return {
                ttfb: t.responseStart - t.navigationStart,
                dcl: t.domContentLoadedEventEnd - t.navigationStart,
                load: t.loadEventEnd - t.navigationStart,
                resources: res.length,
                transfer_bytes: bytes,
            };
        }"""
    )

    # a11y smoke
    a11y = await page.evaluate(
        """() => {
            const imgs = Array.from(document.querySelectorAll('img'));
            const no_alt = imgs.filter(i => !i.hasAttribute('alt')).length;
            const h1s = document.querySelectorAll('h1').length;
            const lang = document.documentElement.getAttribute('lang') || null;
            return { imgs: imgs.length, imgs_missing_alt: no_alt, h1_count: h1s, html_lang: lang };
        }"""
    )

    # Screenshot
    shot_name = f"{idx:02d}-{slug}.png"
    try:
        await page.screenshot(path=str(SHOTS / shot_name), full_page=True)
    except Exception as e:
        console_errors.append(f"screenshot-exception: {e}")

    cold_record = {
        "goto_ms": goto_ms_cold,
        "ttfb_ms": timing["ttfb"],
        "dcl_ms": timing["dcl"],
        "load_ms": timing["load"],
        "resources": timing["resources"],
        "transfer_kb": round(timing["transfer_bytes"] / 1024, 1),
        "doc_status": doc_status,
        "console_errors": console_errors,
        "failed_requests": failed_requests,
    }
    await page.close()

    # ---- WARM ---- (same context, no cache clear)
    page = await context.new_page()
    t0 = time.perf_counter()
    try:
        await page.goto(full_url, wait_until="load", timeout=15000)
    except Exception as e:
        pass
    goto_ms_warm = int((time.perf_counter() - t0) * 1000)
    await asyncio.sleep(0.1)
    timing_w = await page.evaluate(
        """() => {
            const t = performance.timing;
            const res = performance.getEntriesByType('resource');
            const bytes = res.reduce((a,r)=>a+(r.transferSize||0),0);
            return {
                ttfb: t.responseStart - t.navigationStart,
                load: t.loadEventEnd - t.navigationStart,
                resources: res.length,
                transfer_bytes: bytes,
            };
        }"""
    )
    warm_record = {
        "goto_ms": goto_ms_warm,
        "ttfb_ms": timing_w["ttfb"],
        "load_ms": timing_w["load"],
        "resources": timing_w["resources"],
        "transfer_kb": round(timing_w["transfer_bytes"] / 1024, 1),
    }
    await page.close()

    return {
        "url": url_path,
        "slug": slug,
        "cold": cold_record,
        "warm": warm_record,
        "headers": {h: (doc_headers or {}).get(h) for h in SECURITY_HEADERS},
        "a11y": a11y,
        "shot": f"reports/perf-shots/{shot_name}",
    }


def verdict_for(cold: dict) -> tuple[str, list[str]]:
    """Return (worst_verdict, notes_list)."""
    notes = []
    worst = "OK"
    if cold["load_ms"] > 5000:
        notes.append(f"BLOCKER load={cold['load_ms']}ms > 5000ms")
        worst = "BLOCKER"
    elif cold["load_ms"] > 2500:
        notes.append(f"NOTE load={cold['load_ms']}ms > 2500ms")
        worst = "NOTE" if worst != "BLOCKER" else worst
    if cold["ttfb_ms"] > 300:
        notes.append(f"NOTE ttfb={cold['ttfb_ms']}ms > 300ms")
        worst = "NOTE" if worst == "OK" else worst
    if cold["transfer_kb"] > 500:
        notes.append(f"NOTE transfer={cold['transfer_kb']}KB > 500KB")
        worst = "NOTE" if worst == "OK" else worst
    if cold.get("console_errors"):
        notes.append(f"BLOCKER {len(cold['console_errors'])} console error(s)")
        worst = "BLOCKER"
    if cold.get("failed_requests"):
        notes.append(f"BLOCKER {len(cold['failed_requests'])} failed request(s)")
        worst = "BLOCKER"
    return worst, notes


async def main():
    started = datetime.now(timezone.utc).isoformat()
    head = git_head()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        chromium_version = browser.version
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        runs = []
        for idx, (path, slug) in enumerate(PAGES, start=1):
            print(f"[{idx:02d}/{len(PAGES)}] {path}", flush=True)
            rec = await measure_page(context, path, slug, idx)
            runs.append(rec)
            await asyncio.sleep(0.5)  # be nice to dev server
        await context.close()
        await browser.close()

    # Compute verdicts
    for r in runs:
        v, notes = verdict_for(r["cold"])
        r["verdict"] = v
        r["notes"] = notes

    data = {
        "generated_at": started,
        "head_sha": head,
        "browser": "chromium",
        "chromium_version": chromium_version,
        "runs": runs,
    }
    (REPORTS / "perf-data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("wrote reports/perf-data.json")

    # ---- Markdown report ----
    md = []
    md.append("# Verdikt perf + hygiene sweep")
    md.append("")
    md.append(f"- **When:** {started}")
    md.append(f"- **HEAD:** `{head}`")
    md.append(f"- **Browser:** Chromium {chromium_version} (headless, viewport 1440x900)")
    md.append(f"- **Server:** {BASE} (Docker `quire-hackathon-web-1`, assumed warm before run)")
    md.append(f"- **Method:** sequential, 500ms pause between pages, cold context per URL then warm re-hit")
    md.append("")
    md.append("## Summary")
    md.append("")
    md.append("| # | URL | TTFB (ms) | DCL (ms) | Load (ms) | Resources | Transfer (KB) | Verdict |")
    md.append("|---|---|---:|---:|---:|---:|---:|---|")
    for i, r in enumerate(runs, 1):
        c = r["cold"]
        md.append(
            f"| {i} | `{r['url']}` | {c['ttfb_ms']} | {c['dcl_ms']} | {c['load_ms']} | "
            f"{c['resources']} | {c['transfer_kb']} | **{r['verdict']}** |"
        )
    md.append("")
    md.append("## Warm vs cold delta")
    md.append("")
    md.append("| URL | Cold Load (ms) | Warm Load (ms) | Δ Load (ms) | Cold KB | Warm KB | Δ KB |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in runs:
        c, w = r["cold"], r["warm"]
        md.append(
            f"| `{r['url']}` | {c['load_ms']} | {w['load_ms']} | "
            f"{c['load_ms'] - w['load_ms']} | {c['transfer_kb']} | {w['transfer_kb']} | "
            f"{round(c['transfer_kb'] - w['transfer_kb'], 1)} |"
        )
    md.append("")
    md.append("## Security headers (top-level document)")
    md.append("")
    hdr_cols = SECURITY_HEADERS
    md.append("| URL | " + " | ".join(hdr_cols) + " |")
    md.append("|---" + "|---" * len(hdr_cols) + "|")
    for r in runs:
        row = [f"`{r['url']}`"]
        for h in hdr_cols:
            v = r["headers"].get(h)
            row.append("MISSING" if not v else "present")
        md.append("| " + " | ".join(row) + " |")
    md.append("")
    md.append("## Accessibility smoke")
    md.append("")
    md.append("| URL | <img> | missing alt | <h1> count | <html lang> |")
    md.append("|---|---:|---:|---:|---|")
    for r in runs:
        a = r["a11y"]
        md.append(
            f"| `{r['url']}` | {a['imgs']} | {a['imgs_missing_alt']} | "
            f"{a['h1_count']} | {a['html_lang'] or 'MISSING'} |"
        )
    md.append("")

    # Findings
    blockers, notes_out, praises = [], [], []
    for r in runs:
        for n in r["notes"]:
            line = f"- `{r['url']}` — {n}"
            if n.startswith("BLOCKER"):
                blockers.append(line)
            else:
                notes_out.append(line)
        # a11y notes
        a = r["a11y"]
        if a["imgs_missing_alt"] > 0:
            notes_out.append(f"- `{r['url']}` — NOTE {a['imgs_missing_alt']} <img> missing alt")
        if a["h1_count"] != 1:
            notes_out.append(f"- `{r['url']}` — NOTE h1 count = {a['h1_count']} (expected 1)")
        if not a["html_lang"]:
            notes_out.append(f"- `{r['url']}` — NOTE <html> missing lang")
        if not r["headers"].get("content-security-policy"):
            notes_out.append(f"- `{r['url']}` — NOTE missing CSP header")
        # praise
        if r["cold"]["load_ms"] < 400 and r["cold"]["transfer_kb"] < 200:
            praises.append(
                f"- `{r['url']}` — PRAISE fast+lean load={r['cold']['load_ms']}ms "
                f"transfer={r['cold']['transfer_kb']}KB"
            )

    md.append("## Findings")
    md.append("")
    md.append("### BLOCKER")
    md.extend(blockers if blockers else ["- (none)"])
    md.append("")
    md.append("### NOTE")
    md.extend(notes_out if notes_out else ["- (none)"])
    md.append("")
    md.append("### PRAISE")
    md.extend(praises if praises else ["- (none)"])
    md.append("")

    # Top 3 wins
    md.append("## Top 3 recommendations")
    md.append("")
    recs = []
    # Recommendation heuristics
    csp_missing = [r for r in runs if not r["headers"].get("content-security-policy")]
    if csp_missing:
        recs.append(
            f"1. **Add `content-security-policy` to public HTML responses** — "
            f"{len(csp_missing)}/{len(runs)} pages ship without CSP. Even a report-only "
            f"policy for static pages closes the biggest hygiene gap."
        )
    alt_pages = [r for r in runs if r["a11y"]["imgs_missing_alt"] > 0]
    if alt_pages:
        total_missing = sum(r["a11y"]["imgs_missing_alt"] for r in alt_pages)
        recs.append(
            f"{len(recs)+1}. **Backfill `alt` on {total_missing} <img> element(s) across "
            f"{len(alt_pages)} page(s)** — decorative uses `alt=\"\"`, content images get a real "
            f"description. Cheap a11y win."
        )
    heavy = [r for r in runs if r["cold"]["transfer_kb"] > 500]
    if heavy:
        heaviest = max(heavy, key=lambda r: r["cold"]["transfer_kb"])
        recs.append(
            f"{len(recs)+1}. **Trim payload on `{heaviest['url']}`** "
            f"(cold {heaviest['cold']['transfer_kb']} KB). Check for unused JS, uncompressed JSON, "
            f"and full-size PNGs that could be optimized or lazy-loaded."
        )
    slow = [r for r in runs if r["cold"]["load_ms"] > 2500]
    if slow and len(recs) < 3:
        slowest = max(slow, key=lambda r: r["cold"]["load_ms"])
        recs.append(
            f"{len(recs)+1}. **Investigate slow load on `{slowest['url']}`** "
            f"(cold Load={slowest['cold']['load_ms']}ms). "
            f"TTFB={slowest['cold']['ttfb_ms']}ms points at "
            f"{'app work' if slowest['cold']['ttfb_ms'] > 300 else 'downstream fetches / render'}."
        )
    if not recs:
        recs = ["1. Nothing critical — public pages are within budgets. Consider adding CSP once the app CSP nonce policy stabilises."]
    md.extend(recs)
    md.append("")

    md.append("## Raw data")
    md.append("")
    md.append("- `reports/perf-data.json` — full per-URL numbers, headers, a11y")
    md.append("- `reports/perf-shots/*.png` — one full-page screenshot per URL")
    md.append("")
    (REPORTS / "perf-report.md").write_text("\n".join(md), encoding="utf-8")
    print("wrote reports/perf-report.md")


if __name__ == "__main__":
    asyncio.run(main())
