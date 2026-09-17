# Assembling the submission video

Stitches ElevenLabs voice-over audio + Beat 2 Playwright captures + placeholder cards for the motion-graphics beats into a single MP4 that matches the timecodes in [`video-3min.md`](video-3min.md).

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

Reads all inputs under `assets/video/`, writes eight per-beat MP4s under `assets/video/segments/`, concatenates them into `assets/video/verdikt-submission.mp4` (~2:55, 1920×1080, H.264 + AAC 192kbps stereo).

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

## Beat timing (why exactly 2:55)

The script uses **fixed** beat durations from the voice-over script. Audio longer than a beat's target is trimmed; shorter audio is silence-padded. This keeps total run-time deterministic regardless of ElevenLabs pacing drift.

| Beat | Start | Duration |
| :-: | :-: | :-: |
| 1 | 0:00 | 18 s |
| 2a | 0:18 | 14 s |
| 2b | 0:32 | 22 s |
| 2c | 0:54 | 20 s |
| 2d | 1:14 | 18 s |
| 3 | 1:32 | 52 s |
| 4 | 2:24 | 16 s |
| 5 | 2:40 | 15 s |
| **Total** | | **175 s = 2:55** |

If a beat's audio is more than 0.3 s over its target, the script prints a stderr warning. Regenerate that beat in ElevenLabs with tighter pacing (slightly lower Stability, or trim break-tag durations) rather than letting the trim clip the tail.

## Iteration workflow

1. Run the recorder once to produce initial Beat 2 captures (`python scripts/record_demo.py`).
2. Run the assembler with only the audio and captures you have so far — placeholder cards fill the gaps. Watch the output in VLC / any player.
3. Regenerate any beat that isn't landing — audio in ElevenLabs, capture via `--beat 2c --headed` — then re-run the assembler with `RENDER_BEATS="<beat>"` to update just that segment.
4. When every beat lands, delete `assets/video/segments/` and run a clean full build for the final upload.

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

- **Add motion graphics.** Beats 1, 3, 4, 5 render as placeholder cards. Build the polished visuals in a video editor (or produce them as separate MP4/WEBM clips and drop them in `assets/video/captures/` with matching `beat{N}.webm` names — the assembler will use them).
- **Color-grade or normalize audio.** Every beat is encoded at CRF 20 with AAC 192 kbps; if you need more, run a mastering pass over `verdikt-submission.mp4` in your editor of choice.
- **Upload.** YouTube + Devpost are still manual.
