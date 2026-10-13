# Assembling the submission video

Stitches ElevenLabs voice-over audio, hand-composed beat slides, and Playwright captures of the live UI into a single MP4 that matches the story in [`video-3min.md`](video-3min.md). Beats with none of these fall back to a placeholder card.

Driver: [`scripts/assemble_video.sh`](../../scripts/assemble_video.sh) — bash, uses `ffmpeg` + `ffprobe`. Works on Windows via git-bash / WSL and on Linux/macOS unchanged.

---

## Prerequisites

- `ffmpeg` and `ffprobe` on `PATH`. On Windows, grab a full build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or install via Chocolatey / Scoop. `ffmpeg -version` must show `libx264` in the enabled configuration.
- (Optional) A TrueType font at `C:/Windows/Fonts/arial.ttf` — or set `DEMO_FONT_PATH` to any `.ttf` you have. Only used for the placeholder cards; missing font falls back to a flat-color card.

## Expected input layout

```
assets/video/
├── audio/                    # ElevenLabs output — see voiceover-elevenlabs.md
│   ├── beat1.mp3
│   ├── beat2a.mp3
│   ├── beat2b.mp3
│   ├── beat2c.mp3
│   ├── beat2d.mp3
│   ├── beat3.mp3
│   ├── beat4.mp3
│   └── beat5.mp3
├── captures/                 # Playwright output — see playwright-recording.md
│   ├── beat2a.webm
│   ├── beat2c.webm
│   ├── beat2d.webm
│   └── beat2b_alt.webm       # optional; only used if beat2b.webm is absent
├── segments/                 # generated per-beat MP4s (git-ignored)
└── verdikt-submission.mp4    # final output
```

Every file is optional. Missing audio → silence for that beat (with a stderr warning). Missing capture → placeholder card. You can iterate on assembly before all inputs exist.

## Running

```bash
bash scripts/assemble_video.sh
```

Reads all inputs under `assets/video/`, writes one MP4 per beat under `assets/video/segments/`, then cross-dissolves them into `assets/video/verdikt-submission.mp4` (1920x1080, H.264 + AAC 192 kbps stereo) and loudness-normalises to -14 LUFS. Run-time is whatever the narration fits into — just under 3:00 at the time of writing.

**Just rebuild one beat** (keep the others from a previous run):

```bash
RENDER_BEATS="2c" bash scripts/assemble_video.sh
```

Only `beat2c.mp4` is re-rendered; the concat uses the existing segments for every other beat.

**Rebuild several:**

```bash
RENDER_BEATS="3 4 5" bash scripts/assemble_video.sh
```

**Point at different directories / output:**

```bash
DEMO_BASE_DIR=/tmp/verdikt-video DEMO_OUTPUT=/tmp/submission.mp4 bash scripts/assemble_video.sh
```

## Beat timing — fitted to the narration, not fixed

Each beat's slot is measured from its own audio rather than hand-set. For every
`beat*.mp3` the script finds where the **last spoken word ends** (silencedetect,
ignoring the trailing silence ElevenLabs leaves on every render) and sizes the
slot as:

```
slot = speech_end / tempo + TAIL_GAP     (rounded up to 50 ms)
```

`TAIL_GAP` is 0.40 s, so the voice never runs into the cut, and the per-beat
audio chain fades out over the last 300 ms — entirely inside that silence, so
the fade is inaudible and there is no click at the boundary.

`BEAT_TEMPO` carries a per-beat tempo multiplier for beats whose narration would
otherwise push the total over the 3:00 cap; 1.0 leaves the audio untouched. The
hand-set `BEAT_DURATIONS` array is now only the fallback for a beat that has no
audio yet. The script prints each beat's fitted slot and warns if the total
crosses 179.8 s.

## Close-up shots — off by default

`scripts/make_shots.py` can cut a slide into a sequence of 16:9 close-ups timed
to the narration, cropped out of `beat{N}_primary@2x.png` so they stay 1:1
pixels at 1080p rather than upscaling. It is **off**: the zoom reads as a camera
move the rest of the piece doesn't have, so every beat renders as its whole
slide.

```bash
USE_SHOTS=1 bash scripts/assemble_video.sh     # bring the close-ups back
```

With the flag on, any beat that has a `screenshots/shots/beat{N}.txt` manifest
is built as a chain of cross-dissolved stills; the manifest's durations already
carry the transition overlap, so the chain lands exactly on the beat's slot.
Rects live in `SHOTS` in `make_shots.py`, authored in 1x slide coordinates and
taken from the real element geometry of each design. Frame a crop so it **stops
at a column boundary** — cutting through a column mid-word reads as a broken
layout rather than a deliberate close-up.

## Transitions

Beat boundaries cross-dissolve rather than hard-cut (`BEAT_XFADE`, 0.30 s; set
it to 0 to restore hard cuts). Every segment after the first is padded at the
**start** with a frozen copy of its first frame, one transition long, and the
dissolve is laid over that pad — so a beat's own content still begins on its
original cut point and stays in sync with its narration. Audio is
plain-concatenated and never overlapped, which is safe because every beat
already ends in silence.

## Overlays

The five-stage pipeline pill sits **lower-left**, and it is deliberately not
persistent — `BEAT_TIMELINE` is empty for every beat that isn't walking through
a stage, so the pill reads as a stage marker instead of wallpaper. It shows on
beat 4 (sweeping ingest → triage → decide), beat 5 (*Deliver*) and beat 7
(*Remember*). `BEAT_TIMELINE_IN` holds it back a beat-relative number of seconds
so it can land on the spoken stage name rather than at the cut.

The model chips (beat 4) share that left edge, stacked just above the pill. Both
families are transparent PNGs rendered by `scripts/render_overlays.py` from
`stage-timeline.html` and `nvidia-chips.html`. `common.css` reserves the bottom
120 px of every hand-composed beat so slide content never collides with them.

## Iteration workflow

1. Run the recorder once to produce initial Beat 2 captures (`python scripts/record_demo.py`).
2. Run the assembler with only the audio and captures you have so far — placeholder cards fill the gaps. Watch the output in VLC / any player.
3. Regenerate any beat that isn't landing — audio in ElevenLabs, capture via `--beat 2c --headed` — then re-run the assembler with `RENDER_BEATS="<beat>"` to update just that segment.
4. After editing a beat's HTML, re-render the 1x slide (`--scale 1`). Only with
   `USE_SHOTS=1` do you also need `--scale 2` and a `make_shots.py` re-run, or
   the close-ups will still be cut from the previous design.
5. When every beat lands, delete `assets/video/segments/` and run a clean full build for the final upload.

> `RENDER_BEATS="none"` re-joins the existing segments without re-rendering any
> of them — useful when only the transition or the join step changed.

## Common failures

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ffmpeg not on PATH` | Install path missing from `PATH`. | Install ffmpeg from gyan.dev / apt / brew. Verify with `ffmpeg -version`. |
| Final duration ≠ 175 s | Segment corrupt or codec mismatch during concat. | Delete `assets/video/segments/` and re-run for a clean rebuild. |
| Placeholder card is a flat rectangle with no text | Font file missing. | Set `DEMO_FONT_PATH=/path/to/some.ttf` and re-run. |
| `beat*_alt.webm` isn't used even though it exists | You also have `beat2b.webm` — the primary wins. | Delete or rename `beat2b.webm` if you want the fallback used. |
| `Non-monotonous DTS` warnings during concat | Playwright WebM timestamps drifted. | Harmless — the concat still produces the correct output. If it doesn't, delete `segments/` and re-run to force clean encodes. |
| Audio 0.5 s late relative to the visual | ElevenLabs `<break>` tags stretched slightly, or the video capture started a beat early. | Verify with `ffprobe -show_packets` on the offending segment; usually a re-render with a shorter `<break>` fixes it. |

## What the assembly does NOT do

- **Animate anything beyond a cross-dissolve.** Beat visuals are whole stills and live-UI captures. For real motion, produce a clip and drop it in `assets/video/captures/` as `beat{N}.webm` — the assembler prefers it over the slide.
- **Color-grade or normalize audio.** Every beat is encoded at CRF 20 with AAC 192 kbps; if you need more, run a mastering pass over `verdikt-submission.mp4` in your editor of choice.
- **Upload.** YouTube + Devpost are still manual.
