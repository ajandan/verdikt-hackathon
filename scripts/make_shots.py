"""Cut narration-synced close-up shots out of each 2x-rendered beat slide.

The hand-composed beats carry a lot of small type (audit rows, mock letter
bodies, stage cards). At 1920x1080 on YouTube — especially at 720p or on a
phone — that type is unreadable, so the slide ends up as decoration while the
voice does all the work. This script cuts each slide into a short sequence of
16:9 close-ups timed to the narration, so the screen shows the thing the voice
is naming at the moment it names it.

Crops come out of beat{N}_primary@2x.png (3840x2160), so a half-width crop is
still 1:1 pixels at 1080p — genuinely sharp, not upscaled.

    python scripts/render_designs.py --beat 7 --scale 2    # source
    python scripts/make_shots.py                           # cut the shots

Writes assets/video/screenshots/shots/beat{N}_shot{k}.png plus a beat{N}.txt
manifest (transition + per-input durations) that scripts/assemble_video.sh
reads to build the beat as a cross-dissolved sequence.

Rects are authored in 1x slide coordinates (0..1920 x 0..1080) and come from
the real element geometry of each design. `None` means the full slide.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

SRC = Path("assets/video/screenshots")
OUT = SRC / "shots"
CANVAS = (1920, 1080)
TRANSITION = 0.4  # cross-dissolve between shots, seconds

# beat -> (total beat duration, [(rect_or_None, cut_time), ...])
# cut_time is when this shot hands over to the next; the last shot runs to the
# end of the beat. Times are tuned to docs/demo-scripts/voiceover-elevenlabs.md.
SHOTS: dict[str, tuple[float, list]] = {
    # "Thousands apply..." / "Each response, a compliance-audited letter." / "Today it's siloed"
    "0b": (12.00, [
        (None, 3.8),
        ((120, 412, 468, 457), 6.0),    # applicant card + caption
        ((648, 400, 624, 470), 8.4),    # fanned document stack
        ((1332, 412, 468, 457), 10.4),  # coordinator card
        (None, None),
    ]),
    # "You could throw an agent at it." / "must reproduce bit-for-bit, years later" / "So we draw a line"
    "0c": (13.85, [
        (None, 3.0),
        ((120, 314, 744, 646), 7.8),    # greyed-out agent-swarm column
        ((944, 314, 856, 646), None),   # the Verdikt approach column
    ]),
    # "Every channel fans into one pipeline." / "Five named stages" / "Every outcome traced."
    "0d": (9.70, [
        (None, 2.4),
        ((90, 470, 1035, 430), 5.2),    # Ingest / Triage / Decide
        ((960, 470, 870, 430), 7.6),    # Deliver / Remember
        (None, None),
    ]),
    # "an enrollment form, pay stubs, an insurance card, a prescription"
    "1": (12.35, [
        (None, 3.2),
        ((315, 291, 576, 668), 5.4),    # enrollment form
        ((1149, 470, 673, 217), 6.8),   # pay stub
        ((1147, 268, 675, 200), 8.0),   # insurance card
        ((1147, 695, 675, 282), 9.8),   # prescription pad
        (None, None),
    ]),
    # "the outcome letter, an appeals notice, a benefits guide, the blank forms"
    "3": (16.05, [
        (None, 2.6),
        ((94, 396, 419, 466), 4.6),     # outcome letter
        ((535, 399, 412, 461), 6.4),    # appeals notice
        ((974, 400, 411, 459), 8.2),    # benefits guide
        ((1409, 397, 417, 465), 10.2),  # follow-up form
        (None, None),
    ]),
    # "Approve — ..." / "Deny — ..." / "Missing information — ..." / "operator declares the mapping"
    "3b": (18.90, [
        (None, 3.2),
        ((120, 299, 1180, 210), 7.6),   # approve row
        ((120, 481, 1180, 210), 12.0),  # deny row
        ((120, 663, 1180, 210), 16.2),  # missing_info row
        (None, None),
    ]),
    # "A second program. A stricter income cap." / "Same code. Opposite verdict."
    "6": (10.65, [
        (None, 3.6),
        ((154, 391, 672, 546), 6.2),    # Cardiovita APPROVE card
        ((1094, 391, 673, 547), 8.8),   # OncoBridge DENY card
        (None, None),
    ]),
    # "AI events, human approvals, system deliveries — all stamped" / "reproducible, years later"
    # The row's event column ends at x=955, so both crops stop there rather than
    # slicing through the detail column mid-word. Shot 2 -> 3 reads as the log
    # scrolling; the hashes are read in context on the closing full-slide shot.
    "7": (10.80, [
        (None, 2.6),
        ((108, 350, 804, 250), 5.4),    # timestamp + actor + event, upper rows
        ((108, 580, 804, 250), 8.2),    # same framing, lower rows
        (None, None),
    ]),
}


def to_crop(rect, pad: float = 0.06) -> tuple[int, int, int, int]:
    """Grow a content rect by `pad`, square it off to 16:9, clamp to the slide."""
    if rect is None:
        return (0, 0, *CANVAS)
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    w, h = w * (1 + pad * 2), h * (1 + pad * 2)
    if w / h < CANVAS[0] / CANVAS[1]:
        w = h * CANVAS[0] / CANVAS[1]
    else:
        h = w * CANVAS[1] / CANVAS[0]
    if w > CANVAS[0]:
        w, h = CANVAS
    x = min(max(cx - w / 2, 0), CANVAS[0] - w)
    y = min(max(cy - h / 2, 0), CANVAS[1] - h)
    return round(x), round(y), round(w), round(h)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for beat, (total, shots) in SHOTS.items():
        src = SRC / f"beat{beat}_primary@2x.png"
        if not src.exists():
            print(f"skip beat{beat} — {src} missing (run render_designs.py --scale 2)")
            continue
        img = Image.open(src).convert("RGB")
        scale = img.width / CANVAS[0]

        cuts = [c for _, c in shots]
        prev = 0.0
        manifest = [f"T {TRANSITION}"]
        for i, (rect, cut) in enumerate(shots):
            end = total if cut is None else cut
            # First input runs to its cut; every later one carries an extra
            # transition's worth so the chained xfades land back on `total`.
            dur = (end - prev) if i == 0 else (end - prev) + TRANSITION
            prev = end

            x, y, w, h = to_crop(rect)
            box = (round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale))
            shot = img.crop(box).resize(CANVAS, Image.LANCZOS)
            name = f"beat{beat}_shot{i + 1}.png"
            shot.save(OUT / name)
            manifest.append(f"{name} {dur:.2f}")
            zoom = CANVAS[0] / w
            print(f"beat{beat} shot{i + 1}: {dur:5.2f}s  zoom {zoom:.2f}x  crop {x},{y} {w}x{h}")

        if cuts[-1] is not None:
            print(f"  warn: beat{beat} last shot has an explicit cut; it will run to {total}s anyway")
        # newline="\n" — the bash assembler reads this with `read`, and a CRLF
        # would leave a stray \r glued to each duration.
        with open(OUT / f"beat{beat}.txt", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(manifest) + "\n")


if __name__ == "__main__":
    main()
