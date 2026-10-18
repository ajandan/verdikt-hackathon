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

rm -rf "$OUT"
mkdir -p "$OUT"/{slides,screenshots/product-ui,screenshots/e2e-walkthrough,screenshots/architecture,sample-outputs/templates,test-evidence,video}

# --- slides ---------------------------------------------------------------
cp docs/verdikt-judges-deck.pdf  "$OUT/slides/"
cp docs/verdikt-one-pager.pdf    "$OUT/slides/"
cp docs/verdikt-one-slide.pdf    "$OUT/slides/"
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

# --- video ----------------------------------------------------------------
# Newest cut wins; the file is large and gitignored inside submission/ too.
latest=$(ls -t assets/video/verdikt-submission*.mp4 2>/dev/null | head -1 || true)
if [[ -n "$latest" ]]; then
    cp "$latest" "$OUT/video/verdikt-submission.mp4"
    echo "[submission] video: $latest"
else
    echo "[submission] WARN: no assets/video/verdikt-submission*.mp4 found" >&2
fi

echo "[submission] staged $(find "$OUT" -type f | wc -l) files, $(du -sh "$OUT" | cut -f1)"
