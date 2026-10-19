# VLM comparison — PDF template labeling and intake extraction

**Date:** 2026-09-16
**Fixtures:** `Form-A.pdf` (104 fields, flat non-AcroForm), `alderwood-enrollment-form.pdf` filled sample (Alderwood AcroForm, 19 slots), `Page4and5.PDF` (real scanned program form B, 19 slots)
**Purpose:** Signal for two decisions — (1) which vision model to use for the PDF field labeler (annotated-numbered-boxes flow); (2) whether to swap the intake-extraction VLM.

Raw per-run outputs live in `.tmp/nebius/` (gitignored). This doc captures the summary, methodology, and decisions so we don't have to re-run to remember why we picked what we picked.

> **Status, 2026-10-18: superseded in part.** The measurements below stand as a
> dated record. Two decisions in §3 have moved on:
>
> - **Intake extraction model:** no longer undecided. NVIDIA
>   `Nemotron-3.5-Super-VL`, now on serverless Token Factory, read 17/19 and
>   19/19 on the same two fixtures against MiniCPM-V-4_5's 14/19 and 17/19, and
>   is wired in as the extraction cross-check. See
>   [the 2026-10-18 benchmark](vlm-extract-nemotron-vl-2026-10-18.md).
> - **NVIDIA vision model:** an NVIDIA VLM is now usable on serverless Token
>   Factory. Nemotron-Nano-V2-12b, the model tested here, still is not.
>
> The extract prompt has changed since this run, so MiniCPM-V's September
> scores are not directly comparable with October's. Both runs are shown side
> by side on the engineering dashboard (Tests › Models).

---

## 1. Template labeling — Form A (chunked + cropped pipeline)

All four candidates run through the production `label_fields_in_pdf` path with `DOCGEN_PDF_LABELER_MODE=vision`. Chunk size = 8 fields; crop pad = 90 px above / 30 px other sides. Annotated numbered-box PNG sent to each VLM with the same `_VISION_LABEL_PROMPT`.

| Model | Hosted on | Labeled | Est. Correct | Time (parallel-4) | License | Notes |
|---|---|---|---|---|---|---|
| **openbmb/MiniCPM-V-4_5** | Nebius Token Factory | 101/104 | ~60/104 (~58%) | **22.8s** | Apache-2.0 | Current production. Fastest. Terser labels ("First", "MI", "ZIP"). |
| **moonshotai/Kimi-K2.7-Code** | Nebius Token Factory | 88/104 | ~62/104 (~60%) | **1234s (~20 min)** | MIT | Richer labels when it works ("First Name", "Middle Initial"). Two chunks failed (p2 timeout, p5 empty response). Not viable for production. |
| **mistralai/pixtral-12b-2409** | api.mistral.ai (off-Nebius) | 101/104 | ~63/104 (~61%) | 87.8s (serial-1) | Apache-2.0 | Comparable accuracy to MiniCPM. Occasional Unicode issues (broken apostrophes). |
| **mistralai/mistral-ocr-latest** | api.mistral.ai (off-Nebius) | N/A | N/A | ~7s | proprietary | Wrong tool for numbered-box labeling — returns whole-line blocks ("Patient Name (First): __ (MI): __ (Last): __") with no per-caption granularity. |

**Systematic failure shared by all three VLMs:** reading-order bias on dense pages. Even with per-chunk cropping, if a chunk's crop region touches the row above, the model attributes the row-above caption to the top box in the chunk. Fix would need either tighter cropping (risks missing legitimate above-captions on sparse pages) or non-box-based labeling (e.g., feed Mistral OCR's markdown into Nemotron for text-only spatial reasoning).

## 2. Intake extraction (pap_pipeline.stages.extract)

Two fixtures, 19 default slots each. `Alderwood filled` is a synthetic clean PDF via Verdikt's own render pipeline; `Form B scan` is a real-world raster scan with typed + handwritten fields.

| Model | Alderwood (synth) | Form B (real scan) | Avg time | Nebius | License |
|---|---|---|---|---|---|
| moonshotai/Kimi-K3 | 100% | **100%** | ~17s | ✓ | Kimi K3 (custom, permissive up to $20M/12mo) |
| moonshotai/Kimi-K2.7-Code | 100% | **100%** | ~27s | ✓ | MIT |
| zai-org/GLM-5.3-Flash | 100% | 94.7% | **~9s** | ✓ | (Z.ai license) |
| MiniMaxAI/MiniMax-M3 | 84.2% | 94.7% | ~9s | ✓ | (Moonshot license) |
| openbmb/MiniCPM-V-4_5 (current) | 78.9% | 84.2% | ~8s | ✓ | Apache-2.0 |
| google/gemma-3-27b-it | 73.7% | 78.9% | ~11s | ✓ | Gemma license |
| moonshotai/Kimi-K2.6 | 100% | **47.4%** | ~22s | ✓ | MIT |

**Notable danger signal — MiniCPM-V on Form B:** returned `date_of_birth = 1949-07-07` instead of `1990-07-07` (off by 40 years on a healthcare form). Kimi-K2.6 looked like a winner on synthetic (100%) but collapsed on scans (47%). Never trust a single-fixture benchmark.

## 3. Decisions

| Decision | Choice | Reason |
|---|---|---|
| PDF template labeler VLM | Stay on `openbmb/MiniCPM-V-4_5` | Fastest, on Nebius, accuracy comparable to Pixtral. Kimi disqualified by 50× slowdown + chunk failures. |
| Template labeler mode | `DOCGEN_PDF_LABELER_MODE=vision` (new flag added 2026-09-16) | Vision path with chunked+cropped annotated boxes beats text-only Nemotron on flat non-AcroForm templates. **Validated end-to-end via portal on 2026-09-16:** Form A went from 24/104 (auto/text-only) → 98/104 mostly-correct (vision+chunk+crop). Shipped. |
| Intake extraction VLM | ~~Undecided~~ **Superseded 2026-10-18: Nemotron-3.5-Super-VL benchmarked and added as the cross-check; see status note.** Original entry: **Undecided** — MiniCPM-V is weakest on both fixtures; four Nebius alternatives (Kimi-K3, Kimi-K2.7-Code, GLM-5.3-Flash, MiniMax-M3) score higher. Kimi-K3 = best raw accuracy; GLM-5.3-Flash = best speed/accuracy trade. Not yet swapped because (a) hackathon narrative already reads as "3 NVIDIA text + 1 OSS vision complement" and MiniCPM keeps it clean, (b) only tested on 2 fixtures. |
| Nemotron-Nano-V2-12b (NVIDIA VLM) | Not usable | Listed in Nebius catalog but returns 404 on all 12 ID variants × 2 base URLs. Requires dedicated-endpoint provisioning (paid per-hour). Blocked until Nebius adds it to serverless. **2026-10-18:** this model is still unavailable, but NVIDIA `Nemotron-3.5-Super-VL` is on serverless and usable. |

## 4. Code changes made 2026-09-16

- `src/docgen/config.py` (`Settings.pdf_labeler_mode`) — added `pdf_labeler_mode: Literal["auto","vision","text"] = "auto"`
- `src/docgen/designtime/pdf_labeler_backend.py` — surfaced `labeler_mode` in the backend picker dict
- `src/docgen/designtime/pdf_field_detector.py`:
  - added `_VISION_CHUNK_SIZE = 8` and `_VISION_CROP_PAD_ABOVE_PX/OTHER_PX` constants
  - added `_crop_annotated_to_chunk()` helper
  - `label_fields_in_pdf()` accepts `labeler_mode` and routes pages to vision / text / auto
  - `_visual_label_chunk()` replaces the old per-page vision call — chunks each page's fields into groups of 8 sorted by (y, x), crops the annotated PNG to each chunk's region + padding
- `src/docgen/inbox/agent.py` + `src/docgen/ui/routes.py` (both `label_fields_in_pdf` call sites) — threaded `labeler_mode` into every `label_fields_in_pdf` call
- `deploy/.env.example` — documented `DOCGEN_PDF_LABELER_MODE`
- `docker-compose.yml` — exposed `DOCGEN_PDF_LABELER_MODE` and `DOCGEN_PDF_VISION_OCR_MODEL` on `web` and `worker`
- `.env` — set `DOCGEN_PDF_LABELER_MODE=vision` (local dev)

## 5. Reproducing the benchmarks

The harnesses and fixtures are kept outside the repo, under the gitignored
`.tmp/nebius/`: one fixture is a real filled-in form, so neither it nor the
scripts that read it are published. They cover the four-model labeler run on
Form A, the Mistral OCR spatial-match attempt, the per-page diagnostic
overlays, and the seven-model intake sweep on both fixtures.

The published numbers come from their raw JSON output through
`reports/_gen_models_data.py`, which writes `reports/models-data.json` for the
engineering dashboard and withholds every value read from the real form.

All require `DOCGEN_AI_API_KEY` (Nebius) and, for Mistral models, `MISTRAL_API_KEY` passed inline.
