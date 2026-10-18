"""Render the YouTube thumbnail for the submission video.

YouTube wants 1280x720 (16:9), under 2 MB, and it will be read at roughly
210px wide in a list of search results. So the design is rendered at the house
1920x1080 stage size and downsampled with LANCZOS -- rendering straight to 1280x720 leaves the serif
wordmark's thin strokes ragged.

    python scripts/make_thumbnail.py

Writes submission/youtube-thumbnail.png (and .jpg, since YouTube's uploader
is happier with JPEG under its size cap). Edit
assets/video/designs/youtube-thumb.html and re-run.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

SRC = Path("assets/video/designs/youtube-thumb.html").resolve()
OUT_DIR = Path("submission")
TARGET = (1280, 720)
RENDER = (1920, 1080)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = OUT_DIR / ".thumb-2x.png"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": RENDER[0], "height": RENDER[1]})
        page.goto(f"file:///{SRC.as_posix()}")
        # Webfonts: the serif wordmark is the whole composition, so wait for
        # the font to actually load rather than screenshotting a fallback.
        page.wait_for_timeout(1200)
        page.screenshot(path=str(raw), clip={"x": 0, "y": 0,
                                             "width": RENDER[0], "height": RENDER[1]})
        browser.close()

    img = Image.open(raw).convert("RGB").resize(TARGET, Image.LANCZOS)
    png = OUT_DIR / "youtube-thumbnail.png"
    jpg = OUT_DIR / "youtube-thumbnail.jpg"
    img.save(png, optimize=True)
    img.save(jpg, quality=92, optimize=True, progressive=True)
    raw.unlink()

    for f in (png, jpg):
        kb = f.stat().st_size / 1024
        flag = "" if kb < 2048 else "  !! over YouTube's 2MB cap"
        print(f"[thumb] {f}  {img.size[0]}x{img.size[1]}  {kb:.0f} KB{flag}")


if __name__ == "__main__":
    main()
