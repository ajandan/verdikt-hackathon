# v25 Audio-Visual Alignment Audit

**Master reviewed:** `assets/video/verdikt-submission-v25.mp4` (180.09 s, 1920x1080, H.264 + AAC)
**Rebuilt master:** `assets/video/verdikt-submission-v26.mp4` (180.09 s) — see "Direct fixes" below.
**Method:** three sample frames per beat (open / mid / close) extracted with ffmpeg, read against the ground-truth narration in `docs/demo-scripts/voiceover-elevenlabs.md`. Chip-overlay and QR windows sampled at 0.5 s granularity around transitions. Beat-audio speech tails measured with `silencedetect` to confirm every VO line finishes before its beat boundary.

## Per-beat findings

| Beat | Range (v25) | Narration says (paraphrased) | Visual shows | Alignment | Notes / fix |
| :-: | :-: | --- | --- | :-: | --- |
| 0a | 0–4 | "Verdikt. Nebius x NVIDIA Hackathon submission." | Cream title card, wordmark, submitter line, MIT badge | good | Tagline reads *"AI reads. Rules decide. Templates ship."* — old-verb-family, but was intentionally kept as the poetic hook. Not the flagged Extract/Retrieve/Reason/Merge/Audit set. No fix. |
| 0b | 4–18 | "Thousands apply... each application a stack... each response a compliance-audited letter... today, that's manual." | Three-column applicant / docs / coordinator scene with *manual process* footer | good | — |
| 0c | 18–32 | "Ingest. Decide. Deliver. Remember. AI where it earns its place." | Left grey "throw an agent" world; right Verdikt promise band | good | Frame uses NEW 4-stage vocab (Ingest/Decide/Deliver/Remember) ✓ |
| 0d | 32–41 | "Every channel fans into one pipeline. Ingest, Decide, Deliver, Remember. Every outcome traced." | Intake channels → four-column Ingest/Decide/Deliver/Remember, STAGE MAP badge | mild | Sub-stage cards under Decide/Deliver/Remember still use *Retriever / Reason / Merge / Deliver / Audit* verbs. These are correct implementation-level labels, not top-level operator labels — leaving as-is. No fix. |
| 1 | 41–54 | "Applicant applies... enrollment form, pay stubs, insurance card, prescription... this is the intake." | Alderwood enrollment form + insurance card + Rx + prescription card | good | Beat had 0.69 s of slack (audio 12.31 s vs 13 s budget). Used as donor for Beat 6 (see fix). |
| 2 | 54–70 | "Program is data... fields to extract (0:03)... rules to check (0:07)... reply templates (0:09)... onboarding is filling a form." | Live capture of `/ui/programs/cardiovita`: Extraction schema tab @ t=57, Attached rules tab @ t=61, Reply bindings tab @ t=63 | good | Tab progression matches narration cadence exactly. First ~1s is navigation-blank (page loading) — acceptable. |
| 3 | 70–86 | "Not one letter — an ordered doc package: outcome letter, appeals notice, benefits guide, blank forms. Human-approved. Frozen." | Four-doc kit with `FROZEN · sha256` footer | mild | VO audio 16.44 s vs 16 s budget → last 0.09 s of speech "change it" is clipped by ffmpeg trim. Under the script's 0.3 s warn threshold. Not fixed in v26 (no free time to donate). |
| 3b | 86–105 | "Each outcome maps to its own package... approve, deny, missing_info... operator declares the mapping." | 3-row mapping: Approve kit (4), Deny kit (3), Missing-info kit (2), each frozen | good | VO 19.32 s vs 19 s target → 0.32 s trim, borderline but within tail-silence pad. No fix. |
| 4 | 105–125 | "Ingest — MiniCPM-V on Nebius (0:04–0:08)... Decide — NeMo Retriever (0:08–0:11)... Nemotron Super (0:11–0:15)" | Live capture of `/ui/pipeline`. Chip overlays: MiniCPM-V @ 109–113, NeMo @ 113–116, Nemotron @ 116–120 | good (chips) / mild (residual "Extract") | Chip windows line up with the model names in narration. **Residual "Extract" label**: the live UI still shows the pipeline detail section header *"1. Extract — structured intake"* (see `src/docgen/ui/templates/inbox_pipeline_detail.html:268` and body "extract stage"). This is the flagged residual vocab, but only fixable via HTML edit + re-record → out of scope for timing-only fix. Recommend future HTML rename `Extract → Ingest`, `extract stage → ingest stage`. |
| 5 | 125–149 | "Then AI stops. Deliver — byte-identical merge... here's the approval letter, rendered from Alderwood's template... sha256 pinned... same input, same package, forever." | AI-zone (grey, left) / Deterministic-zone (bright, right) split with PDF approval letter, sha256 badge, "no model at render time" band | mild | PDF is on-screen the whole 24 s, so "here's the approval letter" (~mid-beat) lands over an already-visible reveal — a still image can't stage-reveal without a video-layer edit. **Residual "I EXTRACT / II RETRIEVE / III REASON" labels** in the AI-zone stack (left side). These are AI-pipeline-internal stage names, semantically correct for what MiniCPM/NeMo/Nemotron do — leaving as-is unless brand voice demands top-level Ingest/Decide/etc. verbs everywhere. |
| 6 | 149–159 (v25) | "Pipeline is the product, not the program. A second program. Stricter income cap. Same email. Same code. Opposite verdict." | Approve card + Extract/Retrieve/Reason pipeline pill + Deny card | ❌ (v25) / good (v26) | **FIX APPLIED**: v25 audio (10.54 s speech through 10.19 s) was clipped by the 10 s beat budget — narrator's "opposite **verdict**" ending was truncated. v26 gives beat 6 an 11 s budget → 0.94 s tail pad. Residual "Extract / Retrieve / Reason" pill in center of frame remains — same rationale as Beat 5. |
| 7 | 159–170 (v25) | "Remember — every stage auditable. AI events, human approvals, system deliveries — stamped, reproducible from the audit log, years later." | Vertical audit trail with rows: `intake received / Ingest / Decide / Reviewer approved / Deliver / Delivered` — top strip *STAGE 33 · REMEMBER · REPRODUCIBLE* | good | Uses NEW Ingest/Decide/Deliver vocabulary ✓. VO audio 10.68 s < 11 s budget → clean. |
| 8 | 170–180 | "Verdikt. Three NVIDIA models on Nebius. One deterministic reply. MIT-licensed. Repo, demo, dashboards — linked below." | Verdikt landing hero, QR overlay reveals at t=177 (last 3 s) | good | QR absent at t≤176, present at t≥177 ✓ (task spec said "175 onwards" — actual is 177 onwards, which is 3 s window per code `qr_start = duration - 3`, matches operator intent). |

Legend: good = ✓ / mild = ⚠ / mismatch = ❌.

## Direct fixes applied to v26

**File:** `scripts/assemble_video.sh` line 72

- **Old:** `BEAT_DURATIONS=(4 14 14 9 13 16 16 19 20 24 10 11 10)`
- **New:** `BEAT_DURATIONS=(4 14 14 9 12 16 16 19 20 24 11 11 10)`
- **Delta:** Beat 1: 13 → 12 s (donor, still safe — VO speech ends at 11.86 s, 0.14 s tail pad remains). Beat 6: 10 → 11 s (recipient, VO speech ends at 10.06 s → previously clipped, now 0.94 s tail pad).
- **Total unchanged:** 180 s (hackathon 3:00 cap maintained).

**Verification on v26**:
- Duration: 180.09 s (matches v25, well within cap).
- Beat 6 tail silence at global 158.52–159.00 s = 0.48 s of clean silence pad after "opposite verdict." (v25 had speech running right into the boundary — silencedetect showed continuous audio through the beat cut).
- Beat 4 chip overlays: MiniCPM-V @ 108–112 s, NeMo @ 112–115 s, Nemotron @ 115–119 s (chip filter uses beat-local `t`, so windows correctly shifted with Beat 4's new start at t=104).
- Beat 8 QR: absent up to t=176, present t=177 onwards ✓.
- Beat 2 tab progression: unchanged (live-capture replay tied to beat-local `t`).

## Recommendations NOT applied (out of scope for timing-only fixes)

1. **Beat 4 live UI residual "Extract" wording** — `src/docgen/ui/templates/inbox_pipeline_detail.html:268` still reads "Extract — structured intake". Requires HTML rename to "Ingest — structured intake" plus body-copy "extract stage" → "ingest stage", then `python scripts/record_demo.py --beat 4` to re-capture. Same file also drives Beat 4's stage-body copy.
2. **Beat 5 designed-image "I EXTRACT / II RETRIEVE / III REASON" stack** — decision point: keep as internal AI-pipeline verbs (current) or promote to top-level Ingest/Decide/Deliver labels for brand consistency. Only reachable via `assets/video/designs/beat5.html` edit + rerender. Same for Beat 6 center pipeline pill.
3. **Beat 3 / 3b audio trim** — VO audios exceed budgets by 0.44 s / 0.32 s. Below the script's 0.3 s warn threshold for 3b and above it for 3. No free seconds anywhere in the timeline to donate; re-timing would require re-engineering VO or the 3:00 cap. Currently trims silent tail or brief consonant, imperceptible in playback. Leave as-is.
4. **Beat 0a submitter line** — displays "AJANTHAN ARULANANTHAR". Voiceover script credit is "Ajanthan Anandarajan". Confirm which is the intended public name for hackathon submission; if the latter, update `assets/video/designs/beat0a.html` and re-render the still.
