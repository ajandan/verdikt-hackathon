#!/usr/bin/env bash
#
# scripts/assemble_video.sh
#
# Stitch ElevenLabs voice-over audio + Beat 2 Playwright captures + placeholder
# cards for the motion-graphics beats into the final submission video.
#
# Output: assets/video/verdikt-submission.mp4  (~2:55, 1920x1080, H.264 + AAC)
#
# Every beat has a fixed target duration from docs/demo-scripts/voiceover-elevenlabs.md.
# Audio longer than the target is trimmed with a warning; shorter audio is
# silence-padded to fill the beat. This keeps the total run-time deterministic
# at 2:55 regardless of ElevenLabs pacing drift.
#
# Expected inputs (all optional — missing files degrade gracefully):
#   $BASE_DIR/audio/beat{1,2a,2b,2c,2d,3,4,5}.mp3   ElevenLabs voice-over
#   $BASE_DIR/captures/beat{2a,2c,2d}.webm          record_demo.py output
#   $BASE_DIR/captures/beat2b_alt.webm              optional Beat 2b live-UI fallback
#   $BASE_DIR/captures/beat{1,3,4,5}.webm           if you build motion graphics as MP4/WEBM
#
# Missing audio   -> silence for that beat, warning to stderr
# Missing capture -> placeholder card (title + subtitle over ink-900) for that beat
# Missing font    -> placeholder card renders as flat color; set DEMO_FONT_PATH to fix
#
# Env vars (all optional):
#   DEMO_BASE_DIR      default assets/video
#   DEMO_OUTPUT        default $DEMO_BASE_DIR/verdikt-submission.mp4
#   DEMO_FONT_PATH     default C:/Windows/Fonts/arial.ttf
#   RENDER_BEATS       whitespace-separated beat IDs to rebuild (default: all).
#                      Skipped segments must already exist under $SEG_DIR for concat to succeed.
#
# See docs/demo-scripts/assembling-video.md for the walk-through.

set -euo pipefail
trap 'echo "[assemble] failed at line $LINENO" >&2' ERR

# ---- Config ------------------------------------------------------

BASE_DIR="${DEMO_BASE_DIR:-assets/video}"
AUDIO_DIR="$BASE_DIR/audio"
CAPTURE_DIR="$BASE_DIR/captures"
SCREENSHOTS_DIR="$BASE_DIR/screenshots"
SEG_DIR="$BASE_DIR/segments"
OUTPUT="${DEMO_OUTPUT:-$BASE_DIR/verdikt-submission.mp4}"

FONT_PATH="${DEMO_FONT_PATH:-C:/Windows/Fonts/arial.ttf}"
# ffmpeg's filter-graph parser splits on ':' — a Windows drive-letter path
# (C:/…) is a nightmare to escape through bash → filter description. Sidestep
# the whole problem by copying the font to a colon-free relative path.
FONT_LOCAL=""

VIDEO_W=1920
VIDEO_H=1080
FPS=30

# Design-system colors (docs/design-system.html).
BG_DARK="0x141410"      # ink-900
FG_LIGHT="0xf6f5f1"     # ink-50
FG_MUTED="0xa9a394"     # ink-300

RENDER_BEATS="${RENDER_BEATS:-}"

# ---- Beat table (order = final concat order) ---------------------

BEAT_IDS=(0a 0b 0c 0d 1 2 3 3b 4 5 6 7 8)
# Story arc:
#   intro:   title -> PAP domain -> AI-in/deterministic-out -> end-to-end arch
#   body:    intake -> program -> doc package -> outcome mapping -> pipeline ->
#            deterministic -> cameo -> audit -> CTA
# Intro sum 38s, body trimmed by 4s (beats 1,2,6,8) to keep total under the
# 3:00 hackathon cap.
BEAT_DURATIONS=(4 12 14 9 12 16 16 18 26 21 11 11 9)    # sum = 179s (2:59); tuned to fit under the 3:00 hackathon cap after the triage-first Beat 4 rewrite. Beat 4 at 26 to fit triage narration (audio 25.6s). Beat 5 tightened to 21 with a shorter "Then AI stops" script. beat0b/0d/8 trimmed to their audio durations (no content cut); beat3b tail eats ~0.3s.
# Per-beat voice tempo. 1.0 = untouched. Beats whose narration would not otherwise
# fit under the 3:00 cap get a barely-audible 3% lift (on top of the 1.10 render).
BEAT_TEMPO=(1.0 1.0 1.0 1.03 1.0 1.0 1.03 1.03 1.0 1.0 1.0 1.0 1.0)
# Silence kept after the last spoken word so the voice never touches the cut.
TAIL_GAP=0.40
# Cross-dissolve laid over each beat boundary. 0 restores hard cuts.
BEAT_XFADE=0.30
# Narration-synced close-up shots cut by scripts/make_shots.py. Off: the zoomed
# crops read as a camera move the piece does not otherwise have, so every beat
# renders as its whole slide. USE_SHOTS=1 brings them back.
USE_SHOTS=${USE_SHOTS:-0}
# Narration-synced spotlights (scripts/make_highlights.py): on a beat that has a
# manifest, the slide dims everywhere except the element the voice is naming.
# USE_HIGHLIGHTS=0 renders the slides flat.
USE_HIGHLIGHTS=${USE_HIGHLIGHTS:-1}

BEAT_TITLES=(
    "VERDIKT"
    "PATIENT ASSISTANCE PROGRAMS"
    "AI IN, DETERMINISTIC OUT"
    "END-TO-END"
    "INTAKE DOCUMENTS"
    "PROGRAM AS DATA"
    "DOC PACKAGE"
    "OUTCOME MAPPING"
    "PIPELINE"
    "THEN AI STOPS"
    "OPPOSITE VERDICT"
    "AUDITABLE"
    "VERDIKT"
)
BEAT_SUBTITLES=(
    "Nebius × NVIDIA · Global AI Hackathon 2026"
    "thousands apply · each response compliance-audited"
    "AI reads · rules decide · templates ship"
    "every channel · one pipeline · every outcome traced"
    "everything the case begins with"
    "fields | rules | reply templates"
    "human-approved | frozen | forever"
    "operator declares the mapping"
    "extract | retrieve | reason"
    "byte-identical merge"
    "same code | opposite outcome"
    "audit log | reproducible"
    "MIT | repo | demo | dashboards"
)

# Per-beat 5-stage timeline overlay (Ingest → Triage → Decide → Deliver → Remember).
# The lower-left pill is NOT persistent: it shows up only on the beats where a
# stage is actually being walked through, so it reads as a stage marker rather
# than wallpaper. Values map to assets/video/screenshots/timeline/timeline-
# <variant>.png. Empty string = no pill on that beat (the default).
BEAT_TIMELINE=(
    ""                       # 0a title
    ""                       # 0b PAP domain
    ""                       # 0c thesis
    ""                       # 0d the architecture slide names all five stages itself
    ""                       # 1  intake documents - nothing is running yet
    ""                       # 2  program config - setup, not a pipeline run
    ""                       # 3  reply templates - setup
    ""                       # 3b outcome -> template bindings - setup
    "decide"                 # 4  pipeline runs - pill sweeps ingest/triage/decide
    "deliver"                # 5  "Deliver" is named - pill appears on the word
    ""                       # 6  cameo - no stage named, the verdict just varies
    "remember"               # 7  "Remember - every stage is auditable"
    ""                       # 8 CTA
)

# Seconds into a beat at which the stage pill appears, so it lands on the word
# instead of at the cut. 0 = from the first frame. Beat 4 ignores this — its
# pill sweeps ingest -> triage -> decide on its own schedule.
BEAT_TIMELINE_IN=(
    0                        # 0a
    0                        # 0b
    0                        # 0c
    0                        # 0d
    0                        # 1
    0                        # 2
    0                        # 3
    0                        # 3b
    0                        # 4  swept, not gated
    1.2                      # 5  "Then AI stops." <0.8s> "Deliver -"
    0                        # 6
    0                        # 7  "Remember -" opens the beat
    0                        # 8
)

# ---- Pre-flight --------------------------------------------------

command -v ffmpeg  >/dev/null || { echo "ffmpeg not on PATH" >&2;  exit 2; }
command -v ffprobe >/dev/null || { echo "ffprobe not on PATH" >&2; exit 2; }
[[ -f "$FONT_PATH" ]] || echo "warn: font $FONT_PATH not found; placeholder cards will render as flat color" >&2

mkdir -p "$AUDIO_DIR" "$CAPTURE_DIR" "$SCREENSHOTS_DIR" "$SEG_DIR"

# Stage the font locally under $SEG_DIR (colon-free path for the filter graph).
if [[ -f "$FONT_PATH" ]]; then
    FONT_LOCAL="$SEG_DIR/font.ttf"
    [[ -f "$FONT_LOCAL" ]] || cp "$FONT_PATH" "$FONT_LOCAL"
fi

# Discover which beats have live captures. Prefer beat2b.webm over beat2b_alt.webm.
CAPTURE_BEATS=()
USE_2B_ALT=""
for beat in "${BEAT_IDS[@]}"; do
    if [[ -f "$CAPTURE_DIR/beat${beat}.webm" ]]; then
        CAPTURE_BEATS+=("$beat")
    fi
done
if [[ ! " ${CAPTURE_BEATS[*]} " == *" 2b "* ]] && [[ -f "$CAPTURE_DIR/beat2b_alt.webm" ]]; then
    USE_2B_ALT=1
    CAPTURE_BEATS+=(2b)
fi

has_capture() {
    local beat=$1
    [[ " ${CAPTURE_BEATS[*]} " == *" $beat "* ]]
}

find_beat_image() {
    # Look for a beat_primary.{png,jpg,jpeg,webp,svg} — first hit wins.
    # SVGs work with recent ffmpeg builds via librsvg; rasterize first if not.
    local beat=$1
    for ext in png jpg jpeg webp svg; do
        local candidate="$SCREENSHOTS_DIR/beat${beat}_primary.${ext}"
        if [[ -f "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

should_render() {
    local beat=$1
    [[ -z "$RENDER_BEATS" ]] || [[ " $RENDER_BEATS " == *" $beat "* ]]
}

audio_duration() {
    ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null \
        | awk '{printf "%.2f", $1}'
}

# Time (s) at which the last spoken word ends. ElevenLabs mp3s carry ~0.3s of
# trailing silence; silencedetect reports it, and we cut the "speech end" there.
speech_end() {
    local f=$1 dur
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
    ffmpeg -hide_banner -i "$f" -af silencedetect=n=-45dB:d=0.12 -f null - 2>&1         | awk -v dur="$dur" '
            /silence_start/ { s=$NF; open=1 }
            /silence_end/   { e=$5; open=0; if (e+0 < dur-0.05) s="" }
            END { if (s != "") printf "%.2f", s; else printf "%.2f", dur }'
}

# Audio chain: optional gentle tempo, 20ms fade-in (no click), pad/trim to the
# beat, then a 300ms fade-out that lands entirely inside the silent tail.
fit_audio_chain() {
    local beat=$1 duration=$2 tempo=${3:-1.0}
    awk -v d="$duration" -v t="$tempo" 'BEGIN{
        fo = d - 0.30; if (fo < 0) fo = 0
        printf "atempo=%s,afade=t=in:st=0:d=0.02,apad=whole_dur=%s,atrim=duration=%s,asetpts=PTS-STARTPTS,afade=t=out:st=%.2f:d=0.30", t, d, d, fo }'
}

beat_tempo() {
    local want=$1 i
    for i in "${!BEAT_IDS[@]}"; do
        [[ "${BEAT_IDS[$i]}" == "$want" ]] && { echo "${BEAT_TEMPO[$i]}"; return; }
    done
    echo "1.0"
}

# ---- Per-beat renderers ------------------------------------------

timeline_png_path() {
    # Resolve the timeline PNG for a beat's timeline variant, or empty if the
    # beat has no timeline (intro/outro).
    local variant=$1
    [[ -z "$variant" ]] && { echo ""; return; }
    local candidate="$SCREENSHOTS_DIR/timeline/timeline-${variant}.png"
    [[ -f "$candidate" ]] && echo "$candidate" || echo ""
}

timeline_enable() {
    # ffmpeg overlay suffix that holds the pill back until the narration reaches
    # the stage name (see BEAT_TIMELINE_IN); empty = up from the first frame.
    local start=${1:-0}
    [[ -z "$start" || "$start" == "0" ]] && { echo ""; return; }
    echo ":enable='gte(t,${start})'"
}

# Timeline PNG geometry — scaled width + positioning aligned to the Verdikt
# mark (common.css: top:40, right:60, 68x68). Timeline sits just to the LEFT
# of the mark, same top edge. X offset from right edge = mark right margin
# (60) + mark width (68) + gap (16) = 144.
TIMELINE_W=620
TIMELINE_X=36            # lower-left lower-third; overlays share this left edge
TIMELINE_BOTTOM=30       # px from the bottom edge
CHIP_W=300
CHIP_BOTTOM=116          # chips stack just above the timeline pill

# Build a beat's visual from the narration-synced close-up shots cut by
# scripts/make_shots.py: a chain of stills cross-dissolved into each other.
# Durations in the manifest already account for the transition overlap, so the
# chain lands exactly on the beat's slot length. Video only — the caller adds
# audio and the lower-left overlays.
SHOTS_DIR="$SCREENSHOTS_DIR/shots"

build_shot_base() {
    local beat=$1 duration=$2
    local manifest="$SHOTS_DIR/beat${beat}.txt"
    local out="$SEG_DIR/.beat${beat}_base.mp4"

    local trans imgs=() durs=()
    trans=$(tr -d '\r' < "$manifest" | awk '$1=="T"{print $2}')
    while read -r name dur; do
        [[ "$name" == "T" || -z "$name" ]] && continue
        imgs+=("$SHOTS_DIR/$name")
        durs+=("$dur")
    done < <(tr -d '\r' < "$manifest")

    local n=${#imgs[@]}
    local inputs=() filter=""
    for i in "${!imgs[@]}"; do
        inputs+=(-loop 1 -t "${durs[$i]}" -i "${imgs[$i]}")
        filter+="[${i}:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,"
        filter+="pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},setsar=1,fps=${FPS}[v${i}];"
    done
    # Each dissolve starts one transition before its cut point, so the running
    # offset advances by the previous input's length minus the overlap.
    local acc=0 chain="[v0]" label
    for ((i = 1; i < n; i++)); do
        acc=$(awk -v a="$acc" -v d="${durs[$((i - 1))]}" -v t="$trans" \
                  'BEGIN{ printf "%.2f", a + d - t }')
        label="x${i}"
        filter+="${chain}[v${i}]xfade=transition=fade:duration=${trans}:offset=${acc}[${label}];"
        chain="[${label}]"
    done
    filter="${filter%;}"

    echo "[assemble] beat${beat}: ${n} shots -> $out"
    ffmpeg -y -hide_banner -loglevel error \
        "${inputs[@]}" \
        -filter_complex "$filter" \
        -map "$chain" \
        -c:v libx264 -pix_fmt yuv420p -preset medium -crf 18 \
        -t "$duration" \
        "$out"
    echo "$out"
}

# Build a beat's visual as the whole slide plus the narration-synced spotlight
# scrims rendered by scripts/make_highlights.py: everything dims except the
# element the voice is naming. Each scrim is looped for the whole beat and its
# alpha faded up and down inside its own window, so no frame is ever waiting on
# an input that has not started yet. Video only — the caller adds audio and the
# lower-left overlays.
HL_DIR="$SCREENSHOTS_DIR/highlights"

build_highlight_base() {
    local beat=$1 duration=$2 image=$3
    local manifest="$HL_DIR/beat${beat}.txt"
    local out="$SEG_DIR/.beat${beat}_hl.mp4"

    local inputs=(-loop 1 -t "$duration" -i "$image")
    local filter="[0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                       pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},
                       setsar=1,fps=${FPS}[c0];"
    local idx=1 chain="c0" n=0 name start end fade fade_out

    while read -r name start end fade; do
        [[ -z "$name" ]] && continue
        [[ -f "$HL_DIR/$name" ]] || { echo "warn: $HL_DIR/$name missing" >&2; continue; }
        inputs+=(-loop 1 -t "$duration" -i "$HL_DIR/$name")
        fade_out=$(awk -v e="$end" -v f="$fade" 'BEGIN{ printf "%.2f", e - f }')
        filter+="[${idx}:v]format=rgba,fps=${FPS},
                     fade=t=in:st=${start}:d=${fade}:alpha=1,
                     fade=t=out:st=${fade_out}:d=${fade}:alpha=1[h${idx}];"
        filter+="[${chain}][h${idx}]overlay=0:0:format=auto[m${idx}];"
        chain="m${idx}"
        idx=$((idx + 1))
        n=$((n + 1))
    done < <(tr -d '\r' < "$manifest")

    filter="${filter%;}"
    echo "[assemble] beat${beat}: ${n} highlights -> $out" >&2
    ffmpeg -y -hide_banner -loglevel error \
        "${inputs[@]}" \
        -filter_complex "$filter" \
        -map "[${chain}]" \
        -an \
        -c:v libx264 -pix_fmt yuv420p -preset medium -crf 18 \
        -t "$duration" \
        "$out"
    echo "$out"
}

render_capture_beat() {
    local beat=$1 duration=$2 timeline_variant=${3:-}
    local capture_file="$CAPTURE_DIR/beat${beat}.webm"
    # Playwright captures open on a blank frame; skip it. A base built from
    # still shots starts on content, so it is used from its first frame.
    local seek=0.7
    if [[ -n "${BASE_OVERRIDE:-}" ]]; then
        capture_file="$BASE_OVERRIDE"
        seek=0
    fi
    if [[ "$beat" == "2b" && -n "$USE_2B_ALT" ]]; then
        capture_file="$CAPTURE_DIR/beat2b_alt.webm"
    fi
    local audio_file="$AUDIO_DIR/beat${beat}.mp3"
    local out_file="$SEG_DIR/beat${beat}.mp4"

    local audio_inputs=() audio_filter
    if [[ -f "$audio_file" ]]; then
        audio_inputs=(-i "$audio_file")
        audio_filter="[1:a]$(fit_audio_chain "$beat" "$duration" "$(beat_tempo "$beat")")[a]"
    else
        echo "warn: audio $audio_file missing; beat${beat} will be silent" >&2
        audio_inputs=(-f lavfi -i "anullsrc=r=44100:cl=stereo")
        audio_filter="[1:a]atrim=duration=${duration}[a]"
    fi

    # Beat 4: overlay pre-rendered NVIDIA branded chip PNGs at top-right,
    # timed to each model's narration window. Chip PNGs are rendered from
    # assets/video/designs/nvidia-chips.html with transparent background.
    local chip_dir="$SCREENSHOTS_DIR/chips"
    local use_chips=""
    if [[ "$beat" == "4" && -f "$chip_dir/nv-ingest.png" && -f "$chip_dir/nemo.png" && -f "$chip_dir/nemotron.png" ]]; then
        use_chips=1
    fi

    local tl_png=$(timeline_png_path "$timeline_variant")
    local tl_ena=$(timeline_enable "${TL_START:-0}")

    echo "[assemble] beat${beat}: capture ($capture_file) -> $out_file (${duration}s)"
    if [[ -n "$use_chips" && -n "$tl_png" ]]; then
        # Beat 4 is a triage-first pipeline walkthrough. Instead of a single
        # timeline variant frozen for the full beat, sweep the pill through
        # ingest -> triage -> decide as the narration explains each stage.
        # Chip overlays are timed to the same moments.
        local tl_ingest="$SCREENSHOTS_DIR/timeline/timeline-ingest.png"
        local tl_triage="$SCREENSHOTS_DIR/timeline/timeline-triage.png"
        local tl_decide="$SCREENSHOTS_DIR/timeline/timeline-decide.png"
        ffmpeg -y -hide_banner -loglevel error \
            -ss "$seek" -i "$capture_file" \
            "${audio_inputs[@]}" \
            -i "$chip_dir/nv-ingest.png" \
            -i "$chip_dir/nemo.png" \
            -i "$chip_dir/nemotron.png" \
            -i "$tl_ingest" \
            -i "$tl_triage" \
            -i "$tl_decide" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,
                     setsar=1,fps=${FPS},
                     tpad=stop_mode=clone:stop_duration=${duration},
                     trim=duration=${duration},setpts=PTS-STARTPTS[base];
                [5:v]scale=${TIMELINE_W}:-1[tli];
                [6:v]scale=${TIMELINE_W}:-1[tlt];
                [7:v]scale=${TIMELINE_W}:-1[tld];
                [base][tli]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}:enable='between(t,0,6.5)'[bi];
                [bi][tlt]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}:enable='between(t,6.5,14)'[bt];
                [bt][tld]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}:enable='between(t,14,26)'[b1];
                [2:v]scale=${CHIP_W}:-1[c1]; [3:v]scale=${CHIP_W}:-1[c2]; [4:v]scale=${CHIP_W}:-1[c3];
                [b1][c1]overlay=${TIMELINE_X}:H-h-${CHIP_BOTTOM}:enable='between(t,3,7)'[o1];
                [o1][c2]overlay=${TIMELINE_X}:H-h-${CHIP_BOTTOM}:enable='between(t,7,9.5)'[o2];
                [o2][c3]overlay=${TIMELINE_X}:H-h-${CHIP_BOTTOM}:enable='between(t,14,22)'[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    elif [[ -n "$use_chips" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -ss "$seek" -i "$capture_file" \
            "${audio_inputs[@]}" \
            -i "$chip_dir/nv-ingest.png" \
            -i "$chip_dir/nemo.png" \
            -i "$chip_dir/nemotron.png" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,
                     setsar=1,fps=${FPS},
                     tpad=stop_mode=clone:stop_duration=${duration},
                     trim=duration=${duration},setpts=PTS-STARTPTS[base];
                [2:v]scale=${CHIP_W}:-1[c1]; [3:v]scale=${CHIP_W}:-1[c2]; [4:v]scale=${CHIP_W}:-1[c3];
                [base][c1]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}:enable='between(t,4,8)'[o1];
                [o1][c2]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}:enable='between(t,8,11)'[o2];
                [o2][c3]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}:enable='between(t,11,15)'[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    elif [[ -n "$tl_png" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -ss "$seek" -i "$capture_file" \
            "${audio_inputs[@]}" \
            -i "$tl_png" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,
                     setsar=1,fps=${FPS},
                     tpad=stop_mode=clone:stop_duration=${duration},
                     trim=duration=${duration},setpts=PTS-STARTPTS[base];
                [2:v]scale=${TIMELINE_W}:-1[tl];
                [base][tl]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}${tl_ena}[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    else
        ffmpeg -y -hide_banner -loglevel error \
            -ss "$seek" -i "$capture_file" \
            "${audio_inputs[@]}" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,
                     setsar=1,fps=${FPS},
                     tpad=stop_mode=clone:stop_duration=${duration},
                     trim=duration=${duration},setpts=PTS-STARTPTS[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    fi
}

render_image_beat() {
    local beat=$1 duration=$2 image_path=$3 timeline_variant=${4:-}
    local audio_file="$AUDIO_DIR/beat${beat}.mp3"
    local out_file="$SEG_DIR/beat${beat}.mp4"

    local audio_inputs=() audio_filter
    if [[ -f "$audio_file" ]]; then
        audio_inputs=(-i "$audio_file")
        audio_filter="[1:a]$(fit_audio_chain "$beat" "$duration" "$(beat_tempo "$beat")")[a]"
    else
        echo "warn: audio $audio_file missing; beat${beat} will be silent" >&2
        audio_inputs=(-f lavfi -i "anullsrc=r=44100:cl=stereo")
        audio_filter="[1:a]atrim=duration=${duration}[a]"
    fi

    local tl_png=$(timeline_png_path "$timeline_variant")
    local tl_ena=$(timeline_enable "${TL_START:-0}")

    # Beat 8: overlay the QR card PNG for the final 3 seconds only. The card
    # is pre-rendered from beat8-qr-card.html with a transparent background.
    local qr_overlay="$SCREENSHOTS_DIR/beat8-qr-card.png"
    local use_qr_overlay=""
    if [[ "$beat" == "8" && -f "$qr_overlay" ]]; then
        use_qr_overlay=1
    fi

    # Beat 5: overlay the design-time labeler slideshow (real captures:
    # template detail -> placement editor with PDF+bboxes -> expanded 21-field
    # list). It runs t=7.2s -> 15.2s, over "from Alderwood's template - sha256
    # pinned", so the slide's own Human-review gate row stays visible while the
    # voice says "nothing leaves until a human approves" at t=5.1s.
    local labeler_overlay="$CAPTURE_DIR/labeler-clip.mp4"
    local use_labeler_overlay=""
    if [[ "$beat" == "5" && -f "$labeler_overlay" ]]; then
        use_labeler_overlay=1
    fi

    echo "[assemble] beat${beat}: image ($image_path) -> $out_file (${duration}s)"
    # Held frame at 1920x1080, letterboxed if the source isn't 16:9.
    # Subtle 5% zoom over the beat's duration keeps the frame from feeling static
    # against the moving voice-over.
    if [[ -n "$use_qr_overlay" ]]; then
        local qr_start=$(awk -v d="$duration" 'BEGIN{printf "%d", d-3}')
        ffmpeg -y -hide_banner -loglevel error \
            -loop 1 -t "$duration" -i "$image_path" \
            "${audio_inputs[@]}" \
            -i "$qr_overlay" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},
                     setsar=1,fps=${FPS}[base];
                [2:v]scale=560:-1[qr];
                [base][qr]overlay=W-w-150:(H-h)/2:enable='gte(t,${qr_start})'[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    elif [[ -n "$use_labeler_overlay" && -n "$tl_png" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -loop 1 -t "$duration" -i "$image_path" \
            "${audio_inputs[@]}" \
            -i "$tl_png" \
            -itsoffset 7.2 -i "$labeler_overlay" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},
                     setsar=1,fps=${FPS}[base];
                [2:v]scale=${TIMELINE_W}:-1[tl];
                [base][tl]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}${tl_ena}[b1];
                [b1][3:v]overlay=0:0:enable='between(t,7.2,15.2)'[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    elif [[ -n "$tl_png" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -loop 1 -t "$duration" -i "$image_path" \
            "${audio_inputs[@]}" \
            -i "$tl_png" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},
                     setsar=1,fps=${FPS}[base];
                [2:v]scale=${TIMELINE_W}:-1[tl];
                [base][tl]overlay=${TIMELINE_X}:H-h-${TIMELINE_BOTTOM}${tl_ena}[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    else
        ffmpeg -y -hide_banner -loglevel error \
            -loop 1 -t "$duration" -i "$image_path" \
            "${audio_inputs[@]}" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},
                     setsar=1,fps=${FPS}[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    fi
}

render_placeholder_beat() {
    local beat=$1 duration=$2 title=$3 subtitle=$4
    local audio_file="$AUDIO_DIR/beat${beat}.mp3"
    local out_file="$SEG_DIR/beat${beat}.mp4"
    local title_file="$SEG_DIR/.beat${beat}_title.txt"
    local subtitle_file="$SEG_DIR/.beat${beat}_subtitle.txt"
    local wordmark_file="$SEG_DIR/.beat${beat}_wm.txt"

    printf '%s' "$title" > "$title_file"
    printf '%s' "$subtitle" > "$subtitle_file"
    printf '%s' "VERDIKT" > "$wordmark_file"

    local audio_inputs=() audio_filter
    if [[ -f "$audio_file" ]]; then
        audio_inputs=(-i "$audio_file")
        audio_filter="[1:a]$(fit_audio_chain "$beat" "$duration" "$(beat_tempo "$beat")")[a]"
    else
        echo "warn: audio $audio_file missing; beat${beat} will be silent" >&2
        audio_inputs=(-f lavfi -i "anullsrc=r=44100:cl=stereo")
        audio_filter="[1:a]atrim=duration=${duration}[a]"
    fi

    local drawtext_layers=""
    if [[ -n "$FONT_LOCAL" ]]; then
        drawtext_layers="
            drawtext=fontfile=${FONT_LOCAL}:textfile=${title_file}:fontcolor=${FG_LIGHT}:fontsize=96:x=(w-tw)/2:y=(h-th)/2-60,
            drawtext=fontfile=${FONT_LOCAL}:textfile=${subtitle_file}:fontcolor=${FG_MUTED}:fontsize=40:x=(w-tw)/2:y=(h-th)/2+80,
            drawtext=fontfile=${FONT_LOCAL}:textfile=${wordmark_file}:fontcolor=${FG_MUTED}:fontsize=28:x=w-tw-60:y=h-th-40,
        "
    fi

    echo "[assemble] beat${beat}: placeholder -> $out_file (${duration}s)"
    ffmpeg -y -hide_banner -loglevel error \
        -f lavfi -i "color=c=${BG_DARK}:s=${VIDEO_W}x${VIDEO_H}:d=${duration}:r=${FPS}" \
        "${audio_inputs[@]}" \
        -filter_complex "
            [0:v]${drawtext_layers}setsar=1[v];
            ${audio_filter}
        " \
        -map "[v]" -map "[a]" \
        -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
        -c:a aac -b:a 192k -ar 44100 \
        -t "$duration" \
        "$out_file"

    rm -f "$title_file" "$subtitle_file" "$wordmark_file"
}

# ---- Fit each beat to its narration -------------------------------
# duration = (last spoken word / tempo) + TAIL_GAP, rounded up to 50ms. The
# hand-set BEAT_DURATIONS stay as the fallback for beats with no audio.
for i in "${!BEAT_IDS[@]}"; do
    f="$AUDIO_DIR/beat${BEAT_IDS[$i]}.mp3"
    [[ -f "$f" ]] || continue
    end=$(speech_end "$f")
    BEAT_DURATIONS[$i]=$(awk -v e="$end" -v t="${BEAT_TEMPO[$i]}" -v g="$TAIL_GAP"         'BEGIN{ v=e/t+g; printf "%.2f", int(v*20+0.999)/20 }')
    echo "[assemble] beat${BEAT_IDS[$i]}: speech ends ${end}s -> slot ${BEAT_DURATIONS[$i]}s"
done

# ---- Audio-duration sanity check ---------------------------------

total_target=$(printf '%s
' "${BEAT_DURATIONS[@]}" | awk '{t+=$1} END{printf "%.2f", t}')
echo "[assemble] target total: ${total_target}s"
awk -v t="$total_target" 'BEGIN{exit !(t > 179.8)}' && echo "warn: total ${total_target}s is over the 3:00 cap" >&2

for i in "${!BEAT_IDS[@]}"; do
    beat="${BEAT_IDS[$i]}"
    target="${BEAT_DURATIONS[$i]}"
    audio_file="$AUDIO_DIR/beat${beat}.mp3"
    if [[ -f "$audio_file" ]]; then
        # Compare against the tempo-adjusted speech length — the slot was sized
        # from it, so a raw mp3 longer than the slot is not by itself a problem.
        actual=$(speech_end "$audio_file")
        if awk -v a="$actual" -v t="$target" -v p="${BEAT_TEMPO[$i]}" \
               'BEGIN{exit !(a / p > t)}'; then
            echo "warn: beat${beat} speech ${actual}s (tempo ${BEAT_TEMPO[$i]}) overruns slot ${target}s — will be trimmed" >&2
        fi
    fi
done

# ---- Render every beat --------------------------------------------

for i in "${!BEAT_IDS[@]}"; do
    beat="${BEAT_IDS[$i]}"
    dur="${BEAT_DURATIONS[$i]}"
    title="${BEAT_TITLES[$i]}"
    subtitle="${BEAT_SUBTITLES[$i]}"
    out_file="$SEG_DIR/beat${beat}.mp4"

    if ! should_render "$beat"; then
        if [[ ! -f "$out_file" ]]; then
            echo "warn: RENDER_BEATS='$RENDER_BEATS' skips beat${beat} but $out_file is missing — concat will fail" >&2
        else
            echo "[assemble] beat${beat}: skipped (per RENDER_BEATS), reusing $out_file"
        fi
        continue
    fi

    timeline_variant="${BEAT_TIMELINE[$i]:-}"
    TL_START="${BEAT_TIMELINE_IN[$i]:-0}"
    if has_capture "$beat"; then
        render_capture_beat "$beat" "$dur" "$timeline_variant"
    elif [[ "$USE_SHOTS" == "1" && -f "$SHOTS_DIR/beat${beat}.txt" ]]; then
        base=$(build_shot_base "$beat" "$dur" | tail -1)
        BASE_OVERRIDE="$base" render_capture_beat "$beat" "$dur" "$timeline_variant"
    elif [[ "$USE_HIGHLIGHTS" == "1" && -f "$HL_DIR/beat${beat}.txt" ]] \
         && image_path=$(find_beat_image "$beat"); then
        base=$(build_highlight_base "$beat" "$dur" "$image_path" | tail -1)
        BASE_OVERRIDE="$base" render_capture_beat "$beat" "$dur" "$timeline_variant"
    elif image_path=$(find_beat_image "$beat"); then
        render_image_beat "$beat" "$dur" "$image_path" "$timeline_variant"
    else
        render_placeholder_beat "$beat" "$dur" "$title" "$subtitle"
    fi
done

# ---- Concat -------------------------------------------------------

concat_list=$(mktemp)
trap 'rm -f "$concat_list"; echo "[assemble] failed at line $LINENO" >&2' ERR
# Use pwd -W on git-bash so ffmpeg gets a native Windows path (C:/…); falls
# back to plain pwd on Linux/macOS where -W is not supported.
pwd_native=$(pwd -W 2>/dev/null || pwd)
for beat in "${BEAT_IDS[@]}"; do
    seg="$SEG_DIR/beat${beat}.mp4"
    [[ -f "$seg" ]] || { echo "error: missing segment $seg" >&2; exit 1; }
    echo "file '${pwd_native}/${seg}'" >> "$concat_list"
done

if awk -v t="$BEAT_XFADE" 'BEGIN{exit !(t > 0)}'; then
    # Cross-dissolve the beat boundaries instead of hard-cutting them.
    #
    # Each segment after the first is padded at the START with a frozen copy of
    # its first frame, exactly one transition long, and the dissolve is placed
    # over that pad. So a beat's own content still begins on its original cut
    # point and stays in sync with its narration — only the still hand-over is
    # stretched across the boundary. Audio is plain-concatenated and never
    # overlapped, which is safe because every beat already ends in silence.
    seg_inputs=() vfilter="" afilter="" acc=0 chain="[bv0]"
    for i in "${!BEAT_IDS[@]}"; do
        seg="$SEG_DIR/beat${BEAT_IDS[$i]}.mp4"
        seg_inputs+=(-i "$seg")
        if [[ $i -eq 0 ]]; then
            vfilter+="[${i}:v]fps=${FPS},setsar=1[bv${i}];"
        else
            vfilter+="[${i}:v]tpad=start_mode=clone:start_duration=${BEAT_XFADE},fps=${FPS},setsar=1[bv${i}];"
        fi
        afilter+="[${i}:a]"
    done
    for ((i = 1; i < ${#BEAT_IDS[@]}; i++)); do
        prev_seg="$SEG_DIR/beat${BEAT_IDS[$((i - 1))]}.mp4"
        acc=$(awk -v a="$acc" -v d="$(audio_duration "$prev_seg")" 'BEGIN{ printf "%.3f", a + d }')
        off=$(awk -v a="$acc" -v t="$BEAT_XFADE" 'BEGIN{ printf "%.3f", a - t }')
        vfilter+="${chain}[bv${i}]xfade=transition=fade:duration=${BEAT_XFADE}:offset=${off}[bx${i}];"
        chain="[bx${i}]"
    done
    afilter+="concat=n=${#BEAT_IDS[@]}:v=0:a=1[ba]"

    echo "[assemble] joining ${#BEAT_IDS[@]} beats with ${BEAT_XFADE}s dissolves -> $OUTPUT.raw.mp4"
    ffmpeg -y -hide_banner -loglevel error \
        "${seg_inputs[@]}" \
        -filter_complex "${vfilter}${afilter}" \
        -map "$chain" -map "[ba]" \
        -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
        -c:a aac -b:a 192k -ar 44100 \
        "$OUTPUT.raw.mp4"
else
    echo "[assemble] concatenating -> $OUTPUT.raw.mp4"
    ffmpeg -y -hide_banner -loglevel error \
        -f concat -safe 0 -i "$concat_list" \
        -c copy \
        "$OUTPUT.raw.mp4"
fi
rm -f "$concat_list"

# Post-process: loudness-normalize the audio track to -14 LUFS (YouTube target).
# Video stream copies through untouched; only audio is re-encoded.
echo "[assemble] loudnorm pass -> $OUTPUT"
ffmpeg -y -hide_banner -loglevel error \
    -i "$OUTPUT.raw.mp4" \
    -c:v copy \
    -af "loudnorm=I=-14:TP=-1.5:LRA=11" \
    -c:a aac -b:a 192k -ar 44100 \
    "$OUTPUT"
rm -f "$OUTPUT.raw.mp4"

# ---- Report -------------------------------------------------------

final_dur=$(audio_duration "$OUTPUT")
echo "[assemble] done: $OUTPUT (${final_dur}s)"
if awk -v d="$final_dur" 'BEGIN{exit !(d > 180)}'; then
    echo "warn: final duration ${final_dur}s exceeds the 3:00 hackathon cap" >&2
fi
