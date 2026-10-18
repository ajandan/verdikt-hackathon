# Verdikt — Devpost submission artifacts

Staging folder for the Nebius × NVIDIA Global AI Hackathon 2026 submission
(Best Apps & Agents). Rebuild it with `bash scripts/build_submission.sh`;
it wipes and re-copies from the sources below, so never hand-edit anything
here — fix the source and re-run.

**Submission deadline: 2026-10-30.**

## What Devpost asks for, and where it stands

| Field | Status | Value / blocker |
| --- | --- | --- |
| Public repo URL | **BLOCKED** | `github.com/ajandan/quire-hackathon` is **private** — flip public before the deadline or judges get a 404 |
| Public demo URL | **BLOCKED** | Nebius deploy not cut yet; runbook is `docs/deploy-nebius.md` |
| Video (< 3 min) | ready, not uploaded | `video/verdikt-submission.mp4` — 2:58.3, needs a YouTube/Vimeo URL |
| Licence | done | MIT, `LICENSE`, enforced by `scripts/license_check.py` in CI |
| NVIDIA OSS model | done | NVIDIA Nemotron Nano (triage) + Nemotron Super 120B (verdict, PDF labelling) |
| Nebius hosting | partial | all model calls already go through Nebius Token Factory; app not yet hosted on Nebius |
| Image gallery | ready | `screenshots/` — start with `cover.png`, then `product-ui/` |
| Supporting docs | ready | `slides/` |

The two blockers are the repo flip and the demo URL. Everything else in this
folder is uploadable as-is.

## Contents

### `slides/`
- `verdikt-judges-deck.pdf` — 11-slide deck: problem, the line, architecture,
  business flow, triage, proof, models, value, what ships next.
- `verdikt-one-pager.pdf` — the whole argument on one page.
- `verdikt-one-slide.pdf` — single-slide summary for a judging packet.

### `screenshots/`
- `cover.png` — the one-slide summary as an image. Good Devpost cover.
- `product-ui/` (12) — the product as a judge would click it: templates,
  dashboard, programs, rules, admin, packages, inbox, renders.
  `10-inbox-humanloop.png` is the human-review queue that holds a reply
  until a reviewer approves it.
- `e2e-walkthrough/` (11) — a real browser end-to-end run, in order:
  admin session → tenant → models → triage → program → slot → dispatch a
  case → verify the verdict → cleanup. Captured by `scripts/browser_e2e.py`.
- `architecture/` (13) — the narrative and architecture frames from the
  submission video, one per beat. `beat0d_primary.png` is the five-stage
  pipeline diagram; `beat7_primary.png` is the audit trail.

### `sample-outputs/`
The deterministic half of the product, as actual files.
- `reply-approve.pdf`, `reply-deny.pdf`, `reply-missing-info.pdf` — one real
  reply package per verdict outcome, as the pipeline emitted them.
- `templates/` — the frozen, human-approved templates those replies are
  merged into (ASP-201/202/203), plus `alderwood-enrollment-form.pdf`, the
  blank intake form the pipeline reads at the other end.

Every document uses the fictional Alderwood Specialty Pharmacy / Cardiovita
demo tenant on the reserved `.example` domain. No real patient, payer or
manufacturer data appears in any artifact here.

### `test-evidence/`
- `junit-results.xml` — the JUnit XML behind the deck's Proof slide.
- `test-report.xlsx` — the same run as a spreadsheet.

See `reports/dashboard.html` for the rendered version, and
`reports/_gen_dashboard.py` for how that JSON is produced.

### `video/`
- `verdikt-submission.mp4` — 2:58.3, under the 3:00 cap. Thirteen beats:
  hand-composed slides plus live-UI captures, ElevenLabs narration.
  Gitignored (19 MB) — rebuild with `bash scripts/assemble_video.sh`.

## Not included, on purpose

- `assets/video/screenshots/highlights/` — spotlight scrim overlays used by
  the video assembler. Meaningless as standalone images.
- Anything referencing a real pharmaceutical company. The repo has a few
  scanned-form fixtures used for OCR benchmarking; the deck claims the demo
  data is fictional, so only fictional artifacts ship.
- `tmp/extraction.json` and friends — raw extraction dumps of
  patient-shaped fields. Internal test output, not a submission artifact.
