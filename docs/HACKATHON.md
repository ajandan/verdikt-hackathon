# Verdikt — Nebius × NVIDIA Global AI Hackathon submission

**Track:** Best Apps & Agents
**Repo:** <https://github.com/ajandan/verdikt-hackathon> (flips public before 2026-10-30)
**License:** MIT — see [`LICENSE`](../LICENSE)
**Submission window:** 2026-08-26 → 2026-10-30

## What we're submitting

**Verdikt** is a decision pipeline for regulated document workflows. When a Patient Assistance Program (PAP) enrollment packet arrives by email, Verdikt reads the attachments, retrieves the program's eligibility rules, and issues a structured decision — `approve`, `deny`, or `missing_info` — with citations back to the rules that fired. The reply that leaves the building is an **ordered doc package** for that outcome: a byte-identical letter rendered from a human-approved template, concatenated with the tenant's static appeals notices, benefits guides, and blank forms in the order the operator declared. Same input → same package → forever.

We took the opposite bet from the industry: **AI-in, deterministic-out.** AI runs where it earns its place — reading documents, retrieving rules, reasoning over both. Then AI stops. No language model sits on the code path that generates the audited artifact. Full architectural argument in [`docs/demo-scripts/architecture-justification.md`](demo-scripts/architecture-justification.md).

## Reuse declaration

This submission is a hybrid — a hackathon-window pipeline (Verdikt) composed onto a pre-existing doc-gen kernel. Mechanical boundaries make the split provable.

**Prior work — the doc-gen kernel (`src/docgen/`).** Design-time AI mapping, deterministic render path, freeze/publish, generic inbox agent, provider abstraction, multi-tenancy. Predates 2026-08-26. Modified during the submission window only for (a) the Nebius Token Factory provider adapter, (b) moving the PDF field labeler to a text-only NVIDIA Nemotron model on Nebius Token Factory, and (c) one extension point that lets Verdikt plug into the generic inbox agent.

**Submission-window work — Verdikt (`src/pap_pipeline/`).** The three-stage decision pipeline (extract → retrieve → reason), the intake / rule / decision schemas, the four eligibility rules for the Cardiovita demo program, PAP-specific templates, routing-table instrumentation, the orchestrator, and the OncoBridge cameo rules pack. All work landed after 2026-08-26.

Provable mechanically:

- `git log -- src/pap_pipeline/` returns only submission-window commits.
- `tests/test_boundaries.py::test_docgen_never_imports_pap_pipeline` fails the build if the kernel ever reaches back into the pipeline. The dependency direction is one-way: the pipeline imports the kernel; the kernel never imports the pipeline.
- `tests/test_boundaries.py::test_core_never_imports_ai_layer` fails the build if the deterministic render path ever imports an AI provider. The "no model at render time" claim is enforced, not asserted.

## What the hackathon requires, and where it's satisfied

| Requirement | Satisfied by |
| --- | --- |
| Runs on Nebius Token Factory | Every model call goes through one Nebius Token Factory endpoint (`src/docgen/providers/nebius.py`): MiniCPM-V for reading (Nemotron-3.5-Super-VL as an optional second reader), Nemotron Nano for triage, Qwen3-Embedding for retrieval, Nemotron Super for the rules. The application itself is self-hosted; the rule names Token Factory as an alternative to Nebius AI Cloud, and all inference goes through it. |
| ≥ 1 NVIDIA open-source model | Three NVIDIA Nemotron models, all under the `nvidia/` namespace: Nemotron Nano (triage), Nemotron Super (turns rules into formulas; reasons over rules without one) and Nemotron-3.5-Super-VL (optional extraction cross-check). The Nemotron default is enforced by `tests/test_provider_nebius.py::test_nebius_defaults_target_nvidia_nemotron` — the default model name must start with `nvidia/`. |
| Approved OSS license | MIT — see [`LICENSE`](../LICENSE). |
| Public repo, public demo URL, README | This repo (public) + [`README.md`](../README.md) + Devpost-published demo URL. |
| YouTube video < 3 min | Published: [https://youtu.be/EleEatbaEWM](https://youtu.be/EleEatbaEWM). Full shooting script in [`docs/demo-scripts/video-3min.md`](demo-scripts/video-3min.md) — five beats, two engineered applause lines. |

## The three-stage Verdikt architecture

Five open models on one Nebius key, chosen by task shape at each stage (the Deliver and Remember stages use no model):

| Stage | What runs | Why this model, why here |
| --- | --- | --- |
| **Ingest** | `openbmb/MiniCPM-V-4_5` (vision) on Nebius Token Factory; optional verifier `nvidia/Nemotron-3.5-Super-VL` | Reads messy, mixed-modality attachments — pay stubs, insurance cards, prescriptions, denial letters — into the program's field schema. Vision handles what regex cannot. With the verifier on, a second model reads the same pages and any field the two disagree on goes to the reviewer ([benchmark](benchmarks/vlm-extract-nemotron-vl-2026-10-18.md)). |
| **Triage** | Nemotron Nano on Nebius Token Factory (rule-based fallback) | Classifies the case (new intake, refill, appeal, missing-info reply, not decidable) and routes it. |
| **Decide: retrieve** | `Qwen/Qwen3-Embedding-8B` (dense vectors; rerank off) | Surfaces the eligibility rules that apply to *this* case, from a vector store populated at startup from `src/pap_pipeline/rules/*.yaml`. The rules are human-authored and human-versioned; the model's job is to find, not to invent. |
| **Decide: verdict** | Nemotron Super (non-reasoning mode) on Nebius Token Factory | Two jobs. When an author saves a plain-language rule, it translates the rule into a structured formula the author reviews (`src/pap_pipeline/rule_translator.py`). At run time code evaluates those formulas, and Nemotron Super returns a structured-JSON verdict only over retrieved rules with no formula. When every retrieved rule has one, as in the CardioVita demo, the stage is skipped and the audit trail says so. Non-reasoning mode is deliberate — the reasoning variant burns output tokens on trace and returns empty content on this task shape. |

Every model call is instrumented (`src/pap_pipeline/routing.py`) with model name, tokens in/out, wall-clock latency, and per-call cost. The routing-table admin page at `/ui/admin/routing` renders the resulting matrix — real numbers per call site, per stage, defensible as evidence rather than assertion.

Operational health lands on the same principle: `GET /metrics` returns a standard metrics exposition (`src/docgen/metrics.py`) with per-tenant render counters (`docgen_renders_total{tenant_id,format,state}`), a render-latency histogram, queue-depth gauges sampled on scrape, and per-reason preflight-rejection counters. Cardinality is capped at the label set on purpose — tenant + format + outcome, no per-template or per-user labels that would blow the budget. An ops on-call gets "who is this load coming from, what's the P99, is anything stuck" from one scrape.

## Reviewer-facing pipeline evidence

Every decision the pipeline produces is auditable end-to-end from the admin UI.
Template-flow events keep the existing `/ui/inbox/{id}` page unchanged; PAP
events additionally get a dedicated `/ui/inbox/{id}/pipeline` view that
exposes the artifacts a template-flow page has no slot for:

| Section on the pipeline detail page | What it renders | Source |
| --- | --- | --- |
| **Outcome + rationale** | `approve` / `deny` / `missing_info` badge, one-sentence reason | `Decision.outcome`, `Decision.rationale` |
| **Extract — structured intake** | Every slot the program's `extraction_schema` defined, rendered dynamically (blanks shown as `—`). Custom program slots appear alongside canonical Intake fields. | `inbox_events.pap_extraction` (raw slot dict), falling back to `pap_intake` for pre-migration events |
| **Retrieve → Reason — citations** | Table of every rule the reason stage evaluated: rule ID, matched yes/no, reranker score, and the rule statement + kind chip | `Decision.citations` joined against `pap_pipeline/rules/*.yaml` at render time |
| **Missing information** | Field-name list; empty on approve/deny | `Decision.missing_fields` |
| **Assemble → Reply** | Outbound Message-ID and a link to the exact merged PDF package the applicant received, plus the item-by-item list of what went in (letter template version + each static PDF version, in package order) | `inbox_events.reply_message_id`, `inbox_events.reply_attachment_blob_key`, `doc_package_assemblies.resolved_items` |

Persistence primitives added for this view:

- `inbox_events.pap_intake` / `pap_decision` (JSONB, nullable) — Alembic
  migration `0018_inbox_events_pap_artifacts.py`. Written by
  `pap_pipeline.inbox_hook._handler` after `_run_pipeline` succeeds.
- `inbox_events.reply_attachment_blob_key` (String, nullable) — Alembic
  migration `0019_inbox_events_reply_attachment.py`. The handler uploads the
  rendered PDF bytes to the `s3_bucket_documents` S3-compatible bucket before
  handing the same bytes to the mailer, so the reviewer downloads the
  byte-identical artifact that left the building.
- `programs` table — Alembic migration `0023_programs.py`. One row per
  tenant × PAP program (e.g. `cardiovita-pap-2026`), carrying a JSONB
  `extraction_schema` (list of slot dicts) and an optional binding to a
  frozen enrollment template. The extract stage builds the VLM prompt from
  this schema, so a new program is a data change, not a build. Managed
  through the admin UI at `/ui/programs` (list + editable slot table +
  seed-from-template).
- `inbox_events.pap_extraction` (JSONB, nullable) — Alembic migration
  `0024_inbox_events_pap_extraction.py`. Stores the raw slot dict as
  returned by the extract stage — the union of every slot the operator
  defined, before it's coerced onto the fixed canonical Intake shape. This
  is what the detail page renders dynamically and what the reply mapper
  merges into `_build_render_data` so program-specific slots (e.g.
  `member_id`, `rx_medication`) flow into matching template fields
  without needing an Intake schema change.
- `static_documents` + `static_document_versions` + `doc_packages` +
  `doc_package_assemblies` — Alembic migration `0029_doc_packages.py`.
  Tenant-scoped versioned PDF library, ordered composition into packages,
  and an append-only assembly log stamping the resolved template + static
  versions per case. `programs.outcomes` (JSONB array) and
  `programs.package_bindings` (JSONB `outcome → package_id`) added on the
  same migration. See [`docs/adr/0003-doc-packages.md`](adr/0003-doc-packages.md)
  for the model and the version-pinning rationale.

Why a dedicated view rather than expanding the shared page: see
[`docs/adr/0002-pipeline-inbox-view.md`](adr/0002-pipeline-inbox-view.md).

### How dynamic slots reach the reasoning stage

The orchestrator splits the raw extraction dict at a single boundary:
canonical fields flow through `Intake` (so the retrieve stage's fixed-shape
query builder still works); everything else flows through a parallel
`extras` dict that the reason stage merges onto the intake payload sent to
Nemotron. Intake wins on collision — extras are additive context, never an
override channel. A rule that references a program-specific slot therefore
has a value to evaluate against, without polluting the canonical schema
that retrieval and the rule engine key on.

## What the doc-gen kernel contributes (and why it matters here)

The deterministic-render boundary is the reason Verdikt lands on the existing kernel rather than on a fresh substrate. `docgen/core/` (render path) does not import `docgen/providers/` (AI). The boundary is enforced in tests, not in comments. Verdikt produces a structured `Decision`; the kernel renders that Decision into the reply letter through a byte-identical merge over a frozen `TemplateVersion`. No model runs at render time. No model can. That is what makes *"same input, same output, forever"* a defensible engineering claim rather than marketing.

The rest of the kernel — multi-tenancy, provider abstraction, freeze/publish lifecycle, generic inbox agent — carries the operational weight so Verdikt can focus on the pipeline itself. One extension point on `docgen.inbox.agent` (a `use_pipeline_backend` config flag) is all it takes to route intake through Verdikt without touching the substrate.

## Judging-criteria mapping

**Technical implementation.** Three NVIDIA models composed at their strongest task shapes, on one Nebius key, with per-call routing instrumentation that turns "why Nebius/NVIDIA?" into a data question. Deterministic render boundary enforced in tests. Provable one-way dependency (`docgen ← pap_pipeline`, never reverse) prevents the substrate from silently contaminating the submission-window work. Every inter-stage artifact is a structured pydantic model — the pipeline is independently testable at each seam without spinning up the whole stack.

**Design (complete product).** Verdikt is observable end-to-end. Every stage's output is a structured artifact a human reviewer can read: the intake object, the retrieved rules with rerank scores, the decision with citations, the rendered letter. The admin routing-table page surfaces the model-selection decisions as live data. A first-run path takes a stranger from empty inbox to rendered approval letter without maintainer intervention.

**Potential impact.** Patient Assistance Programs are one instance of a class — regulated document workflows where the letter that leaves the building is the audit artifact. Prior authorization, benefit verification, informed consent, MedWatch reporting all share the same shape. The pipeline structure generalizes; PAP is the demo. Onboarding a second program is a data change, not a build — proven by the OncoBridge cameo (different rules pack, opposite verdict, same code) and by the `programs` table itself: each tenant × program row carries its own extraction schema, so a new program's slot list, rules, and reply template are all data, not code.

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
- **Public demo URL.** [https://verdikt.aismith.ai](https://verdikt.aismith.ai). Live on self-hosted infrastructure, decoupled from the production doc-gen kernel deploy scheduled for mid-November.
- **YouTube video.** [https://youtu.be/EleEatbaEWM](https://youtu.be/EleEatbaEWM). Under-3-minute cut, full shooting script in [`docs/demo-scripts/video-3min.md`](demo-scripts/video-3min.md) (cold open, live demo, architecture beat, second-program cameo, close/CTA).
- **Devpost submission.** URL, repo link, video, and this document as the technical write-up.

## Companion documents

- [`docs/demo-scripts/architecture-justification.md`](demo-scripts/architecture-justification.md) — the pipeline-vs-agents argument, README-ready.
- [`docs/demo-scripts/submission-roadmap.md`](demo-scripts/submission-roadmap.md) — the 50-day plan with phase gates and the product-name decision.
- [`docs/demo-scripts/video-3min.md`](demo-scripts/video-3min.md) — full 3-minute shooting script (cold open → live demo → architecture beat → cameo → close), with per-beat visual-sync tables, delivery notes, and a latest-features coverage matrix.
- [`docs/demo-scripts/judge-qa-briefing.md`](demo-scripts/judge-qa-briefing.md) — live-Q&A material (kept out of public-facing docs).
- [`docs/deploy-nebius.md`](deploy-nebius.md) — Nebius AI Cloud deploy runbook (reference; the live deploy is self-hosted).
