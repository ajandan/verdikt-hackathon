"""Render the video's transparent overlay PNGs (stage timeline + model chips).

    python scripts/render_overlays.py

Outputs under assets/video/screenshots/{timeline,chips}/ — consumed by
scripts/assemble_video.sh and placed lower-left.
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

D = Path("assets/video/designs")
OUT = Path("assets/video/screenshots")
VARIANTS = ["none", "ingest", "triage", "decide", "deliver", "remember"]
CHIPS = {"chip-minicpm-v": "nv-ingest", "chip-qwen-embed": "nemo", "chip-nemotron": "nemotron"}


async def main() -> None:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=2)
        (OUT / "timeline").mkdir(parents=True, exist_ok=True)
        (OUT / "chips").mkdir(parents=True, exist_ok=True)
        for v in VARIANTS:
            await pg.goto((D / "stage-timeline.html").resolve().as_uri() + f"?active={v}", wait_until="networkidle")
            await pg.wait_for_timeout(400)
            await pg.locator("body").screenshot(path=str(OUT / "timeline" / f"timeline-{v}.png"), omit_background=True)
        await pg.goto((D / "nvidia-chips.html").resolve().as_uri(), wait_until="networkidle")
        await pg.wait_for_timeout(400)
        for cid, name in CHIPS.items():
            await pg.locator(f"#{cid}").screenshot(path=str(OUT / "chips" / f"{name}.png"), omit_background=True)
        await b.close()


asyncio.run(main())
