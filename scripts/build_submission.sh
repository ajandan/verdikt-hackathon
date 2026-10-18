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
#   - .tmp/ fixtures built from real program forms; every shipped artifact
#     uses the fictional Alderwood/Cardiovita data the deck promises
#   - tmp/extraction.json, tmp/alderwood_intake.json
#     raw extraction dumps full of patient-shaped fields; internal, not a
#     submission artifact
set -euo pipefail
cd "$(dirname "$0")/.."
FINAL=submission
# Build into a staging directory and swap it in at the end, rather than
# deleting the live folder first. On Windows a PDF open in a viewer holds an
# exclusive lock on that one file; deleting in place then fails part-way and
# leaves a half-built folder (and `set -e` aborts before anything is restored).
# A directory rename does not need access to the files inside it, so the swap
# works with the deck open -- which is the normal case, since someone reviewing
# the deck is exactly who re-runs this.
OUT="$FINAL.new"
rm -rf "$OUT"

mkdir -p "$OUT"/{slides,screenshots/product-ui,screenshots/architecture,test-demo,sample-outputs/templates,video}

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
# Product UI tour: the 11 screens a judge would click through, including the
# HumanLoop queue that backs the human-review claim.
cp reports/ui-audit-shots/*.png  "$OUT/screenshots/product-ui/"
# The browser E2E screenshots are deliberately not shipped: they showed test
# tenants and a test API key. scripts/browser_e2e.py still writes them locally.
# Architecture / narrative frames from the submission video (1x only; the @2x
# pairs are for the video pipeline, not for a gallery).
for f in assets/video/screenshots/beat*_primary.png; do
    case "$f" in *@2x*) continue;; esac
    cp "$f" "$OUT/screenshots/architecture/"
done

# --- sample outputs (the "test PDFs") -------------------------------------
# Real pipeline output: one reply package per verdict outcome.
# Tracked copies, so a clean clone can build this folder. They used to be read
# from tmp/generated_packages/, scratch output that only existed on one machine.
cp samples/reply-packages/reply-approve.pdf      "$OUT/sample-outputs/"
cp samples/reply-packages/reply-deny.pdf         "$OUT/sample-outputs/"
cp samples/reply-packages/reply-missing-info.pdf "$OUT/sample-outputs/"
# The frozen, human-approved templates those replies are merged into, plus the
# blank intake form the pipeline reads.
cp sample_templates/alderwood/alderwood-pap-approval-ASP-201.pdf     "$OUT/sample-outputs/templates/"
cp sample_templates/alderwood/alderwood-pap-denial-ASP-202.pdf       "$OUT/sample-outputs/templates/"
cp sample_templates/alderwood/alderwood-pap-missing-info-ASP-203.pdf "$OUT/sample-outputs/templates/"
cp sample_templates/alderwood/alderwood-enrollment-form.pdf          "$OUT/sample-outputs/templates/"

# --- evidence -------------------------------------------------------------
# No raw test output ships: JUnit XML carries machine hostnames, and the raw
# reports were removed from the repo before it went public. The README states
# the numbers; reports/dashboard.html renders them.
# --- test demo: everything a judge needs to try it by email -----------------
# The step-by-step guide (rendered from docs/verdikt-email-test-guide.html) and
# the two attachments it tells them to send.
cp docs/verdikt-email-test-guide.pdf                    "$OUT/test-demo/"
cp samples/email-test/ASP-100-enrollment-maria-chen.pdf  "$OUT/test-demo/"
cp samples/email-test/ASP-110-income-worksheet-maria-chen.pdf "$OUT/test-demo/"

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

# --- swap the staged tree into place --------------------------------------
TRASH="$FINAL.old.$$"
if [[ ! -d "$FINAL" ]]; then
    mv "$OUT" "$FINAL"
elif mv "$FINAL" "$TRASH" 2>/dev/null; then
    mv "$OUT" "$FINAL"
    # Best-effort: a locked file in the old tree must not fail the build, since
    # the new tree is already in place by this point.
    rm -rf "$TRASH" 2>/dev/null ||         echo "[submission] note: could not delete $TRASH (a file in it is open)" >&2
else
    # The directory itself is held open -- an Explorer window inside it, or a
    # shell whose cwd is in it. A rename needs a handle on the directory; a
    # file copy does not. Sync in place instead so an open folder cannot block
    # a rebuild, which it otherwise does every time someone is reading the deck.
    echo "[submission] $FINAL/ is held open; syncing in place instead of swapping" >&2
    (cd "$OUT" && find . -type d -exec mkdir -p "../$FINAL/{}" \; )
    # cp fails on a file currently open in a viewer. Skipping it quietly
    # would leave a stale artifact staged for submission, so each failure
    # is named and the build exits non-zero.
    sync_failed=0
    while IFS= read -r f; do
        # An open viewer locks the file it is showing. If that file is
        # already byte-identical there is nothing to do and nothing to warn
        # about; only a genuine change that cannot be written matters.
        if cmp -s "$OUT/$f" "$FINAL/$f" 2>/dev/null; then
            continue
        fi
        cp -f "$OUT/$f" "$FINAL/$f" 2>/dev/null || {
            echo "[submission] LOCKED, needs updating but could not be written: $f" >&2
            sync_failed=$((sync_failed + 1))
        }
    done < <(cd "$OUT" && find . -type f -printf "%P\n")
    # Drop anything the new build no longer contains, ignoring what is locked.
    (cd "$FINAL" && find . -type f -print) | while read -r f; do
        [[ -f "$OUT/$f" ]] || rm -f "$FINAL/$f" 2>/dev/null || true
    done
    rm -rf "$OUT" 2>/dev/null || true
fi
OUT="$FINAL"
if [[ "${sync_failed:-0}" -gt 0 ]]; then
    echo "[submission] $sync_failed file(s) could not be replaced -- close them and re-run." >&2
    exit 1
fi

# --- YouTube thumbnail ----------------------------------------------------
# After the swap, so it lands in the real folder and survives it.
python scripts/make_thumbnail.py

echo "[submission] staged $(find "$OUT" -type f | wc -l) files, $(du -sh "$OUT" | cut -f1)"
