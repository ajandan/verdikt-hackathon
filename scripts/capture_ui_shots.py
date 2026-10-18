"""Capture the admin-UI tour used in the Devpost submission gallery.

The previous set was taken by hand (see reports/ui-count-audit.md) and then
scrubbed, because screenshots of a working system carry whatever data is on
screen. This script re-takes them from the running stack so the set can be
regenerated at any time -- after a UI change, or to prove the shots match the
code at a given commit.

    python scripts/capture_ui_shots.py

Writes reports/ui-audit-shots/NN-<page>.png.

Navigation is discovered from the rendered chrome rather than hardcoded, so a
page added to or removed from the nav is picked up without editing this file.
Auth uses the same session-minting trick the E2E script uses: sign a session
inside the container rather than driving the login form, which keeps the shots
free of credentials.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8100"
CONTAINER = "quire-hackathon-web-1"
REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "ui-audit-shots"

# Pages worth a gallery slot even if the nav does not surface them.
EXTRA = ["/ui/inbox/humanloop"]
# Never shoot these: destructive, or a credential form.
SKIP = re.compile(r"/(login|logout|upload)/?$|/delete|/revoke", re.I)


def mint_session() -> str:
    out = subprocess.check_output(
        ["docker", "exec", CONTAINER, "python", "-c",
         "from docgen.auth import sign_session; print(sign_session('admin'))"]
    )
    return out.decode().strip().splitlines()[-1]


def slug(path: str) -> str:
    s = path.replace("/ui", "").strip("/").replace("/", "-")
    return s or "templates"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()

    token = mint_session()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        ctx.add_cookies([
            {"name": "docgen_session", "value": token, "url": BASE},
            {"name": "docgen_csrf", "value": "t", "url": BASE},
        ])
        page = ctx.new_page()

        page.goto(f"{BASE}/ui/", wait_until="networkidle")
        hrefs = page.eval_on_selector_all(
            "nav a[href^='/ui'], header a[href^='/ui'], .nav a[href^='/ui']",
            "els => els.map(e => e.getAttribute('href'))",
        )
        seen, paths = set(), []
        for h in ["/ui/", *hrefs, *EXTRA]:
            h = (h or "").split("?")[0].rstrip("/") or "/ui"
            if h in seen or SKIP.search(h) or "{" in h:
                continue
            seen.add(h)
            paths.append(h)

        print(f"[shots] {len(paths)} pages discovered")
        n = 0
        for path in paths:
            resp = page.goto(f"{BASE}{path}", wait_until="networkidle")
            code = resp.status if resp else 0
            if code >= 400:
                print(f"[shots] skip {path} -> HTTP {code}")
                continue
            n += 1
            page.wait_for_timeout(500)
            name = f"{n:02d}-{slug(path)}.png"
            page.screenshot(path=str(OUT / name), full_page=True)
            print(f"[shots] {name:34s} {path}  HTTP {code}")
        browser.close()
    print(f"[shots] wrote {n} screenshots to {OUT}")


if __name__ == "__main__":
    main()
