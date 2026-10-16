# Voice-over voice choice — Verdikt submission video

_Documents the voice actually used for `assets/video/audio/beat*.mp3`. The default in `scripts/generate_voiceover.py` is a free-tier fallback (Adam); the values below are what the submission audio was rendered with. Override via env vars — do not edit the script default._

## Voice

| Field | Value |
| --- | --- |
| **Name** | Nate — "Engaging and Confident Innovator" |
| **Voice ID** | `kTVcdVhzYgIburyGmKK0` |
| **Category** | `professional` (ElevenLabs Voice Library, paid tier only) |
| **Model** | `eleven_multilingual_v2` (required for `<break time="..." />` tag honoring) |

Nate was located via `GET /v2/voices?search=nate` on the ElevenLabs REST API. Register: warm, confident, mid-pace narrator — fits the "innovator explaining a product" tone the script leans into.

## Settings (final)

| Setting | Value | Env var | Reason |
| --- | --- | --- | --- |
| Stability | `0.5` | `ELEVENLABS_STABILITY` | Balanced — not overly monotone, not drifty. |
| Similarity boost | `0.75` | `ELEVENLABS_SIMILARITY` | Standard. Higher risks emphasis loss on `<break>` boundaries. |
| Style exaggeration | `0.0` | `ELEVENLABS_STYLE` | Narration register, not performance. |
| Speaker boost | `true` | `ELEVENLABS_SPEAKER_BOOST` | On. |
| Speed | `1.10` | `ELEVENLABS_SPEED` | Bumped from 1.05 → 1.10 to fit the ~180 s total budget in `scripts/assemble_video.sh`. |
| Output format | `mp3_44100_128` | (n/a — API default) | Matches downstream ffmpeg `-ar 44100` re-encode. |

## Invocation

```bash
export ELEVENLABS_API_KEY='sk_...'                  # never commit; rotate if leaked
export ELEVENLABS_VOICE_ID='kTVcdVhzYgIburyGmKK0'   # Nate
export ELEVENLABS_STABILITY=0.5
export ELEVENLABS_SIMILARITY=0.75
export ELEVENLABS_STYLE=0.0
export ELEVENLABS_SPEAKER_BOOST=true
export ELEVENLABS_SPEED=1.10

# All beats (fresh render)
python scripts/generate_voiceover.py --force

# Single beat (e.g. after editing Beat 0c copy)
python scripts/generate_voiceover.py --beat 0c --force
```

Then rebuild the affected video segment:

```bash
RENDER_BEATS="0c" bash scripts/assemble_video.sh
```

## Why not the previous voices

| Voice | ID | Why abandoned |
| --- | --- | --- |
| Adam | `pNInz6obpgDQGcFmaJgB` | Free-tier fallback; too flat / newsreader register. Kept as `DEFAULT_VOICE_ID` in the script so the code runs without a paid key. |
| Liam | `TX3LPaxmHKxFdv7VOQHJ` | Also free-tier; younger / social-media energy, misread the "regulated verticals" thesis as marketing hype. |

## Paid-tier caveat

Nate is a Voice Library "professional" voice — free-tier API keys return HTTP 401/403 with a `not entitled` message when hitting it. Anyone regenerating the audio must be on a paid ElevenLabs plan whose key has Voice Library access enabled.

If a contributor without a paid key needs to rebuild the video from scratch:
1. Fall back to Liam (`TX3LPaxmHKxFdv7VOQHJ`) via `ELEVENLABS_VOICE_ID=...`
2. Expect ~2 s of pacing drift per beat vs. the Nate cut — the `BEAT_DURATIONS` array in `assemble_video.sh` was tuned for Nate at speed 1.10 and will silence-pad or trim to fit.

## What NOT to change

- Don't edit `DEFAULT_VOICE_ID` in `scripts/generate_voiceover.py`. Keep it on the free-tier premade so the script is usable without a paid key. Override via env for the actual submission render.
- Don't lower `speed` below `1.10` without retuning `BEAT_DURATIONS` in `scripts/assemble_video.sh` — the timing budget is calibrated to Nate at 1.10.
- Don't switch to `eleven_turbo_v2` or `eleven_monolingual_v1` — those models silently ignore `<break time="..." />` tags and the applause-line pauses collapse.

## HeyGen voice clone — partial re-render, 2026-10-16

Beats `0a 0b 0c 0d 1 2 3 3b 4 5` were re-rendered from a HeyGen **instant voice
clone** via `scripts/generate_voiceover_heygen.py`. Beats `6 7 8` are **still
ElevenLabs Nate** — the account ran out of `api` credits mid-run. The cut is
therefore mixed-voice and must not be published until those three are redone.

| Field | Value |
| --- | --- |
| **Voice ID** | `0188c9a3ecbe4b80ac4bbd73218161b3` |
| **Kind** | instant voice clone, `status: complete`, `support_pause: true` |
| **Endpoint** | `POST /v3/voices/speech` |
| **Engine** | `orca` (Starfish) — the engine that speaks this clone *with* break tags |
| **Speed** | `1.25` (`HEYGEN_SPEED`) |

### Why `/v3/voices/speech` and not `/v3/models/audio/tts`

The model route looks like the natural home for a clone and appears to draw on
a separate free pool, but it rejects our text outright:

    400 invalid_parameter: break tags are not supported for instant voice clones

The `<break time="Xs" />` tags are the timing skeleton the whole picture hangs
off, so that route is unusable here regardless of price. `/v3/voices/speech`
honours them and bills `api` credits — roughly 2 per call, ~0.17 per second of
audio. The 13-beat script needs about 25.

### Speed 1.25 was chosen to match the old pacing

HeyGen is *slower* per word than ElevenLabs but compresses much better, and
break tags do not scale with `speed`. Measured on beat 0a: 1.1 → 4.05 s,
1.25 → 3.58 s, 1.4 → 3.21 s. At 1.25 the ten re-rendered beats come in at
129.18 s against ElevenLabs' 148.51 s — a 0.87 ratio, projecting ~155 s
(2:35) for the full thirteen against the previous 178 s. That buys back ~23 s
of headroom under the 3:00 submission cap, and means `BEAT_TEMPO` in
`scripts/assemble_video.sh` can probably go back to a flat 1.0 once beats
6-8 exist and the real total is known.

### Word timestamps replace forced alignment

The response carries `word_timestamps`, and the break tags come back in that
list as timed tokens. `scripts/generate_voiceover_heygen.py` saves them to
`assets/video/audio/timings/beat{N}.json`. This is strictly better than the
old method: `silencedetect` found phrase *boundaries* but never said which
phrase sat in which gap, and reading them by eye is what put the beat 5
labeler clip on top of the line it was moved to expose. Retime from these
files, not from pauses.

**Re-timing is still owed.** Every visual cue is timed to the ElevenLabs audio
and is now wrong for the ten re-rendered beats. Known from `beat5.json`:
`"Deliver"` is at 1.88-2.15 (was 2.16) and `"Nothing leaves until a human
approves."` at 6.07-7.49 (was 7.14-8.98), so `BEAT_TIMELINE_IN[5]` and the
labeler-clip window both move again.
