"""Generate the site favicons from the Verdikt mark.

Writes, under src/docgen/ui/static/:
  favicon.svg           modern browsers; scales to any size
  favicon.ico           16/32/48 px, for /favicon.ico and older browsers
  apple-touch-icon.png  180 px, iOS home screen (iOS rounds the corners itself)

The mark is the same paths the admin pages inline. It sits on a paper tile
because the bare dark-green strokes all but disappear in a dark browser tab.

    python scripts/make_favicons.py
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "src" / "docgen" / "ui" / "static"

MARK = ('<g transform="translate(1.5 3.2) scale(0.9)" fill="none" stroke-width="6" stroke-linecap="square">'
        '<path d="M38 32C28 32 28 13 18 13H8" stroke="#5fa58f"/>'
        '<path d="M38 32C28 32 28 51 18 51H8" stroke="#8ec7b5"/>'
        '<path d="M8 32H42" stroke="#1e5f4f"/>'
        '<circle cx="51" cy="32" r="9" stroke="#1e5f4f"/>'
        '<circle cx="51" cy="32" r="3.5" fill="#3cb371" stroke="none"/></g>')


def svg(rounded: bool) -> str:
    rx = ' rx="14"' if rounded else ""
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
            f'<rect width="64" height="64"{rx} fill="#f6f5f1"/>{MARK}</svg>')


def raster(page, markup: str, size: int) -> Image.Image:
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(f'<html><body style="margin:0;background:transparent">'
                     f'{markup.replace("width=\"64\" height=\"64\"", f"width=\"{size}\" height=\"{size}\"")}'
                     f'</body></html>')
    png = page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main() -> None:
    (STATIC / "favicon.svg").write_text(svg(rounded=True), encoding="utf-8", newline="\n")
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        big = raster(page, svg(rounded=True), 256)
        big.save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        raster(page, svg(rounded=False), 180).save(STATIC / "apple-touch-icon.png", optimize=True)
        b.close()
    for n in ("favicon.svg", "favicon.ico", "apple-touch-icon.png"):
        print(f"[favicons] {n:22s} {(STATIC / n).stat().st_size:6d} bytes")


if __name__ == "__main__":
    main()
