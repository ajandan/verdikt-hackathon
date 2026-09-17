"""Generate submission-video voice-over MP3s via the ElevenLabs REST API.

Beats are parsed from docs/demo-scripts/voiceover-elevenlabs.md — that file is
the single source of truth for the text (including <break time="Xs" /> tags).
Edit the markdown, re-run this script.

Output: one MP3 per beat under assets/video/audio/ — the layout the assembly
script (scripts/assemble_video.sh) expects.

SECURITY: pass the API key via the ELEVENLABS_API_KEY env var. Never bake it
into any file that gets committed.

Usage:
    export ELEVENLABS_API_KEY=sk_...
    python scripts/generate_voiceover.py                       # all beats
    python scripts/generate_voiceover.py --beat 3              # just one
    python scripts/generate_voiceover.py --dry-run             # char counts, then exit
    python scripts/generate_voiceover.py --force               # overwrite existing files
    python scripts/generate_voiceover.py --voice-id VOICE_ID   # override the voice

Env vars:
    ELEVENLABS_API_KEY       required
    ELEVENLABS_VOICE_ID      optional, default: Adam (pNInz6obpgDQGcFmaJgB)
    ELEVENLABS_MODEL_ID      optional, default: eleven_multilingual_v2
    DEMO_AUDIO_DIR           optional, default: assets/video/audio
    DEMO_VOICEOVER_MD        optional, default: docs/demo-scripts/voiceover-elevenlabs.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL_TEMPLATE = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"        # Adam
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_AUDIO_DIR = Path(os.environ.get("DEMO_AUDIO_DIR", "assets/video/audio"))
DEFAULT_VOICEOVER_MD = Path(
    os.environ.get("DEMO_VOICEOVER_MD", "docs/demo-scripts/voiceover-elevenlabs.md")
)

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


VOICE_SETTINGS = {
    "stability": _env_float("ELEVENLABS_STABILITY", 0.55),
    "similarity_boost": _env_float("ELEVENLABS_SIMILARITY", 0.75),
    "style": _env_float("ELEVENLABS_STYLE", 0.20),
    "use_speaker_boost": _env_bool("ELEVENLABS_SPEAKER_BOOST", True),
    "speed": _env_float("ELEVENLABS_SPEED", 1.0),
}


def parse_beats(md_path: Path) -> dict[str, str]:
    """Extract each beat's text from the voice-over markdown.

    Structure expected per beat:
        ## Beat <id> — <title>
        ...
        ```
        <text with break tags>
        ```
    """
    content = md_path.read_text(encoding="utf-8")
    beats: dict[str, str] = {}
    # Split on beat headers. The result is
    #   [preamble, id_1, body_1, id_2, body_2, ...].
    parts = re.split(r"^## Beat (\S+) — ", content, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        beat_id = parts[i]
        body = parts[i + 1]
        # Grab the first fenced code block in the body.
        m = re.search(r"^```\s*\n(.*?)\n^```", body, flags=re.MULTILINE | re.DOTALL)
        if m:
            beats[beat_id] = m.group(1).strip()
    return beats


def synthesize(text: str, voice_id: str, model_id: str, api_key: str, out_path: Path) -> None:
    url = API_URL_TEMPLATE.format(voice_id=voice_id)
    body = json.dumps(
        {"text": text, "model_id": model_id, "voice_settings": VOICE_SETTINGS}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs API {exc.code}: {err_body}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--beat", help="Generate just this beat (e.g. 2c). Default: all.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print character counts and exit — no API calls."
    )
    parser.add_argument("--voice-id", help="ElevenLabs voice ID (overrides env / default).")
    parser.add_argument("--model-id", help="ElevenLabs model ID (overrides env / default).")
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key and not args.dry_run:
        print("ELEVENLABS_API_KEY is not set", file=sys.stderr)
        sys.exit(2)

    voice_id = args.voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID
    model_id = args.model_id or os.environ.get("ELEVENLABS_MODEL_ID") or DEFAULT_MODEL_ID

    md_path = DEFAULT_VOICEOVER_MD
    if not md_path.exists():
        print(f"Voice-over markdown not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    beats = parse_beats(md_path)
    if not beats:
        print(f"No beats parsed from {md_path}", file=sys.stderr)
        sys.exit(1)

    if args.beat:
        if args.beat not in beats:
            print(
                f"Beat '{args.beat}' not found in {md_path}. Available: {list(beats)}",
                file=sys.stderr,
            )
            sys.exit(1)
        beats = {args.beat: beats[args.beat]}

    total = sum(len(t) for t in beats.values())
    print(f"[voiceover] parsed {len(beats)} beats from {md_path}", file=sys.stderr)
    for beat_id, text in beats.items():
        print(f"[voiceover]   beat{beat_id}: {len(text)} chars", file=sys.stderr)
    print(f"[voiceover] total: {total} chars (billable to ElevenLabs)", file=sys.stderr)

    if args.dry_run:
        return

    DEFAULT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"[voiceover] voice_id={voice_id} model_id={model_id} -> {DEFAULT_AUDIO_DIR}",
        file=sys.stderr,
    )

    for beat_id, text in beats.items():
        out_path = DEFAULT_AUDIO_DIR / f"beat{beat_id}.mp3"
        if out_path.exists() and not args.force:
            print(
                f"[voiceover] skip beat{beat_id} — {out_path} exists (use --force to overwrite)",
                file=sys.stderr,
            )
            continue
        print(f"[voiceover] generating beat{beat_id} -> {out_path}", file=sys.stderr)
        synthesize(text, voice_id, model_id, api_key, out_path)
        size_kb = out_path.stat().st_size / 1024
        print(f"[voiceover]   wrote {size_kb:.1f} KB", file=sys.stderr)


if __name__ == "__main__":
    main()
