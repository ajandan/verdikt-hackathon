"""Render each assets/video/designs/beat{N}.html to a 1920x1080 PNG.

Uses Playwright's headless Chromium so Google Fonts + CSS render exactly as
in a real browser. Screenshots land at assets/video/screenshots/beat{N}_primary.png
which is what scripts/assemble_video.sh picks up as the beat's visual fill.

Usage:
    python scripts/render_designs.py                # all 8 beats
    python scripts/render_designs.py --beat 3       # just one
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DESIGNS_DIR = Path("assets/video/designs")
OUTPUT_DIR = Path("assets/video/screenshots")
VIEWPORT = {"width": 1920, "height": 1080}


async def render_one(beat: str, scale: int = 1) -> None:
    html_file = DESIGNS_DIR / f"beat{beat}.html"
    suffix = "_primary" if scale == 1 else f"_primary@{scale}x"
    out_file = OUTPUT_DIR / f"beat{beat}{suffix}.png"
    if not html_file.exists():
        print(f"[render] skip beat{beat} — {html_file} missing", file=sys.stderr)
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=scale)
        page = await context.new_page()
        url = html_file.resolve().as_uri()
        print(f"[render] beat{beat}: {url}", file=sys.stderr)
        await page.goto(url, wait_until="networkidle")
        # Give web fonts a moment to finalize their metrics.
        await page.wait_for_timeout(600)
        await page.screenshot(path=str(out_file), full_page=False, omit_background=False)
        await browser.close()
    kb = out_file.stat().st_size / 1024
    print(f"[render]   wrote {out_file} ({kb:.1f} KB)", file=sys.stderr)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--beat", help="Render just one beat (e.g. 4). Default: all 1..8.")
    parser.add_argument("--scale", type=int, default=1,
                        help="Device scale factor. 2 writes beat{N}_primary@2x.png for shot cropping.")
    args = parser.parse_args()

    beats = [args.beat] if args.beat else [str(i) for i in range(1, 9)]
    for b in beats:
        await render_one(b, args.scale)


if __name__ == "__main__":
    asyncio.run(main())
