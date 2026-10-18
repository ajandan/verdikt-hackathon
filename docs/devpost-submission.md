# Verdikt — Devpost submission artifacts

Staging folder for the Nebius × NVIDIA Global AI Hackathon 2026 submission
(Best Apps & Agents). Rebuild it with `bash scripts/build_submission.sh`;
it wipes and re-copies from the sources below, so never hand-edit anything
here — fix the source and re-run. This page included: its source is
`docs/devpost-submission.md`.

**Submission deadline: 2026-10-30.**

## What Devpost asks for, and where it stands

| Field | Status | Value / blocker |
| --- | --- | --- |
| Public repo URL | **BLOCKED** | `github.com/ajandan/quire-hackathon` is **private** — flip public before the deadline or judges get a 404 |
| Public demo URL | **done** | https://verdikt.aismith.ai — self-hosted, TLS, verified responding |
| Video (< 3 min) | **done** | https://www.youtube.com/watch?v=FF_svj1Y6pk — 2:58.3, under the cap. Local copy in `video/` |
| Licence | done | MIT, `LICENSE`, enforced by `scripts/license_check.py` in CI |
| NVIDIA OSS model | done | NVIDIA Nemotron Nano (triage) + Nemotron Super 120B (verdict, PDF labelling) |
| Nebius hosting | **done** | every model call goes through Nebius Token Factory. The app itself is self-hosted — the rule allows either |
| Image gallery | ready | `screenshots/` — start with `cover.png`, then `product-ui/` |
| Gallery thumbnail | ready | `devpost-thumbnail.jpg` (3000×2000, 3:2, 0.4 MB) — upload as the Devpost thumbnail |
| Video thumbnail | ready | `youtube-thumbnail.jpg` (1280×720, 64 KB) — upload as the custom thumbnail |
| Supporting docs | ready | `slides/` |

The only blocker left is the repo flip. Everything else in this folder is
uploadable as-is.

> Two defects are open on the live site and fixed in `main`, so it needs a
> redeploy before judging: static assets are emitted as `http://` on an
> `https://` page (8 blocked images), and the landing page still points at a
> video file that is not deployed. The landing page now links the published
> YouTube video instead, which removes that dependency entirely.

## Contents

### `slides/`
Rendered from their HTML by `scripts/render_docs_pdf.py` — not exported by
hand. That matters: the repo accumulated `-v2/-v3/-v4` PDFs and the canonical
filename fell three revisions behind its own source, which is how an
old-style deck got staged here once already.

- `verdikt-judges-deck.pdf` — 11-slide deck: problem, the line, architecture,
  business flow, triage, proof, models, value, what ships next.
- `verdikt-one-pager.pdf` — the whole argument on one page.
- `verdikt-one-slide.pdf` — single-slide summary for a judging packet.

### `screenshots/`
- `cover.png` — the one-slide summary as an image. Good Devpost cover.
- `product-ui/` (9) — the running product, and the very same files the landing
  page's carousel serves at https://verdikt.aismith.ai, so the gallery and the
  live site always match. Dashboard, cases, a case's pipeline verdict and its
  audit trail, the rule that decided it, the rules library, programs, the
  HumanLoop review queue that holds every reply until a reviewer approves, and
  renders. 2880×1800, captured by `scripts/capture_console_shots.py`, which
  checks each page for personal addresses, real company names and API keys
  before writing anything.

  The browser E2E screenshots are not shipped: they show test tenants and a
  test API key.
- `case-review/` (4) — one reviewed case, QC-0009, outcome approve, as a
  reviewer sees it. `01`/`02`: the fields read from page 1 and page 2 of the
  scanned enrollment form, 88 of 105 slots filled, misses flagged. `03`/`04`:
  all seven rules evaluated, each with the field values that decided it and
  the match trace. Captured by `scripts/capture_case_shots.py`.
- `architecture/` (13) — the narrative and architecture frames from the
  submission video, one per beat. `beat0d_primary.png` is the five-stage
  pipeline diagram; `beat7_primary.png` is the audit trail.

### `test-demo/`
Everything a judge needs to try Verdikt by email, from any address.
- `verdikt-email-test-guide.pdf` — three pages, step by step: what to send,
  the exact subject and body, which file to attach, what happens next, and
  why no reply arrives until a reviewer approves. Rendered from
  `docs/verdikt-email-test-guide.html` by `scripts/render_docs_pdf.py`.
- `ASP-100-enrollment-maria-chen.pdf`, `ASP-110-income-worksheet-maria-chen.pdf`
  — the fictional packet the guide tells them to attach.

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

### `devpost-thumbnail.png` / `.jpg`
The Devpost gallery card: 3000×2000 (3:2), 0.4 MB as JPEG against Devpost's
5 MB cap. Generated by `scripts/make_devpost_thumb.py` from
`assets/video/designs/devpost-thumb.html`. The headline, a real screenshot of
a decided case with its verdict, the human-review hold, and the five stages
split at the line between AI-in and deterministic-out. Every figure on it is
measured: 934 tests with 0 failing, 0 models on the render path, 5 stages, and
case QC-0009's 7 of 7 rules. Upload the JPEG.

### `youtube-thumbnail.png` / `.jpg`
1280×720, the size and ratio YouTube wants, generated by
`scripts/make_thumbnail.py` from `assets/video/designs/youtube-thumb.html`.
Upload the JPEG — 64 KB against YouTube's 2 MB cap. The composition is three
short lines so it still reads at the ~210px width of a search result.

### Test results
Not shipped as files: raw JUnit output carries machine hostnames, and it was
removed from the repo before going public. The figures on the Proof slide —
**934 tests, 928 passing, 0 failing, 6 skipped** (1 skip + 5 xfail) — come from
a full run on 2026-10-18 against disposable postgres/redis/minio containers,
re-run green after the merge into `main`. `reports/dashboard.html` renders the
per-suite breakdown.

### `video/`
- `verdikt-submission.mp4` — 2:58.3, under the 3:00 cap. Thirteen beats:
  hand-composed slides plus live-UI captures, ElevenLabs narration.
  Gitignored (19 MB) — rebuild with `bash scripts/assemble_video.sh`.
  Staged through a `+faststart` remux so the `moov` atom precedes `mdat`;
  without it a browser cannot begin playing until the whole file has arrived,
  which is what made the landing-page player look broken.

## Not included, on purpose

- `assets/video/screenshots/highlights/` — spotlight scrim overlays used by
  the video assembler. Meaningless as standalone images.
- Anything referencing a real pharmaceutical company. The repo has a few
  scanned-form fixtures used for OCR benchmarking; the deck claims the demo
  data is fictional, so only fictional artifacts ship.
- `tmp/extraction.json` and friends — raw extraction dumps of
  patient-shaped fields. Internal test output, not a submission artifact.
