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
BEAT_DURATIONS=(4 14 14 10 12 16 16 19 20 24 11 11 10)   # sum = 181s; beat0d bumped 9->10 to fit "Five named stages" 5-stage voiceover (audio 9.85s). Final ffmpeg trim to 179.9s eats the 1.1s tail silence at end of beat 8.
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
# Persistent top-right pill (level with the Verdikt mark); only the highlighted
# stage moves beat-to-beat so judges can track where each explanation sits in
# the pipeline. Values map to assets/video/screenshots/timeline/timeline-
# <variant>.png. Empty string = no timeline overlay (intro/outro beats).
BEAT_TIMELINE=(
    ""                       # 0a title
    ""                       # 0b PAP domain
    ""                       # 0c thesis
    "none"                   # 0d architecture (all 5 stages neutral — intro to pipeline)
    "ingest"                 # 1  intake documents
    "ingest"                 # 2  program config (still pre-run setup)
    "ingest"                 # 3  reply templates (still setup — pipeline hasn't run yet)
    "ingest"                 # 3b outcome→template bindings (still setup)
    "decide"                 # 4  pipeline runs — decide stage
    "deliver"                # 5  deterministic render — deliver stage
    "deliver"                # 6  cameo — still delivering, verdict just varies
    "remember"               # 7  audit trail — remember stage
    ""                       # 8 CTA
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

# ---- Per-beat renderers ------------------------------------------

timeline_png_path() {
    # Resolve the timeline PNG for a beat's timeline variant, or empty if the
    # beat has no timeline (intro/outro).
    local variant=$1
    [[ -z "$variant" ]] && { echo ""; return; }
    local candidate="$SCREENSHOTS_DIR/timeline/timeline-${variant}.png"
    [[ -f "$candidate" ]] && echo "$candidate" || echo ""
}

# Timeline PNG geometry — scaled width + positioning aligned to the Verdikt
# mark (common.css: top:40, right:60, 68x68). Timeline sits just to the LEFT
# of the mark, same top edge. X offset from right edge = mark right margin
# (60) + mark width (68) + gap (16) = 144.
TIMELINE_W=640
TIMELINE_Y=53
TIMELINE_X_FROM_RIGHT=144

render_capture_beat() {
    local beat=$1 duration=$2 timeline_variant=${3:-}
    local capture_file="$CAPTURE_DIR/beat${beat}.webm"
    if [[ "$beat" == "2b" && -n "$USE_2B_ALT" ]]; then
        capture_file="$CAPTURE_DIR/beat2b_alt.webm"
    fi
    local audio_file="$AUDIO_DIR/beat${beat}.mp3"
    local out_file="$SEG_DIR/beat${beat}.mp4"

    local audio_inputs=() audio_filter
    if [[ -f "$audio_file" ]]; then
        audio_inputs=(-i "$audio_file")
        audio_filter="[1:a]atrim=duration=${duration},asetpts=PTS-STARTPTS,apad=whole_dur=${duration}[a]"
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

    echo "[assemble] beat${beat}: capture ($capture_file) -> $out_file (${duration}s)"
    if [[ -n "$use_chips" && -n "$tl_png" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -ss 0.7 -i "$capture_file" \
            "${audio_inputs[@]}" \
            -i "$chip_dir/nv-ingest.png" \
            -i "$chip_dir/nemo.png" \
            -i "$chip_dir/nemotron.png" \
            -i "$tl_png" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,
                     setsar=1,fps=${FPS},
                     tpad=stop_mode=clone:stop_duration=${duration},
                     trim=duration=${duration},setpts=PTS-STARTPTS[base];
                [5:v]scale=${TIMELINE_W}:-1[tl];
                [base][tl]overlay=W-w-${TIMELINE_X_FROM_RIGHT}:${TIMELINE_Y}[b1];
                [b1][2:v]overlay=W-w-70:120:enable='between(t,4,8)'[o1];
                [o1][3:v]overlay=W-w-70:120:enable='between(t,8,11)'[o2];
                [o2][4:v]overlay=W-w-70:120:enable='between(t,11,15)'[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    elif [[ -n "$use_chips" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -ss 0.7 -i "$capture_file" \
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
                [base][2:v]overlay=W-w-70:80:enable='between(t,4,8)'[o1];
                [o1][3:v]overlay=W-w-70:80:enable='between(t,8,11)'[o2];
                [o2][4:v]overlay=W-w-70:80:enable='between(t,11,15)'[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    elif [[ -n "$tl_png" ]]; then
        ffmpeg -y -hide_banner -loglevel error \
            -ss 0.7 -i "$capture_file" \
            "${audio_inputs[@]}" \
            -i "$tl_png" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,
                     setsar=1,fps=${FPS},
                     tpad=stop_mode=clone:stop_duration=${duration},
                     trim=duration=${duration},setpts=PTS-STARTPTS[base];
                [2:v]scale=${TIMELINE_W}:-1[tl];
                [base][tl]overlay=W-w-${TIMELINE_X_FROM_RIGHT}:${TIMELINE_Y}[v];
                ${audio_filter}
            " \
            -map "[v]" -map "[a]" \
            -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
            -c:a aac -b:a 192k -ar 44100 \
            -t "$duration" \
            "$out_file"
    else
        ffmpeg -y -hide_banner -loglevel error \
            -ss 0.7 -i "$capture_file" \
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
        audio_filter="[1:a]atrim=duration=${duration},asetpts=PTS-STARTPTS,apad=whole_dur=${duration}[a]"
    else
        echo "warn: audio $audio_file missing; beat${beat} will be silent" >&2
        audio_inputs=(-f lavfi -i "anullsrc=r=44100:cl=stereo")
        audio_filter="[1:a]atrim=duration=${duration}[a]"
    fi

    local tl_png=$(timeline_png_path "$timeline_variant")

    # Beat 8: overlay the QR card PNG for the final 3 seconds only. The card
    # is pre-rendered from beat8-qr-card.html with a transparent background.
    local qr_overlay="$SCREENSHOTS_DIR/beat8-qr-card.png"
    local use_qr_overlay=""
    if [[ "$beat" == "8" && -f "$qr_overlay" ]]; then
        use_qr_overlay=1
    fi

    # Beat 5: overlay the design-time labeler slideshow (real captures:
    # template detail → placement editor with PDF+bboxes → expanded 21-field
    # list) during the "Templates drafted by design-time AI" narration window
    # (t=3.0s → 11.0s within the beat — 8s covers all three states).
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
                [base][qr]overlay=(W-w)/2:(H-h)/2:enable='gte(t,${qr_start})'[v];
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
            -itsoffset 3.0 -i "$labeler_overlay" \
            -filter_complex "
                [0:v]scale=${VIDEO_W}:${VIDEO_H}:force_original_aspect_ratio=decrease,
                     pad=${VIDEO_W}:${VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=${BG_DARK},
                     setsar=1,fps=${FPS}[base];
                [2:v]scale=${TIMELINE_W}:-1[tl];
                [base][tl]overlay=W-w-${TIMELINE_X_FROM_RIGHT}:${TIMELINE_Y}[b1];
                [b1][3:v]overlay=0:0:enable='between(t,3.0,11.0)'[v];
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
                [base][tl]overlay=W-w-${TIMELINE_X_FROM_RIGHT}:${TIMELINE_Y}[v];
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
        audio_filter="[1:a]atrim=duration=${duration},asetpts=PTS-STARTPTS,apad=whole_dur=${duration}[a]"
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

# ---- Audio-duration sanity check ---------------------------------

total_target=0
for d in "${BEAT_DURATIONS[@]}"; do total_target=$((total_target + d)); done
echo "[assemble] target total: ${total_target}s ($(printf '%d:%02d' $((total_target / 60)) $((total_target % 60))))"

for i in "${!BEAT_IDS[@]}"; do
    beat="${BEAT_IDS[$i]}"
    target="${BEAT_DURATIONS[$i]}"
    audio_file="$AUDIO_DIR/beat${beat}.mp3"
    if [[ -f "$audio_file" ]]; then
        actual=$(audio_duration "$audio_file")
        if awk -v a="$actual" -v t="$target" 'BEGIN{exit !(a > t + 0.3)}'; then
            echo "warn: beat${beat} audio ${actual}s > target ${target}s — will be trimmed" >&2
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
    if has_capture "$beat"; then
        render_capture_beat "$beat" "$dur" "$timeline_variant"
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

echo "[assemble] concatenating -> $OUTPUT.raw.mp4"
ffmpeg -y -hide_banner -loglevel error \
    -f concat -safe 0 -i "$concat_list" \
    -c copy \
    "$OUTPUT.raw.mp4"
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
