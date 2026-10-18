"""Render the Devpost gallery thumbnail.

Devpost asks for JPG, PNG or GIF, 5 MB max, ideally 3:2. The design is laid out
at 1500x1000 in assets/video/designs/devpost-thumb.html and rendered at 2x, so
the output is 3000x2000 -- sharp on a retina display, still well under the cap.

    python scripts/make_devpost_thumb.py

Writes submission/devpost-thumbnail.png and .jpg. Upload the JPEG.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

SRC = Path("assets/video/designs/devpost-thumb.html").resolve()
OUT = Path("submission")
CAP = 5 * 1024 * 1024


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "devpost-thumbnail.png"
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=2)
        page.goto(f"file:///{SRC.as_posix()}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)          # webfonts carry the design
        page.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": 1500, "height": 1000})
        b.close()
    im = Image.open(png).convert("RGB")
    jpg = OUT / "devpost-thumbnail.jpg"
    im.save(jpg, quality=90, optimize=True, progressive=True)
    for f in (png, jpg):
        size = f.stat().st_size
        ratio = im.width / im.height
        print(f"[devpost] {f}  {im.width}x{im.height}  ratio {ratio:.3f}  "
              f"{size / 1024 / 1024:.2f} MB  {'OK' if size < CAP and abs(ratio - 1.5) < 0.001 else 'CHECK'}")


if __name__ == "__main__":
    main()
