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

## HeyGen voice clone — evaluated and reverted, 2026-10-16

**The submission audio is ElevenLabs Nate, as documented above.** A HeyGen
instant voice clone (`0188c9a3ecbe4b80ac4bbd73218161b3`) was rendered for ten
of the thirteen beats and then reverted, because the account ran out of credit
before beats 6, 7 and 8 and a mixed-voice cut is not shippable. Nothing in the
build pipeline was retimed, so `assets/video/audio/` and every cue in
`scripts/assemble_video.sh` are back in their original, consistent state.

The render is parked at `assets/video/audio-heygen-unused/` (ten mp3s plus
`timings/`), and `scripts/generate_voiceover_heygen.py` still works. To pick it
back up, top up credit and generate beats `6 7 8` at the same speed — the other
ten are cached server-side and re-request free.

### What it cost

Credits are a $0.02 view of a USD wallet (`billing_type: wallet`). A
`/v3/voices/speech` call is a flat 2 credits — **$0.04 regardless of length**.
The run spent **$0.48**: $0.36 on nine billed beat calls (the tenth was a cache
hit), $0.12 on three speed probes. A full thirteen-beat render is ~$0.52.

Check the wallet with `GET /v3/users/me`, not the credit counts on the
deprecated `GET /v2/user/remaining_quota`. The v2 view reports
`tts_free_credit: 180` and `tts_shared_free_ms: 600000` beside the real `api`
balance; those pools belong to `/v3/models/audio/tts` and are not spendable on
the only route that works here, so the v2 view reads as a free allowance that
does not exist.

### `/v3/models/audio/tts` cannot be used for this script

It is the documented home for a clone and it does not bill `api` credits, but:

    400 invalid_parameter: break tags are not supported for instant voice clones

The `<break time="Xs" />` tags in `voiceover-elevenlabs.md` are the timing
skeleton the whole picture hangs off. The route also ignores `speed` for
instant voices. `/v3/voices/speech` honours breaks, renders the clone on the
`orca` (Starfish) engine, and bills `api`.

### Worth keeping: speech APIs can return word timings

`/v3/voices/speech` returns `word_timestamps` with the break tags included as
timed tokens, which is strictly better than the method this project has been
using. `silencedetect` finds phrase *boundaries* but never says which phrase
sits in which gap, and reading them by eye is what put the beat 5 labeler clip
on top of the line it had been moved to expose. ElevenLabs offers the same
thing through `POST /v1/forced-alignment` on an existing mp3 — no re-render and
no voice change needed. Time cues off one of those, not off pauses. The parked
`timings/*.json` show the shape of the data.

### If the clone is ever revisited

HeyGen at speed 1.25 ran *shorter* than ElevenLabs: 129.18 s across the ten
beats against 148.51 s, a 0.87 ratio projecting ~155 s (2:35) for all thirteen
against the current 178 s. Breaks do not scale with `speed` (beat 0a: 1.1 →
4.05 s, 1.25 → 3.58 s, 1.4 → 3.21 s), so short beats compress worst — do not
extrapolate a total from one of them. The headroom would let `BEAT_TEMPO` go
back to a flat 1.0. All of it would need a full retime.
