"""Capture the console screenshots shown in the landing page's carousel.

The carousel on `/` cycles through nine console screens served from
src/docgen/ui/static/console-*.png. They were taken by hand in September and
went stale as the UI and the demo data moved on. This re-takes all nine from
the running stack at a fixed 1440x900 viewport and 2x scale, so every frame is
exactly 2880x1800 -- the same 16:10 shape the landing page reserves for them.

    python scripts/capture_console_shots.py

Each frame is a viewport shot, not a full page: the carousel shows one screen
at a time at a fixed ratio, so a long page has to be framed, not shrunk.
Before writing anything, every page's rendered text is checked for personal
addresses, real company names and API-key prefixes; a hit aborts the run so a
stale or unscrubbed database cannot leak into the public landing page.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8100"
CONTAINER = "quire-hackathon-web-1"
REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "docgen" / "ui" / "static"

# QC-0009 (approve, every rule-relevant field read correctly) and the rule that
# decided it, so the carousel tells one consistent story.
CASE = "b8290139-1d11-4dd8-a09a-d9c02823d75b"
RULE = "e6d440f1-1ac5-448c-9445-5124bf16c11f"   # CV-CLIN-01-cardio
# Pages that take ?tenant= are framed on the Alderwood demo tenant. The dev
# database also holds test-suite fixture tenants and failed render jobs from
# development; they are real data, but not what the landing page should show.
ALDERWOOD = "7b473f7c-78a1-4af6-9d85-809e1be8e485"
T = f"?tenant={ALDERWOOD}"

# name -> (path, heading to scroll to or None for the top of the page)
SHOTS = {
    "console-dashboard":   (f"/ui/dashboard{T}", None),
    "console-inbox":       (f"/ui/inbox{T}", None),
    "console-pipeline":    (f"/ui/inbox/{CASE}/pipeline", None),
    "console-audit":       (f"/ui/inbox/{CASE}/audit", None),
    # Alderwood has no render jobs, so tenant-framing left an empty state;
    # succeeded jobs across tenants show the page doing its job without the
    # failed rows from development.
    "console-renders":     ("/ui/renders?state=succeeded", None),
    "console-programs":    ("/ui/programs", None),
    "console-rules":       ("/ui/rules", None),
    "console-rule-detail": (f"/ui/rules/{RULE}", None),
    # The review queue replaced the tenants list: it backs the human-review claim,
    # and the tenants list has no filter, so it showed every test fixture.
    "console-humanloop":   (f"/ui/inbox/humanloop{T}", None),
}

# Personal addresses and API-key prefixes are always refused. Organisation
# names to refuse come from DOCGEN_SHOT_DENYLIST (comma-separated) rather than
# being written here: a public repo whose rule is "no real company names"
# should not spell them out in its own guard.
_DENY = [w.strip() for w in os.environ.get("DOCGEN_SHOT_DENYLIST", "").split(",") if w.strip()]
_ALWAYS = r"@gmail\.com|dgk_[A-Za-z0-9]+"
LEAK = re.compile(
    _ALWAYS + (r"|\b(?:" + "|".join(re.escape(w) for w in _DENY) + r")\b" if _DENY else ""),
    re.I,
)


def mint_session() -> str:
    out = subprocess.check_output(
        ["docker", "exec", CONTAINER, "python", "-c",
         "from docgen.auth import sign_session; print(sign_session('admin'))"]
    )
    return out.decode().strip().splitlines()[-1]


def main() -> None:
    token = mint_session()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        ctx.add_cookies([
            {"name": "docgen_session", "value": token, "url": BASE},
            {"name": "docgen_csrf", "value": "t", "url": BASE},
        ])
        page = ctx.new_page()

        # Pass 1: check every page before overwriting any image.
        for name, (path, _) in SHOTS.items():
            resp = page.goto(f"{BASE}{path}", wait_until="networkidle")
            if not resp or resp.status >= 400:
                sys.exit(f"[console] {path} -> HTTP {resp.status if resp else '?'}; nothing written")
            hits = sorted(set(m.group(0) for m in LEAK.finditer(page.content())))
            if hits:
                sys.exit(f"[console] {path} shows {hits}; nothing written")

        # Pass 2: shoot.
        for name, (path, _) in SHOTS.items():
            page.goto(f"{BASE}{path}", wait_until="networkidle")
            page.wait_for_timeout(500)
            out = STATIC / f"{name}.png"
            page.screenshot(path=str(out))          # viewport only: 2880x1800
            print(f"[console] {out.name:28s} {path}")
        browser.close()
    print(f"[console] wrote {len(SHOTS)} frames to {STATIC}")


if __name__ == "__main__":
    main()
