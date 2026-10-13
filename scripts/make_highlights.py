"""Render the per-beat spotlight scrims that follow the narration.

A beat slide holds more than the voice is talking about at any one moment, so
the eye has nowhere to go. This builds, for each beat, a set of full-frame
transparent PNGs: everything dimmed except the element the script is naming,
with a soft rounded cut-out and a seal-green edge. The assembler fades them in
and out over the still, which gives the slide movement without a camera move.

Targets are CSS selectors resolved against the real design page, so a scrim can
never drift out of register with the layout — edit the HTML and re-run.

    python scripts/make_highlights.py            # all beats
    python scripts/make_highlights.py 1 3b       # just these

Writes assets/video/screenshots/highlights/beat{N}_hl{k}.png plus a beat{N}.txt
manifest (png, start, end, fade) read by scripts/assemble_video.sh.

TIMING: the windows below are not guesses. ElevenLabs renders every `<break>`
in docs/demo-scripts/voiceover-elevenlabs.md as real silence, so

    ffmpeg -i beatN.mp3 -af silencedetect=n=-42dB:d=0.18 -f null -

prints the exact boundary between one phrase and the next. Each window is taken
from that output and divided by the beat's BEAT_TEMPO (assemble_video.sh speeds
a few beats by 3% to fit the 3:00 cap), which is what `tempo` carries here.
Re-run silencedetect after regenerating any audio.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from playwright.sync_api import sync_playwright

DESIGNS = Path("assets/video/designs").resolve()
OUT = Path("assets/video/screenshots/highlights")
CANVAS = (1920, 1080)

DIM = 0.42          # how far the un-named part of the slide falls back
PAD = 16            # breathing room around the target element
RADIUS = 18
FEATHER = 7         # px of blur on the cut-out edge
RING = (47, 143, 118, 150)   # seal-500, the design-system accent
RING_W = 3
FADE = 0.22         # seconds; clamped to a third of the window for fast cuts
CHAIN_GAP = 0.50    # moments closer than this hand over instead of letting go

# beat -> (tempo, [(selectors, start, end), ...])
# A selector is "css" or "css@n" for the nth match; several in one moment punch
# several holes in the same scrim. Times are seconds into the beat.
MOMENTS: dict[str, tuple[float, list]] = {
    # "Thousands apply..." / "Each response, a compliance-audited letter."
    # / "Today it's siloed — every rule change waits on developers."
    "0b": (1.0, [
        (["div.col-scene@0"], 0.80, 4.40),
        (["div.col-scene@1"], 4.73, 7.70),
        (["div.col-scene@2"], 8.04, 11.50),
    ]),
    # "You could throw an agent at it." / "...reproduce bit-for-bit, years
    # later. Most AI can't." / "So we draw a line — AI where it earns its place"
    "0c": (1.0, [
        (["div.col-agent"], 0.40, 4.70),
        (["div.strike-line"], 5.24, 7.40),
        (["div.col-ours"], 8.02, 13.50),
    ]),
    # "Every channel fans into one pipeline." / "Five named stages —" / each
    # stage named in turn / "Every outcome traced."
    "0d": (1.03, [
        (["div.intake-strip"], 0.30, 1.90),
        (["div.stages"], 2.32, 3.19),
        (["div.stage-col@0"], 3.69, 4.20),
        (["div.stage-col@1"], 4.66, 5.25),
        (["div.stage-col@2"], 5.65, 6.16),
        (["div.stage-col@3"], 6.55, 6.98),
        (["div.stage-col@4"], 7.30, 7.80),
        (["div.outcome-strip"], 8.15, 9.25),
    ]),
    # "an enrollment form, pay stubs, an insurance card, a prescription"
    "1": (1.0, [
        (["div.form-frame"], 3.82, 5.35),
        (["div.paystub"], 5.59, 6.40),
        (["div.ins-card"], 6.69, 7.75),
        (["div.rx-pad"], 8.01, 8.95),
    ]),
    # "the outcome letter, an appeals notice, a benefits guide, the blank forms"
    # / "Every one is a human-approved template. Once frozen..."
    "3": (1.03, [
        (["div.pdf-card@0"], 4.30, 5.30),
        (["div.pdf-card@1"], 5.53, 6.60),
        (["div.pdf-card@2"], 6.88, 7.95),
        (["div.pdf-card@3"], 8.21, 10.30),
        (["div.package-callout"], 10.95, 15.60),
    ]),
    # "Approve — ..." / "Deny — ..." / "Missing information — ..."
    # / "The operator declares the mapping."
    "3b": (1.03, [
        (["div.verdict-pill.approve", "div.pkg-row.approve"], 3.41, 7.60),
        (["div.verdict-pill.deny", "div.pkg-row.deny"], 7.88, 10.80),
        (["div.verdict-pill.missing", "div.pkg-row.missing"], 11.00, 16.30),
        (["div.operator-band"], 16.82, 18.60),
    ]),
    # "The pipeline is the product, not the program." / "A second program. A
    # stricter income cap." / "Same email. Same code." / "Opposite verdict."
    "6": (1.0, [
        (["div.pipeline-mini"], 0.30, 2.50),
        (["div.verdict-side.right"], 3.80, 6.90),
        (["div.center-col"], 7.23, 9.00),
        (["div.verdict-card-lg@0", "div.verdict-card-lg@1"], 9.33, 10.50),
    ]),
    # "AI events, human approvals, system deliveries — all stamped, all
    # reproducible from the audit log, years later."
    "7": (1.0, [
        (["div.row@1", "div.row@2", "div.row@3"], 2.53, 3.60),
        (["div.row@4"], 3.77, 4.70),
        (["div.row@5", "div.row@6"], 4.93, 5.90),
        (["div.row .hash"], 6.01, 6.85),
        (["div.foot"], 7.18, 9.60),
    ]),
}


def resolve(page, selectors: list[str]) -> list[tuple[int, int, int, int]]:
    """Turn selectors into padded boxes. "sel@n" picks the nth match; a bare
    selector with several matches is taken as all of them."""
    rects = []
    for spec in selectors:
        sel, _, idx = spec.partition("@")
        boxes = page.query_selector_all(sel)
        if not boxes:
            raise SystemExit(f"selector matched nothing: {spec}")
        chosen = [boxes[int(idx)]] if idx else boxes
        for el in chosen:
            b = el.bounding_box()
            rects.append((
                max(0, round(b["x"]) - PAD),
                max(0, round(b["y"]) - PAD),
                min(CANVAS[0], round(b["x"] + b["width"]) + PAD),
                min(CANVAS[1], round(b["y"] + b["height"]) + PAD),
            ))
    return rects


def scrim(rects) -> Image.Image:
    """Full-frame dim with the target rects cut out and ringed."""
    hole = Image.new("L", CANVAS, 0)
    pen = ImageDraw.Draw(hole)
    for r in rects:
        pen.rounded_rectangle(r, RADIUS, fill=255)
    hole = hole.filter(ImageFilter.GaussianBlur(FEATHER))

    img = Image.new("RGBA", CANVAS, (10, 10, 8, round(DIM * 255)))
    # Alpha falls to zero inside the hole, feathered at its edge.
    img.putalpha(Image.eval(hole, lambda v: round((255 - v) * DIM)))

    ring = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    pen = ImageDraw.Draw(ring)
    for r in rects:
        pen.rounded_rectangle(r, RADIUS, outline=RING, width=RING_W)
    return Image.alpha_composite(img, ring)


def main() -> None:
    wanted = sys.argv[1:] or list(MOMENTS)
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": CANVAS[0], "height": CANVAS[1]})
        for beat in wanted:
            tempo, moments = MOMENTS[beat]
            src = DESIGNS / f"beat{beat}.html"
            if not src.exists():
                print(f"skip beat{beat} — {src} missing")
                continue
            page.goto(f"file:///{src.as_posix()}")
            page.wait_for_timeout(700)

            # Audio times are measured on the mp3; the assembler plays a few
            # beats 3% fast, so the picture has to meet them there.
            specs = []
            for selectors, start, end in moments:
                s, e = start / tempo, end / tempo
                specs.append([selectors, s, e, min(FADE, (e - s) / 3)])

            # When one phrase runs straight into the next, hold the first scrim
            # until the second has faded up. Otherwise the slide flashes back to
            # full brightness between two spotlights — five times in four
            # seconds on beat 0d, which strobes. Chained, the hole just slides.
            for cur, nxt in zip(specs, specs[1:]):
                if nxt[1] - cur[2] < CHAIN_GAP:
                    cur[2] = nxt[1] + cur[3]

            manifest = []
            for k, (selectors, s, e, fade) in enumerate(specs, 1):
                rects = resolve(page, selectors)
                name = f"beat{beat}_hl{k}.png"
                scrim(rects).save(OUT / name)
                manifest.append(f"{name} {s:.2f} {e:.2f} {fade:.2f}")
                print(f"beat{beat} hl{k}: {s:5.2f}-{e:5.2f}s  {len(rects)} target(s)  {selectors}")

            with open(OUT / f"beat{beat}.txt", "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(manifest) + "\n")
        browser.close()


if __name__ == "__main__":
    main()
