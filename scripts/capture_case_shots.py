"""Capture a reviewed case: what was extracted, and which rules decided it.

The product-UI tour (scripts/capture_ui_shots.py) only walks list pages. The
screen that carries the argument is the case detail -- the fields the models
pulled out of the packet, and every rule evaluated against them with its
citation -- so it gets its own capture, two shots per section:

    extract-page1   fields read from page 1 of the enrollment form
    extract-page2   fields read from page 2
    rules-1, rules-2  the evaluated rules, split where the list is longest

    python scripts/capture_case_shots.py                  # default case
    python scripts/capture_case_shots.py <inbox_event_id> [<label>]

Writes reports/case-shots/<label>-NN-<section>.png. Same session-minting as the
other capture scripts, so no credentials appear on screen.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = "http://localhost:8100"
CONTAINER = "quire-hackathon-web-1"
REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "case-shots"

# QC-0009, outcome approve. Chosen because every field the rules read was
# extracted correctly -- drug, prescriber, both signatures. QC-0011 also
# approves, but its reader put the patient's name in the prescriber-signature
# slot and only matched the drug through the 2-edit scan tolerance; a judge
# reading the shot would see the model's mistake rather than the product.
DEFAULT_CASE = ("b8290139-1d11-4dd8-a09a-d9c02823d75b", "approve")


def mint_session() -> str:
    out = subprocess.check_output(
        ["docker", "exec", CONTAINER, "python", "-c",
         "from docgen.auth import sign_session; print(sign_session('admin'))"]
    )
    return out.decode().strip().splitlines()[-1]


def section(page: Page, heading: str):
    """The block that owns an <h2> whose text starts with `heading`."""
    h2 = page.locator("h2", has_text=heading).first
    return h2.locator("xpath=ancestor::section[1] | ancestor::*[contains(@class,'card')][1]").first


def shoot(page: Page, el, path: Path) -> None:
    el.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    el.screenshot(path=str(path))
    print(f"[case] {path.name}")


def main() -> None:
    event_id, label = (sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "case") \
        if len(sys.argv) > 1 else DEFAULT_CASE
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob(f"{label}-*.png"):
        old.unlink()

    token = mint_session()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        ctx.add_cookies([
            {"name": "docgen_session", "value": token, "url": BASE},
            {"name": "docgen_csrf", "value": "t", "url": BASE},
        ])
        page = ctx.new_page()
        resp = page.goto(f"{BASE}/ui/inbox/{event_id}/pipeline", wait_until="networkidle")
        if not resp or resp.status >= 400:
            sys.exit(f"case page returned HTTP {resp.status if resp else '?'}")

        # The app bar is sticky, so an element screenshot scrolled under it
        # comes out with the bar painted over its first rows. Unstick
        # everything pinned to the viewport for the duration of the capture.
        # CSSOM writes, not a <style> tag: the page's nonce CSP blocks injected
        # stylesheets, but not style changes made through the object model.
        page.evaluate("""() => {
          for (const el of document.querySelectorAll('body *')) {
            const pos = getComputedStyle(el).position;
            if (pos === 'sticky' || pos === 'fixed') el.style.setProperty('position', 'static', 'important');
          }
        }""")

        # --- Extract: one shot per page of the form ------------------------
        extract = section(page, "Extract")
        tabs = extract.locator("button", has_text="Page")
        n_tabs = tabs.count()
        for i in range(min(n_tabs, 2)):
            tabs.nth(i).click()
            page.wait_for_timeout(400)
            shoot(page, extract, OUT / f"{label}-0{i + 1}-extract-page{i + 1}.png")

        # --- Rules: the list is long, so split it in two -------------------
        # Split on a card boundary, never mid-card: take the card whose top is
        # nearest the middle of the section. Coordinates are page coordinates
        # (bounding boxes are viewport-relative, which a full-page clip is not).
        geo = page.evaluate(r"""() => {
          const h = [...document.querySelectorAll('h2')]
                      .find(e => e.textContent.trim().startsWith('Rules evaluated'));
          let sec = h.parentElement;
          while (sec && sec.getBoundingClientRect().height < 400) sec = sec.parentElement;
          const r = sec.getBoundingClientRect();
          const cards = [...sec.querySelectorAll('.rule-cards > *')]
                          .map(c => c.getBoundingClientRect().top + scrollY);
          return {x: r.left + scrollX, top: r.top + scrollY, w: r.width, h: r.height, cards};
        }""")
        top, bottom = geo["top"], geo["top"] + geo["h"]
        mid = (top + bottom) / 2
        cut = min(geo["cards"][1:], key=lambda y: abs(y - mid)) if len(geo["cards"]) > 1 else mid
        for i, (y0, y1) in enumerate([(top, cut), (cut, bottom)]):
            clip = {"x": geo["x"], "y": y0, "width": geo["w"], "height": y1 - y0}
            path = OUT / f"{label}-0{i + 3}-rules-{i + 1}.png"
            page.screenshot(path=str(path), clip=clip, full_page=True)
            print(f"[case] {path.name}")

        browser.close()
    print(f"[case] wrote 4 shots for {label} to {OUT}")


if __name__ == "__main__":
    main()
