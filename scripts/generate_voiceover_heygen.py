"""Generate submission-video voice-over MP3s via the HeyGen speech API.

The sibling scripts/generate_voiceover.py does the same job against ElevenLabs.
Both read the beat text out of docs/demo-scripts/voiceover-elevenlabs.md, which
stays the single source of truth — edit the markdown, re-run one of these.

Why this one exists: HeyGen's POST /v3/voices/speech returns `word_timestamps`
alongside the audio, and the break tags come back in that list as timed tokens.
That is the exact thing the picture needs. Timing visuals off `silencedetect`
finds phrase *boundaries* but cannot say which phrase sits in which gap, and
reading them by eye put the beat 5 labeler clip on top of the very line it was
moved to expose. Here the answer is in the response, so no guessing step exists
to get wrong. Timings land in audio/timings/beat{N}.json next to the mp3.

SECURITY: pass the key via HEYGEN_API_KEY. Never bake it into a committed file.

Usage:
    export HEYGEN_API_KEY=sk_V2_...
    export HEYGEN_VOICE_ID=<32-hex voice id>
    python scripts/generate_voiceover_heygen.py                  # all beats
    python scripts/generate_voiceover_heygen.py --beat 5         # just one
    python scripts/generate_voiceover_heygen.py --dry-run        # no API calls
    python scripts/generate_voiceover_heygen.py --speed 1.3

Env vars:
    HEYGEN_API_KEY      required
    HEYGEN_VOICE_ID     required
    HEYGEN_SPEED        optional, default 1.25 (0.5-2.0)
    HEYGEN_ENGINE       optional, omitted = the voice's saved default
    DEMO_AUDIO_DIR      optional, default assets/video/audio

Speed is the lever that keeps the cut under the 3:00 submission cap. HeyGen's
own speed control re-renders the prosody, which sounds better than squeezing
the finished mp3 with atempo, so spend it here first and leave BEAT_TEMPO in
assemble_video.sh for the last percent or two.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from generate_voiceover import DEFAULT_VOICEOVER_MD, parse_beats

API_URL = "https://api.heygen.com/v3/voices/speech"
DEFAULT_SPEED = 1.25

AUDIO_DIR = Path(os.environ.get("DEMO_AUDIO_DIR", "assets/video/audio"))
TIMINGS_DIR = AUDIO_DIR / "timings"


def synthesize(text: str, voice_id: str, speed: float, engine: str | None, api_key: str) -> dict:
    """Ask HeyGen for one beat. Returns the response `data` block."""
    payload: dict = {"text": text, "voice_id": voice_id, "speed": speed}
    if engine:
        payload["engine"] = engine
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())["data"]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HeyGen API {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
        ) from exc


def fetch_as_mp3(audio_url: str, out_path: Path) -> None:
    """HeyGen hands back a wav URL; the assembler wants beat{N}.mp3."""
    with urllib.request.urlopen(audio_url, timeout=300) as resp:
        raw = resp.read()
    tmp = out_path.with_suffix(".src")
    tmp.write_bytes(raw)
    # -q:a 2 is transparent for speech and keeps the files small enough to live
    # beside the repo. ffmpeg is not on PATH on this box; see docs.
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(tmp), "-codec:a", "libmp3lame", "-q:a", "2", str(out_path)],
        check=True,
    )
    tmp.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--beat", help="Generate just this beat (e.g. 3b). Default: all.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing mp3s.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan, make no calls.")
    parser.add_argument("--voice-id", help="HeyGen voice id (overrides env).")
    parser.add_argument("--speed", type=float, help="Speed multiplier 0.5-2.0.")
    parser.add_argument("--engine", help="heygen_voice | starfish | elevenlabs | ...")
    args = parser.parse_args()

    api_key = os.environ.get("HEYGEN_API_KEY")
    voice_id = args.voice_id or os.environ.get("HEYGEN_VOICE_ID")
    speed = args.speed or float(os.environ.get("HEYGEN_SPEED") or DEFAULT_SPEED)
    engine = args.engine or os.environ.get("HEYGEN_ENGINE") or None

    if not args.dry_run:
        if not api_key:
            sys.exit("HEYGEN_API_KEY is not set")
        if not voice_id:
            sys.exit("HEYGEN_VOICE_ID is not set")

    beats = parse_beats(DEFAULT_VOICEOVER_MD)
    if not beats:
        sys.exit(f"No beats parsed from {DEFAULT_VOICEOVER_MD}")
    if args.beat:
        if args.beat not in beats:
            sys.exit(f"Beat '{args.beat}' not found. Available: {list(beats)}")
        beats = {args.beat: beats[args.beat]}

    print(f"[heygen] {len(beats)} beats, voice={voice_id}, speed={speed}", file=sys.stderr)
    if args.dry_run:
        for beat_id, text in beats.items():
            print(f"[heygen]   beat{beat_id}: {len(text)} chars", file=sys.stderr)
        return

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    TIMINGS_DIR.mkdir(parents=True, exist_ok=True)

    total = 0.0
    for beat_id, text in beats.items():
        out_path = AUDIO_DIR / f"beat{beat_id}.mp3"
        if out_path.exists() and not args.force:
            print(f"[heygen] skip beat{beat_id} — exists (--force to overwrite)", file=sys.stderr)
            continue
        data = synthesize(text, voice_id, speed, engine, api_key)
        fetch_as_mp3(data["audio_url"], out_path)
        # The response's own duration, the engine that served it and the word
        # list are what the retiming pass reads. Keep them with the audio.
        (TIMINGS_DIR / f"beat{beat_id}.json").write_text(
            json.dumps(
                {
                    "beat": beat_id,
                    "voice_id": voice_id,
                    "speed": speed,
                    "engine": data.get("engine"),
                    "duration": data["duration"],
                    "text": text,
                    "word_timestamps": data.get("word_timestamps") or [],
                },
                indent=2,
            ),
            encoding="utf-8",
            newline="\n",
        )
        total += data["duration"]
        print(f"[heygen] beat{beat_id}: {data['duration']:6.2f}s  {out_path}", file=sys.stderr)

    print(f"[heygen] generated {total:.2f}s of narration", file=sys.stderr)


if __name__ == "__main__":
    main()
