#!/usr/bin/env bash
# Stage everything the Devpost submission needs into submission/.
#
# Devpost wants a repo URL, a demo URL, a video URL and a gallery of images,
# plus whatever supporting documents we choose to attach. This collects the
# attachable artifacts out of the places they are generated so the upload is
# one folder and nothing is hunted for at the deadline.
#
# Idempotent: wipes and rebuilds submission/ from current sources.
#
#   bash scripts/build_submission.sh
#
# Deliberately excluded:
#   - assets/video/screenshots/highlights/*  spotlight scrims, internal to the
#     video build, meaningless on their own
#   - .tmp/<real-company>_*                           reference a real pharmaceutical
#     company; every shipped artifact uses the fictional Alderwood/Cardiovita
#     data the deck promises
#   - tmp/extraction.json, tmp/alderwood_intake.json
#     raw extraction dumps full of patient-shaped fields; internal, not a
#     submission artifact
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=submission

# Windows holds transient locks on freshly-read files (indexer, AV, a PDF
# preview pane), and with `set -e` a single failed unlink would abort here
# leaving the folder half-deleted. Retry briefly, then say so plainly.
for attempt in 1 2 3 4 5; do
    rm -rf "$OUT" 2>/dev/null && break
    if [[ $attempt -eq 5 ]]; then
        echo "[submission] cannot clear $OUT -- a file is locked." >&2
        echo "[submission] close anything previewing submission/ and re-run." >&2
        exit 1
    fi
    sleep 1
done

mkdir -p "$OUT"/{slides,screenshots/product-ui,screenshots/e2e-walkthrough,screenshots/architecture,sample-outputs/templates,test-evidence,video}

# --- slides ---------------------------------------------------------------
# These are rendered from their HTML by scripts/render_docs_pdf.py. Re-run that
# after scripts/update_slide_stats.py, or the PDFs here carry numbers the HTML
# no longer claims -- which is exactly how a deck three revisions behind its
# own source ended up staged for submission once already.
for f in verdikt-judges-deck verdikt-one-pager verdikt-one-slide; do
    src="docs/$f.pdf"
    [[ -f "$src" ]] || { echo "[submission] WARN: missing $src" >&2; continue; }
    if [[ "docs/$f.html" -nt "$src" ]]; then
        echo "[submission] WARN: docs/$f.html is newer than its PDF -- run scripts/render_docs_pdf.py" >&2
    fi
    cp "$src" "$OUT/slides/"
done
cp docs/verdikt-one-slide.png    "$OUT/screenshots/cover.png"

# --- screenshots ----------------------------------------------------------
# Product UI tour: the 12 screens a judge would click through, including the
# HumanLoop queue that backs the human-review claim.
cp reports/ui-audit-shots/*.png  "$OUT/screenshots/product-ui/"
# Browser E2E walkthrough: tenant -> program -> slot -> dispatch -> verdict.
cp reports/e2e-shots/*.png       "$OUT/screenshots/e2e-walkthrough/"
# Architecture / narrative frames from the submission video (1x only; the @2x
# pairs are for the video pipeline, not for a gallery).
for f in assets/video/screenshots/beat*_primary.png; do
    case "$f" in *@2x*) continue;; esac
    cp "$f" "$OUT/screenshots/architecture/"
done

# --- sample outputs (the "test PDFs") -------------------------------------
# Real pipeline output: one reply package per verdict outcome.
cp tmp/generated_packages/case39_approve.pdf      "$OUT/sample-outputs/reply-approve.pdf"
cp tmp/generated_packages/case39_deny.pdf         "$OUT/sample-outputs/reply-deny.pdf"
cp tmp/generated_packages/case39_missing_info.pdf "$OUT/sample-outputs/reply-missing-info.pdf"
# The frozen, human-approved templates those replies are merged into, plus the
# blank intake form the pipeline reads.
cp sample_templates/alderwood/alderwood-pap-approval-ASP-201.pdf     "$OUT/sample-outputs/templates/"
cp sample_templates/alderwood/alderwood-pap-denial-ASP-202.pdf       "$OUT/sample-outputs/templates/"
cp sample_templates/alderwood/alderwood-pap-missing-info-ASP-203.pdf "$OUT/sample-outputs/templates/"
cp sample_templates/alderwood/alderwood-enrollment-form.pdf          "$OUT/sample-outputs/templates/"

# --- evidence -------------------------------------------------------------
cp reports/results.xml       "$OUT/test-evidence/junit-results.xml"
cp reports/test-report.xlsx  "$OUT/test-evidence/"
cp LICENSE                   "$OUT/"
# The index for the folder. Its source lives in docs/ precisely because
# this script wipes $OUT -- a README written into the folder by hand does
# not survive the next build.
cp docs/devpost-submission.md "$OUT/README.md"

# --- video ----------------------------------------------------------------
# Newest cut wins; the file is large and gitignored inside submission/ too.
latest=$(ls -t assets/video/verdikt-submission*.mp4 2>/dev/null | head -1 || true)
if [[ -n "$latest" ]]; then
    # Remux rather than copy, to force +faststart. Cuts produced before the
    # assembler learned that flag have their moov atom after mdat, so a browser
    # streaming them over HTTP cannot start playing until the whole file has
    # arrived -- the landing page's <video> just sits there. Stream copy, so
    # this is lossless and quick.
    if command -v ffmpeg >/dev/null 2>&1; then
        ffmpeg -y -hide_banner -loglevel error             -i "$latest" -c copy -movflags +faststart             "$OUT/video/verdikt-submission.mp4"
        echo "[submission] video: $latest (remuxed +faststart)"
    else
        cp "$latest" "$OUT/video/verdikt-submission.mp4"
        echo "[submission] WARN: ffmpeg not on PATH -- video copied without +faststart" >&2
    fi
else
    echo "[submission] WARN: no assets/video/verdikt-submission*.mp4 found" >&2
fi

# --- YouTube thumbnail ----------------------------------------------------
# Generated last: this script wipes $OUT at the top, so the thumbnail has to be
# produced after that, not copied from a previous build.
python scripts/make_thumbnail.py

echo "[submission] staged $(find "$OUT" -type f | wc -l) files, $(du -sh "$OUT" | cut -f1)"
