# Design Document — Template-Based Document Generation Service

> **Status:** Living document. Stack decided (§4); detailed design in progress.
> **Working title:** *DocGen* (placeholder — see Open Decision D-0)
> **Lineage:** Evolution of DAPI GEN (Pega global hackathon winner, 2020), rebuilt standalone with no platform dependency.
> **Last updated:** 2026-09-01 (PDF-as-template kind — §4, §6, §8, §9, §14, §18)

---

## 1. Purpose

Captures the architecture *as it is being designed*. Records irreversible decisions, deliberate deferrals, and open questions. Every section is expected to change; the decision log at the bottom is append-only.

Organizing principle: **classify every decision as Day 1 (irreversible — get it right now) or Day 2 (reversible — defer without guilt).** 80% of design effort goes to Day 1.

---

## 2. Problem & thesis

Enterprises regulated enough that SaaS document generation is off the table — healthcare payers, banks, insurers — still generate contractual correspondence (denial letters, policy documents, statements, appeals notices) through tools that are app-specific, department-specific, or a heavyweight CCM platform they can't fully afford or staff.

Two structural gaps sit between the extremes:

1. **AI-native doc-gen startups** (upload → AI map → REST/MCP) are cloud-only — ruled out for data-residency-constrained estates.
2. **CCM incumbents** (Quadient, OpenText, Smart Communications) have audit-grade governance and any-prem deployment but no clean AI-native upload-and-map front door, and are heavy to buy and run.

**Thesis:** a seam exists between them — self-hosted, AI-native at design time, deterministic and provably data-blind at runtime, agent-consumable — that neither side occupies today.

**The new argument (didn't exist in 2020):** agents don't respect departmental boundaries. One agent on one case may need a clinical letter, a billing statement, and an appeals notice — three template silos today. A single registry with a uniform contract is the only thing that serves it.

---

## 3. Design principles (non-negotiable)

- **Cut scope, never cut craft.** A malformed regulated document is a compliance incident, not a bug report. Ship one thing flawlessly over eight that mostly work.
- **Deterministic at runtime.** No LLM ever sits between merge fields and rendered output. AI is design-time only. This is a security claim, not just architecture.
- **AI is a suggestion engine, not a step.** It proposes; a human confirms once; the result is frozen as a hashed, versioned artifact and never re-inferred at render time.
- **Single artifact, config not code.** One container image across all customers. The first per-customer fork ends the margin.
- **Governance is the moat, not generation.** Naive merge is commodity. Versioning, provenance, and audit are defensible.

---

## 4. Technology stack — DECIDED (D-1)

Chosen for **reliability and practicality for a solo maintainer**, not theoretical best-of-breed per layer. The deciding logic: (a) minimize the amount of hard OOXML edge-case code owned in-house, (b) one language across all three layers, (c) first-class AI/MCP ecosystem.

| Concern | Choice | Why |
|---------|--------|-----|
| **Language** | Python 3.12+ | One language across render core, AI mapper, and adapters. Best AI/MCP ecosystem. Largest talent pool if it grows. |
| **DOCX templating/merge** | **docxtpl** (Jinja-in-Word) | Purpose-built for *exactly* this: `{% for %}`, `{% if %}`, rich text, image insertion, nested tables — processed inside the Word doc's own structure. The Jinja tags *are* the mapping artifact. Minimizes owned edge-case code (the #1 reliability risk). |
| **Schema / validation** | **Pydantic v2** | One schema definition drives request validation, OpenAPI, and the MCP tool contract (§7). |
| **REST API** | **FastAPI + Uvicorn/Gunicorn** | Async-native, auto OpenAPI, Pydantic-native. |
| **MCP surface** | **Official Python MCP SDK (FastMCP)** | Each template → an MCP tool with input schema derived from the frozen template schema. |
| **Async jobs** | **Dramatiq + Redis** (fallback: Celery) | Rendering is CPU-bound and unbounded. Dramatiq is simpler and more robust to operate solo than Celery; Celery is the conservative fallback if broader ecosystem is needed. |
| **Relational store** | **PostgreSQL** | Provenance, versioning, metadata. Hybrid: fixed columns + `jsonb`. |
| **Blob store** | **S3-compatible — MinIO (on-prem) / S3 (cloud)** | Template binaries + rendered documents. Metadata + hash live in Postgres. |
| **PDF as template kind** | **pypdf** (AcroForm read + merge), **pypdfium2** (page raster for the editor + preflight), **pdfplumber** (vector geometry + text-with-bboxes for field detection), **reportlab** (value overlay) | PDF is a *first-class template kind*, not a DOCX conversion target. Two flavors — `acroform` (fill widgets) and `flat` (draw at stored coords) — render through one overlay pipeline. All permissive licenses (Apache-2.0 / MIT / BSD-3) — see [ADR-0001](adr/0001-adopt-agpl-for-pymupdf.md) for the pymupdf → pypdfium2 + pdfplumber swap. See §6.  |
| **AI provider** | **Pluggable adapter** — Azure OpenAI / AWS Bedrock / local (Ollama/vLLM, OpenAI-compatible) | Config-driven. Never a hardcoded key phoning home. Design-time only. |
| **Packaging** | Single Docker image; docker-compose (simple on-prem) + Helm chart (k8s estates) | Config not code. |

### Why not the alternatives
- **.NET + Open XML SDK** — best free fidelity + single-file deploy, but you *build the templating grammar yourself*, which is exactly the deep, edge-case-ridden code a solo vendor should avoid owning. Fidelity gain doesn't outweigh the maintenance risk.
- **Java + docx4j** — deepest comfort zone, but docx4j's complex-markup handling is the category's known bug zone, and no single-binary deploy.
- **Split-language (.NET core + Python mapper)** — theoretically optimal per layer; rejected because two runtimes + two deploy artifacts is the opposite of practical for one maintainer.
- **Aspose.Words** — best fidelity that exists, but redistribution licensing kills margin and hands your core dependency to a vendor, inside the customer's firewall.
- **Go / Rust** — single-binary appeal, but immature OOXML ecosystem = yak-shaving.

> **Practical caveat carried forward:** docxtpl inherits some python-docx limits on the most exotic conditional-table splitting. The **preflight validator (§8)** is what turns this from a runtime risk into a design-time gate.

---

## 5. System architecture

Three layers. The **render core is the only bone**; everything else is muscle.

```
┌────────────────────────────────────────────────────────────────────┐
│  DESIGN TIME  — AI touches only blank templates, never runtime data  │
│                                                                      │
│  Upload .docx / .pdf ─▶ Preflight validate ─▶                        │
│      docx: AI proposes Jinja tags + typed schema                     │
│      pdf : preflight harvests widgets (acroform) or empty (flat)     │
│  ─▶ Human confirms in preview / editor ─▶ FREEZE:                    │
│      tagged .docx OR source .pdf + placements (blob)                 │
│      + schema (Pydantic/JSON) + version hash                          │
│  ── AI provider adapter (Azure OpenAI / Bedrock / local) ──           │
└────────────────────────────────────────────────────────────────────┘
                         │  frozen artifact + typed schema
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  RENDER CORE  — deterministic, zero intelligence                     │
│  validate payload against frozen schema ─▶ dispatch by kind:         │
│      docx: docxtpl merge ──────────────────────────▶ DOCX             │
│      pdf : reportlab overlay + pypdf merge ────────▶ PDF              │
└────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  ADAPTER & GOVERNANCE LAYER                                          │
│  FastAPI (REST) ┐                                                    │
│  FastMCP (MCP)  ┴─ thin, over one core   •   Dramatiq async jobs     │
│  Postgres: provenance (version hash per render, append-only log)     │
│  MinIO/S3: template + document blobs                                 │
└────────────────────────────────────────────────────────────────────┘
```

Components (all one Python codebase, deployed as one image):
- **`core/`** — dispatch + engines: docxtpl merge (`render_pipeline.py`) and PDF overlay+merge (`render_pipeline_pdf.py`), plus DOCX preflight validator. No web, no AI. Pure functions over (template, data) → document. Dispatch keys off `TemplateVersion.pdf_flavor` — nullable for docx, set for pdf.
- **`designtime/`** — upload handling (`upload.py` for docx, `upload_pdf_template` for pdf), AI mapping adapter, PDF preflight (`preflight_pdf.py`), freeze logic. Calls `core` preflights.
- **`api/`** — FastAPI routes (REST) + FastMCP tools (MCP). Thin; both call the same handlers.
- **`jobs/`** — Dramatiq actors for async render.
- **`store/`** — Postgres (SQLAlchemy/SQLModel) + S3 client.
- **`providers/`** — AI provider adapter interface + implementations.

---

## 6. The render pipeline (step by step)

Two template kinds share the same design-time gate and provenance
model but diverge at the merge step. Which pipeline runs is decided
at upload time and pinned on `Template.kind` for the template's
lifetime (a docx template stays docx; a pdf template stays pdf).

### 6a. DOCX pipeline

**Design time (once per template):**
1. Business user uploads a `.docx`.
2. **Preflight** parses it, checks renderability (§8a). Reject/warn loudly here — never at render time.
3. **AI mapper** reads the *blank* template, proposes where Jinja tags go and a typed field schema (scalars, lists→loops, conditionals). No customer data involved.
4. Human reviews in a preview, edits, confirms.
5. **Freeze:** store the tagged `.docx` in blob storage, the schema in Postgres, compute a `version_hash` over (tagged template bytes + schema). This tuple is immutable. A change = a new version, never an edit.

**Runtime (per document, deterministic):**
6. Caller invokes render with `template_id`, optional `version`, and a data payload.
7. Payload validated against the frozen Pydantic schema. Invalid → rejected, no partial render.
8. **docxtpl merge** → DOCX.
9. Result stored in blob storage; a provenance row is written stamping the exact `version_hash`, caller identity, timestamp.
10. Sync response for small jobs; async job ID for large ones (§10).

### 6b. PDF pipeline (kind='pdf')

**Design time (once per template):**
1. Business user uploads a `.pdf`.
2. **PDF preflight** (§8b) classifies the flavor:
   - `acroform` — source has fillable widgets → harvest each widget's
     page + rect into an initial `pdf_field_placements` list (as
     page-relative percentages so the placements survive minor page
     re-sizes). Renderable immediately.
   - `flat` — no widgets → placements start empty. The user must open
     the placement editor before the template will render.
3. **No AI mapper step for PDFs.** The placement's `name` field IS
   the payload key — there are no Jinja tags to align, and the schema
   derives 1:1 from placements. (PR3 adds AI *re-alignment* on
   re-upload, which is a different problem.)
4. Human reviews / edits placements in the editor (canvas over page
   preview; click to add, drag to move).
5. **Freeze:** store the source `.pdf` unchanged in blob storage,
   the placement list + derived schema in Postgres, compute a
   `version_hash` over (source PDF bytes + canonical placements + schema).
   No text injection into the PDF — placements are metadata.

**Runtime (per document, deterministic):**
6. Caller invokes render with `template_id`, optional `version`, and a data payload.
7. Payload validated against the frozen Pydantic schema (schema
   fields are the placement names). Invalid → rejected.
8. **reportlab draws each placement's value** at its stored coords
   onto a transparent overlay; **pypdf merges** overlay + source page;
   `/AcroForm` is stripped from the output so the result is a truly
   flat PDF (no residual interactive widgets).
9. Provenance row identical to DOCX.
10. Same sync/async contract as DOCX.

---

## 7. Unified schema model (the DAPI GEN idea, done right)

A single Pydantic model per template version is the **one source of truth** that drives three surfaces:

```
Frozen template schema (Pydantic v2)
        │
        ├─▶ Runtime payload validation  (reject bad data before merge)
        ├─▶ REST OpenAPI spec            (auto-generated request contract)
        └─▶ MCP tool input schema        (agent-consumable contract)
```

This is the 2020 "auto-generated request JSON" made rigorous: define the schema once at freeze time, and the REST contract, the MCP tool signature, and the validation gate all derive from it. No drift between surfaces by construction.

**PDF templates** collapse the schema-derivation step: the placement
`name` field IS the payload key, and the schema is derived
mechanically from the placement list at freeze time. Same three
downstream surfaces, same guarantee — one source of truth per
version, no drift by construction.

---

## 8. Preflight validation (the core differentiator)

*The most defensible original piece; the least glamorous.*

Every competitor fails at **render** time on constructs the engine can't faithfully merge — in this domain, a compliance incident. This design moves the failure to **upload** time.

Same three-valued gate for both template kinds: **accept**,
**accept-with-warnings**, **reject** — each with actionable messages.
Failing here is safe and cheap; failing at render is the incident
being insured against.

### 8a. DOCX preflight

The validator inspects the uploaded `.docx` and rejects/warns on:
- Jinja tag structure that won't parse or references undefined fields.
- Constructs docxtpl can't guarantee (certain nested conditional table splits, malformed merge regions).
- Tracked-change residue, broken field codes, corrupt OPC parts.

### 8b. PDF preflight

The validator inspects the uploaded `.pdf` and rejects/warns on:
- Encrypted or password-protected PDFs (`reject` — can't merge).
- Corrupt / unopenable PDFs (`reject`).
- Missing font subsets that would prevent overlay merge (`warn`).
- Duplicate widget names in AcroForm PDFs (`warn` — placement names
  must be unique in the derived schema; duplicates get suffixed).
- Very large page counts (`warn` above threshold; renders will be slow).

The preflight report also carries the classified `flavor` and the
initial `pdf_field_placements` list (harvested widget rects for
acroform, empty for flat). Both are persisted on the version row.

---

## 9. Data model & storage

**PostgreSQL (queryable metadata, versioning, provenance):**

| Table | Key columns | Notes |
|-------|-------------|-------|
| `templates` | `id`, `name`, `owner`, `kind` (`'docx'`\|`'pdf'`), `current_version`, `created_at` | Logical template. `kind` is fixed at first upload — a template is one artifact family for its lifetime. |
| `template_versions` | `id`, `template_id`, `version`, `schema` (jsonb), `blob_key`, `version_hash`, `status`, `pdf_flavor` (`'acroform'`\|`'flat'`\|null), `pdf_field_placements` (jsonb), `created_by`, `created_at` | Immutable. New version, never edit. `pdf_flavor` / `pdf_field_placements` populated only when parent is `kind='pdf'`; both null on docx rows. `pdf_flavor` doubles as the render-pipeline dispatch discriminator (avoids a JOIN on hot path). |
| `render_jobs` | `id`, `template_version_id`, `state`, `caller`, `submitted_at`, `completed_at`, `result_blob_key`, `error` | Async job lifecycle. |
| `provenance` | `id`, `render_job_id`, `template_version_id`, `version_hash`, `caller`, `rendered_at` | **Append-only.** Answers "which template version, invoked by whom, produced this document." |
| `tenants` | `id`, `name`, `key_ref`, `ai_provider_config` (jsonb) | Per-tenant isolation + provider config. |
| `static_documents` | `id`, `tenant_id`, `name`, `current_version`, `created_at` | Tenant-scoped store for stable PDFs (guides, appeals notices, blank forms). Bytes-in, bytes-out — not rendered. See ADR-0003. |
| `static_document_versions` | `id`, `static_document_id`, `version`, `blob_key`, `content_hash`, `page_count`, `uploaded_by`, `uploaded_at` | Immutable per version. New upload = new row. `content_hash` is SHA-256 over source bytes. |
| `doc_packages` | `id`, `tenant_id`, `name`, `items` (jsonb), `created_at`, `updated_at` | Tenant-scoped ordered composition. `items` = ordered list of `{kind: "template"\|"static", ref_id: uuid}` — no version pin; assembler picks latest at runtime. |
| `doc_package_assemblies` | `id`, `case_id`, `doc_package_id`, `resolved_items` (jsonb), `merged_blob_key`, `outcome`, `assembled_at` | **Append-only.** One row per assembler run. `resolved_items` stamps the exact template_version_id / static_document_version_id for each package item at assembly time. |

**Programs (Verdikt-level, defined for reference):** `programs.outcomes` (jsonb array of declared outcome slugs) and `programs.package_bindings` (jsonb map `outcome → doc_package_id`) — the two columns that carry the outcome vocabulary and the per-outcome package selection. Enforced at publish: every entry in `outcomes` must have a `package_bindings` entry, and every rule's emitted outcome must appear in `outcomes`. See ADR-0003 for the full model.

Hybrid storage per principle: fixed columns for universal query fields, `jsonb` for flexible per-template schema/config, blobs externalized.

**MinIO / S3 (blobs):** tagged template `.docx` files and rendered outputs, keyed by `blob_key`. Nothing renderable lives in Postgres.

---

## 10. Async job model (Day 1 contract)

Rendering is CPU-bound and unbounded — sync works until someone posts a 400-page merge. The contract is fixed from line one because sync→async is a breaking change.

- **Submit:** `POST /render` → if small, render inline; if large (or `async=true`), enqueue Dramatiq actor, return `202` + `job_id`.
- **States:** `queued → running → succeeded | failed`.
- **Retrieve:** `GET /jobs/{job_id}` (poll) or optional webhook callback on completion.
- **Result:** stored in blob storage; `GET /jobs/{job_id}/result` streams it.
- Redis backs the queue and short-lived job status; Postgres holds durable job + provenance records.

---

## 11. API & MCP contracts (thin over one core)

**REST (FastAPI):**
```
POST   /templates                 upload .docx → preflight → returns preflight report
POST   /templates/{id}/map        run AI mapping → returns proposed tags + schema
POST   /templates/{id}/freeze     confirm → freeze version, return version_hash
GET    /templates/{id}/schema     frozen schema (drives OpenAPI)
POST   /render                    render (sync small / 202 async large)
GET    /jobs/{job_id}             job status
GET    /jobs/{job_id}/result      rendered document
GET    /templates/{id}/provenance audit trail
```

**MCP (FastMCP):** each frozen template is exposed as a tool `render_<template_name>` whose input schema is the frozen Pydantic schema. An agent discovers available templates and calls them directly — no developer, no integration project. Both surfaces call identical handlers; MCP never "does more" than REST.

---

## 12. Security & data-residency architecture (Day 1)

- **Deterministic boundary as a security claim.** The AI service is a *separate module* that only ever sees blank templates at design time. The render path has no model in it. This is enforced architecturally, not by policy — "the model provably never sees runtime PHI" is a sentence that survives a HIPAA review.
- **Provider-pluggable, config-driven.** Customer supplies Azure OpenAI in their tenant, Bedrock in their VPC, or a local model. No egress. Verifiable by their security team via network monitoring.
- **Per-tenant isolation.** Tenant-scoped keys and provider config; blob and DB access scoped per tenant.
- **Dependency hygiene.** License scanning in CI from day one (agentic tooling pulls packages fast; one GPL dep in a licensed product is a contract problem).
- **Self-hosted single artifact.** Nothing leaves the customer's network.

---

## 13. Deployment topology

- **One Docker image** containing the full app; workers and web run from the same image with different entrypoints.
- **Dependencies:** PostgreSQL, Redis, S3-compatible store (MinIO on-prem), and — for Day 2 PDF — LibreOffice in the image.
- **docker-compose** for simple on-prem installs; **Helm chart** for k8s estates. Same image, different orchestration.
- **Config not code:** all per-customer settings via env/config (AI provider, keys, limits). No forks.

---

## 14. Day 1 / Day 2 ledger

**Day 1 — irreversible (decided unless noted):**

| ID | Decision | State |
|----|----------|-------|
| D-1 | Stack: Python + docxtpl + FastAPI + FastMCP + Dramatiq/Redis + Postgres + MinIO/S3 | **DECIDED (§4)** |
| D-2 | Templating grammar: Jinja-in-Word via docxtpl | **DECIDED** (was open; docxtpl choice resolves it) |
| D-3 | Async job contract | Accepted |
| D-4 | Deterministic render boundary | Accepted |
| D-5 | Pluggable AI provider | Accepted |
| D-6 | Provenance = append-only log + per-render version hash | Accepted |
| D-7 | Hybrid storage (Postgres fixed+jsonb, blobs in S3) | Accepted |
| D-8 | PDF as a first-class template kind (not a DOCX conversion target); unified overlay pipeline for both `acroform` and `flat` flavors | **DECIDED 2026-09-01** — see §18. Supersedes any prior "DOCX → PDF via LibreOffice" plan (reverted 2026-08-31). |

**Day 2 — deferred (seam designed, not built):** admin/template-management portal beyond minimum; multi-tenant niceties beyond key isolation; additional output formats; per-field mapping overrides; non-text PDF widget types (checkbox / radio / signature — currently coerced to text overlay).

---

## 15. Known issues in existing products → design response

| Known category failure | Response | Ref |
|------------------------|----------|-----|
| Complex-markup breakage | docxtpl (works inside Word structure) + preflight gate | §4, §8 |
| Sync API times out on large merges | Async from line one | §10 |
| Nondeterministic output (AI re-infers at render) | Mapping frozen; zero LLM at runtime | §6, §12 |
| Template sprawl, no audit trail | Version hash + append-only provenance | §9 |
| Data-egress fear | Provider-pluggable, design-time-only AI, self-hosted | §12 |
| Per-customer drift & fork rot | One image, config not code | §13 |

---

## 16. Competitive gap (confirm by demo, not research)

Every individual bone has a vendor; the **combination** was not found assembled anywhere.
- Upload→AI-map→REST/MCP: DocsAutomator, CraftMyPDF, Documentero, Foxit — all SaaS.
- BYO-model self-hosted: Docsie, elDoc — but for document *intelligence* (extraction/Q&A/RAG), not deterministic generation.
- Audit-grade versioning + any-prem: Quadient, OpenText, Smart Communications — no AI-native upload-and-map front door.
- **Preflight-as-hard-gate + deterministic-boundary-as-security-claim:** not found as a product feature. Unclaimed.

Confirm the seam with a 30-min demo call posing as a regulated buyer (N-2), not more searching.

---

## 17. Open decisions & next steps

| ID | Item | Type | Notes |
|----|------|------|-------|
| D-0 | Product name & positioning | **Open** | Standalone product, or the missing "generate" verb in DocForge's existing MCP/REST toolset? If the latter, match DocForge's stack. Resolve before naming. |
| N-1 | Thinnest end-to-end slice | Next | Real `.docx` from a real regulated context → preflight → AI map → freeze → render via REST + MCP. No governance UI, no multi-tenancy yet. |
| N-2 | Validate the seam | Next | Demo call w/ DocsAutomator as a regulated buyer: self-host? bring own model? per-version audit trail? |
| N-3 | One design partner | Next | One healthcare/banking contact who says they'd pay *before* the fun parts get built. No partner = signal to stop. |
| N-4 | IP & employment clarity | **Blocker** | Written clarity on aismith.ai IP vs. employment (SI employer) before any vendor/design-partner conversation. Lawyer + actual agreement. |
| N-5 | Golden-file test corpus | Standing | Real templates + expected outputs, structure-compared in CI. "Quality is the product" needs a regression net. |

---

## 18. Decision log (append-only)

| Date | ID | Decision | Rationale |
|------|----|----------|-----------|
| 2026-08-06 | D-3 | Async contract = Day 1 | Sync→async is a breaking change |
| 2026-08-06 | D-4 | Deterministic render boundary | Security + audit foundation |
| 2026-08-06 | D-5 | Pluggable AI provider | Keeps "data never leaves" true |
| 2026-08-06 | D-6 | Provenance = append-only + per-render hash | Immutable audit from first record |
| 2026-08-06 | D-7 | Hybrid storage | Data model shape is irreversible |
| 2026-08-06 | §8 | Preflight validation = core differentiator | Move failure from render (incident) to upload (safe) |
| 2026-08-06 | D-1 | **Stack decided: Python-centric** (docxtpl/FastAPI/FastMCP/Dramatiq/Postgres/MinIO) | Reliability + practicality for a solo maintainer > best-fidelity-per-layer; docxtpl is purpose-built; one language across all layers |
| 2026-08-06 | D-2 | **Templating grammar = Jinja-in-Word (docxtpl)** | Resolved by the docxtpl choice; the Jinja tags are the mapping artifact |
| 2026-08-31 | — | **Revert DOCX → PDF conversion feature** | LibreOffice sidecar cold-start dominated latency; on-prem installs balked at the extra runtime; PDF-as-*template* (§6b) covers the "customer already has a PDF form" case that the conversion was meant to fake. |
| 2026-09-01 | D-8 | **PDF is a first-class template kind** (`Template.kind='pdf'`); two flavors (`acroform`, `flat`) render through one overlay pipeline (reportlab + pypdf). Placement `name` IS the payload key. | Two viable shapes existed: fill widgets in place (keeps interactive form) vs. extract widgets to placements and overlay-merge like flat (one pipeline, one editor, one AI-re-align story). Chose the unified overlay because the editor + re-align surface areas pay off across all subsequent PRs; the output being flat is fine — regulated PDFs are usually printed / attached, not resubmitted. |
| 2026-09-12 | D-9 | **Outbound artifact = ordered doc package**, not a single reply letter. New entities: `static_documents` (tenant-scoped versioned PDFs), `doc_packages` (ordered composition of template + static items), `programs.outcomes[]` + `programs.package_bindings{}`. Assembler stage runs after signed verdict: resolves binding → renders templates → concatenates static PDFs → attaches to outbound + exposes `package_url` in JSON verdict. Items auto-follow latest published version (no pin). See ADR-0003. | Real replies bundle a generated letter with static appeals notices, guides, and blank forms. Single-template model couldn't express the composition or the per-outcome fan-out (approved vs denied vs pended each carry different static content). Rejected per-outcome template variants because that would embed static content into template blobs, breaking the "static is static" audit story. |
