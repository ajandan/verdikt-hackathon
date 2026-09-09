# Verdikt — Nebius × NVIDIA Global AI Hackathon submission

**Track:** Best Apps & Agents
**Repo:** <https://github.com/ajandan/quire-hackathon> (flips public before 2026-10-30)
**License:** MIT — see [`LICENSE`](../LICENSE)
**Submission window:** 2026-08-26 → 2026-10-30

## What we're submitting

**Verdikt** is a decision pipeline for regulated document workflows. When a Patient Assistance Program (PAP) enrollment packet arrives by email, Verdikt reads the attachments, retrieves the program's eligibility rules, and issues a structured decision — `approve`, `deny`, or `missing_info` — with citations back to the rules that fired. The reply letter is a byte-identical merge of a human-approved template with that decision.

We took the opposite bet from the industry: **AI-in, deterministic-out.** AI runs where it earns its place — reading documents, retrieving rules, reasoning over both. Then AI stops. No language model sits on the code path that generates the audited artifact. Full architectural argument in [`docs/demo-scripts/architecture-justification.md`](demo-scripts/architecture-justification.md).

## Reuse declaration

This submission is a hybrid — a hackathon-window pipeline (Verdikt) composed onto a pre-existing doc-gen kernel (Quire). Mechanical boundaries make the split provable.

**Prior work — Quire kernel (`src/docgen/`).** Design-time AI mapping, deterministic render path, freeze/publish, generic inbox agent, provider abstraction, multi-tenancy. Predates 2026-08-26. Modified during the submission window only for (a) the Nebius Token Factory provider adapter, (b) moving the PDF field labeler to a text-only NVIDIA Nemotron model on Nebius Token Factory, and (c) one extension point that lets Verdikt plug into the generic inbox agent.

**Submission-window work — Verdikt (`src/pap_pipeline/`).** The three-stage decision pipeline (extract → retrieve → reason), the intake / rule / decision schemas, the four eligibility rules for the Cardiovita demo program, PAP-specific templates, routing-table instrumentation, the orchestrator, and the OncoBridge cameo rules pack. All work landed after 2026-08-26.

Provable mechanically:

- `git log -- src/pap_pipeline/` returns only submission-window commits.
- `tests/test_boundaries.py::test_docgen_never_imports_pap_pipeline` fails the build if Quire ever reaches back into Verdikt. The dependency direction is one-way: Verdikt imports Quire; Quire never imports Verdikt.
- `tests/test_boundaries.py::test_core_never_imports_ai_layer` fails the build if the deterministic render path ever imports an AI provider. The "no model at render time" claim is enforced, not asserted.

## What the hackathon requires, and where it's satisfied

| Requirement | Satisfied by |
| --- | --- |
| Runs on Nebius Token Factory / Nebius AI Cloud | Reasoning stage uses NVIDIA Nemotron on Nebius Token Factory (`src/docgen/providers/nebius.py`); retrieval uses NeMo Retriever NIMs; extraction uses `nv-ingest` on a Nebius AI Cloud VM. Deploy runbook: [`docs/deploy-nebius.md`](deploy-nebius.md). |
| ≥ 1 NVIDIA open-source model | Three, one per stage: `nv-ingest` (multimodal extraction), NeMo Retriever NIMs (`nv-embedqa-e5-v5` + `nv-rerankqa-mistral-4b-v3`), and Nemotron Super (reasoning). All published under the `nvidia/` namespace. The Nemotron default is enforced by `tests/test_provider_nebius.py::test_nebius_defaults_target_nvidia_nemotron` — the default model name must start with `nvidia/`. |
| Approved OSS license | MIT — see [`LICENSE`](../LICENSE). |
| Public repo, public demo URL, README | This repo (public by 2026-10-30) + [`README.md`](../README.md) + Devpost-published demo URL. |
| YouTube video < 3 min | Architecture-beat script in [`docs/demo-scripts/video-narration-60s.md`](demo-scripts/video-narration-60s.md); link added post-recording. |

## The three-stage Verdikt architecture

Three NVIDIA-family models on one Nebius key, chosen by task shape at each stage:

| Stage | What runs | Why this model, why here |
| --- | --- | --- |
| **Extract** | `nv-ingest` on a Nebius AI Cloud VM | Multimodal PDF extraction over messy, mixed-modality attachments — pay stubs, insurance cards, prescriptions, denial letters. Vision handles what regex cannot. Nothing else runs at this stage. |
| **Retrieve** | NeMo Retriever NIMs — `nv-embedqa-e5-v5` + `nv-rerankqa-mistral-4b-v3` | Embedding + rerank surface the eligibility rules that apply to *this* case, from a pgvector store populated at startup from `src/pap_pipeline/rules/*.yaml`. The rules are human-authored and human-versioned; the model's job is to find, not to invent. |
| **Reason** | Nemotron Super (non-reasoning mode) on Nebius Token Factory | Structured-JSON verdict over the retrieved rules. Non-reasoning mode is deliberate — the reasoning variant burns output tokens on trace and returns empty content on this task shape. |

Every model call is instrumented (`src/pap_pipeline/routing.py`) with model name, tokens in/out, wall-clock latency, and per-call cost. The routing-table admin page at `/ui/admin/routing` renders the resulting matrix — real numbers per call site, per stage, defensible as evidence rather than assertion.

## What Quire contributes (and why it matters here)

The deterministic-render boundary is the reason Verdikt lands on Quire rather than on a fresh substrate. `docgen/core/` (render path) does not import `docgen/providers/` (AI). The boundary is enforced in tests, not in comments. Verdikt produces a structured `Decision`; Quire renders that Decision into the reply letter through a byte-identical merge over a frozen `TemplateVersion`. No model runs at render time. No model can. That is what makes *"same input, same output, forever"* a defensible engineering claim rather than marketing.

The rest of the Quire kernel — multi-tenancy, provider abstraction, freeze/publish lifecycle, generic inbox agent — carries the operational weight so Verdikt can focus on the pipeline itself. One extension point on `docgen.inbox.agent` (a `use_pipeline_backend` config flag) is all it takes to route intake through Verdikt without touching the substrate.

## Judging-criteria mapping

**Technical implementation.** Three NVIDIA models composed at their strongest task shapes, on one Nebius key, with per-call routing instrumentation that turns "why Nebius/NVIDIA?" into a data question. Deterministic render boundary enforced in tests. Provable one-way dependency (`docgen ← pap_pipeline`, never reverse) prevents the substrate from silently contaminating the submission-window work. Every inter-stage artifact is a structured pydantic model — the pipeline is independently testable at each seam without spinning up the whole stack.

**Design (complete product).** Verdikt is observable end-to-end. Every stage's output is a structured artifact a human reviewer can read: the intake object, the retrieved rules with rerank scores, the decision with citations, the rendered letter. The admin routing-table page surfaces the model-selection decisions as live data. A first-run path takes a stranger from empty inbox to rendered approval letter without maintainer intervention.

**Potential impact.** Patient Assistance Programs are one instance of a class — regulated document workflows where the letter that leaves the building is the audit artifact. Prior authorization, benefit verification, informed consent, MedWatch reporting all share the same shape. The pipeline structure generalizes; PAP is the demo. Onboarding a second program is a data change, not a build — proven by the OncoBridge cameo: same email, same code, different rules pack, opposite verdict.

**Quality of idea.** Most 2026 doc-gen submissions put a language model on the artifact-generation code path and hope the compliance officer signs off. Verdikt refuses that path: **AI-in, deterministic-out.** The EU AI Act categorizes healthcare eligibility decisions as high-risk and demands the exact reproducibility guarantees Verdikt provides mechanically. Building audit-defensible in 2026 is *ahead* of the regulatory curve, not behind the capability curve.

## Running locally

```bash
cp .env.example .env
# fill DOCGEN_SESSION_SECRET, DOCGEN_WEBHOOK_SIGNING_SECRET
# set DOCGEN_AI_PROVIDER=nebius and DOCGEN_AI_API_KEY=<Nebius Token Factory key>
docker compose up -d postgres redis minio minio-init
docker compose run --rm web alembic upgrade head
docker compose up -d web worker
open http://localhost:8000/ui/
```

For the public deploy, see [`docs/deploy-nebius.md`](deploy-nebius.md).

## Open items before 2026-10-30

- **Repo visibility.** Currently PRIVATE (set 2026-09-07). Flip public before submission.
- **Complete the pymupdf replacement.** `pyproject.toml` declares `AGPL-3.0-or-later` because `pymupdf` (AGPL) is a runtime dep — see [`docs/adr/0001-adopt-agpl-for-pymupdf.md`](adr/0001-adopt-agpl-for-pymupdf.md). The hackathon rules ([verified 2026-09-09](https://nebiusglobalaihackathon.devpost.com/rules)) require an open-source license with permissive examples (Apache 2.0 / MIT / MPL 2.0); AGPL is technically arguable under "such as" but not worth the disqualification risk. Execute ADR-0001 Option 2 — swap pymupdf for `pypdfium2` (Apache-2.0) + `pdfplumber` (MIT) — then flip `pyproject.toml` to MIT and retire the ADR. Scheduled into Phase 1 wrap-up (see submission roadmap).
- **Public demo URL.** A minimal Verdikt hackathon-demo deploy on Nebius, decoupled from the production Quire deploy scheduled for mid-November. See roadmap Phase 3.
- **YouTube video.** 3-minute cut, script in [`docs/demo-scripts/video-narration-60s.md`](demo-scripts/video-narration-60s.md), including the 15-second second-program cameo beat.
- **Devpost submission.** URL, repo link, video, and this document as the technical write-up.

## Companion documents

- [`docs/demo-scripts/architecture-justification.md`](demo-scripts/architecture-justification.md) — the pipeline-vs-agents argument, README-ready.
- [`docs/demo-scripts/submission-roadmap.md`](demo-scripts/submission-roadmap.md) — the 50-day plan with phase gates and the product-name decision.
- [`docs/demo-scripts/video-narration-60s.md`](demo-scripts/video-narration-60s.md) — the 60-second architecture beat plus the 15-second cameo script, with delivery + visual-sync notes.
- [`docs/demo-scripts/judge-qa-briefing.md`](demo-scripts/judge-qa-briefing.md) — live-Q&A material (kept out of public-facing docs).
- [`docs/deploy-nebius.md`](deploy-nebius.md) — Nebius AI Cloud deploy runbook.
