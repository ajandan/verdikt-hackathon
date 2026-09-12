# Submission roadmap — 50 days to Oct 30

_Four phases from repo-start-state to Devpost submission. Each phase ends with an explicit go/no-go gate so scope stays honest as the deadline compresses._

## Product name

**Verdikt** — chosen 2026-09-09. Nordic-legal spelling of "verdict"; the name is the output. Ties directly to the slogan (*"AI-in, deterministic-out"* → *"AI-in, Verdikt-out"*), avoids the crowded trademark space around the standard spelling, and reads sharply in a 60-second video.

The Python package stays `pap_pipeline` — Verdikt is the product brand; the package name is the demo's PAP implementation of it. Standard split (PyTorch/`torch`, Kubernetes/`k8s`). Rename can happen later if we want to align them.

## Code structure and boundary

The submission stays in the `quire-hackathon` repo but the new work is walled off in a distinct top-level subpackage, with a one-way dependency onto the Quire kernel:

```
src/
  docgen/               ← Quire kernel. Prior work. Unchanged except for one hook.
    api/
    core/               (deterministic render — untouched)
    designtime/
    inbox/              (generic inbox agent, gains one extension point)
    jobs/
    providers/
    store/
    ui/                 (admin portal — hosts the routing-table page)
  pap_pipeline/         ← NEW — all submission-window work lives here
    stages/
      extract.py        (calls nv-ingest → structured intake)
      retrieve.py       (calls NeMo Retriever NIMs → relevant rules)
      reason.py         (calls Nemotron → structured decision)
    schemas/            (intake schema, decision schema, rule schema)
    rules/              (eligibility rules as YAML — one file per rule, namespaced by program)
    templates/          (PAP approval / denial / missing-info templates)
    orchestrator.py     (composes the three stages)
    routing.py          (per-call instrumentation → routing table data)
    demo_data/          (sample PAP packets for testing)
```

**Dependency direction:** `pap_pipeline/` imports from `docgen/`. `docgen/` never imports from `pap_pipeline/`. This is enforced by a boundary test alongside the existing `core/` vs. `providers/` import audit — same pattern.

**Wire-up point (Phase 2):** `docgen.inbox.agent` grows a single config flag `use_pipeline_backend`. When true, it calls `pap_pipeline.orchestrator.run(msg)` and uses the returned decision to select which template to render. When false, existing behavior — nothing regresses.

**Reuse declaration this structure produces (`HACKATHON.md`):**

> **Prior work (before 2026-08-26):** everything under `src/docgen/`. Quire is the doc-gen kernel — design-time AI mapping, deterministic render, freeze/publish, generic inbox agent, provider abstraction, multi-tenancy.
>
> **New during the submission window:** everything under `src/pap_pipeline/`. The PAP intake pipeline — three stages (extract via `nv-ingest`, retrieve via NeMo Retriever NIMs, reason via Nemotron), the intake and decision schemas, the four eligibility rules, PAP-specific templates, routing-table instrumentation, and the orchestrator. The pipeline consumes Quire as a library; Quire is unchanged except for a single extension point that lets the pipeline plug in.

Provable by `git log -- src/pap_pipeline/` returning only commits after 2026-08-26.

## Phase 1 — Risk spikes and scope lock (Week 1, ~7 days)

**Goal: know by end of week whether the ambitious composition is real, or whether the fallback path is committed. Scaffold the new subpackage so downstream phases have a place to land.**

- **Spike A — nv-ingest on Nebius.** Stand up a Nebius VM, get `nv-ingest` running, feed it 8–10 realistic PAP-shaped PDFs (pay stubs, insurance cards, prescriptions, denial letters, enrollment forms — synthesized or public samples). Score how many come back with usable structured output.
  - _Decision:_ keep `nv-ingest`, or fall back to MiniCPM-V for the extraction stage.
- **Spike B — NeMo Retriever access.** Verify whether Nebius Token Factory serves the embedding + rerank NIMs directly, or whether they need to be self-hosted on the same VM. Call each once from Python end-to-end.
- **Scaffold `src/pap_pipeline/`.** Create the subpackage skeleton per the layout above — empty `__init__.py` files, placeholder modules with docstrings that describe what each will hold. No logic yet; just the shape.
- **Add the boundary test.** A pytest import-audit that fails if any module in `src/docgen/` imports from `src/pap_pipeline/`. Mirror the existing `core/` vs. `providers/` audit script.
- **Author the demo scenario.** Pick one imaginary manufacturer, one drug, one program name. Write out four eligibility rules in plain English and land them as YAML/JSON fixtures in `src/pap_pipeline/rules/`.
- **Lock the intake schema.** Structured fields the pipeline emits after extraction (annual income, insurance status, diagnosis, prescribed drug, patient demographics, etc.). Define as pydantic models in `src/pap_pipeline/schemas/`.
- ~~**Decide the pipeline's product name.**~~ Locked 2026-09-09 as **Verdikt** — see "Product name" section above.
- **Update `HACKATHON.md`** with the actual submission story and the reuse declaration above.

**Go/no-go gate:** both spikes work AND the subpackage skeleton compiles AND the boundary test runs green. If `nv-ingest` is flaky against realistic inputs, fall back to MiniCPM-V-only and rewrite the "Multimodal PDF Extraction pattern" framing in the README honestly.

### Phase 1 blocker — replace pymupdf to unblock MIT licensing (~~pending~~ Done 2026-09-09)

**Status:** Executed. `pyproject.toml` now declares `license = "MIT"`, pymupdf is removed from dependencies, and `scripts/license_check.py` forbids AGPL again. See the "Superseded outcome" section of `docs/adr/0001-adopt-agpl-for-pymupdf.md` for the exact stack that replaced pymupdf and the behaviour deltas the migration accepted.

Original rationale (kept for context): hackathon rules (verified 2026-09-09) require an open-source license with permissive examples (Apache 2.0 / MIT / MPL 2.0). While pymupdf (AGPL) was a runtime dep, MIT-licensing this codebase would have been a legal contradiction. Executed ADR-0001 Option 2 — swap to `pypdfium2` (Apache-2.0) for rasterization + text-with-bboxes and `pdfplumber` (MIT) for vector drawings, with `reportlab` + `pypdf` already covering overlay draw.

## Phase 2 — Build the three pipeline stages (Weeks 2–3, ~14 days)

**Goal: end of week 3, an email with a realistic packet produces a decision + rendered reply, end-to-end — via the new subpackage, wired into the existing inbox agent by a single config flag.**

- **Stage 1 (intake)** — `src/pap_pipeline/stages/extract.py`. Wire the chosen extraction backend (nv-ingest or MiniCPM-V fallback). Attachments in, structured intake object out (validated against the schemas from Phase 1). Log timings per call.
- **Stage 2 (retrieval)** — `src/pap_pipeline/stages/retrieve.py`. Populate a pgvector table with the four eligibility rules as retrievable chunks. Wire NeMo Retriever embed + rerank NIMs. Given an intake object, retrieve top-k relevant rules with scores.
- **Stage 3 (reasoning)** — `src/pap_pipeline/stages/reason.py`. Nemotron-Super call: given intake + retrieved rules, emit a structured decision (`approve` | `deny` | `missing_info`) with citations to which rules fired and — for `missing_info` — which fields are absent or illegible.
- **Orchestrator** — `src/pap_pipeline/orchestrator.py`. Composes the three stages, catches errors between them, returns a canonical `Decision` object. This is the single entry point the inbox agent will call.
- **Four PAP templates** — `src/pap_pipeline/templates/` — enrollment form (the thing patients / providers send in), approval letter, denial letter, missing-info letter. Run through the existing Quire design-time flow (upload → freeze → publish) so they're first-class `TemplateVersion` records.
- **Wire-up point in `docgen.inbox.agent`.** Add the `use_pipeline_backend` config flag. When true, after intake resolution, call `pap_pipeline.orchestrator.run(msg)` and route the resulting decision to the right template render. When false, existing behavior. Feature-flag on by default in the demo tenant; off for all other tenants (nothing regresses).
- **Instrument every model call** — `src/pap_pipeline/routing.py`. Model name, tokens in/out, wall-clock latency, per-call cost. This is the raw data source for the routing-table page.

**Go/no-go gate:** end-to-end loop working with one demo document set — email arrives, pipeline runs, correct template renders, reply sends. Boundary test still green. If not, scope shrinks — probably drop retrieval and hardcode the rules directly in the reasoning prompt.

## Phase 3 — Compose, polish, and productize the demo (Weeks 4–5, ~14 days)

**Goal: the thing looks like a product with customers, not a proof of concept.**

- **Routing-table page** at `/ui/admin/routing` — the view lives in `docgen/ui/` (one admin portal, one auth path) but reads its data from `pap_pipeline/routing.py`. Real numbers per call site, one row per model per stage.
- **Review UI polish.** This is where 25% of Design points live for most entries. Empty states, loading states, error states, a real domain name, pipeline stages visible with their artifacts (intake object, retrieved rules with scores, decision + citations).
- **README rewrite** around the PAP + pipeline story, using `architecture-justification.md` as the load-bearing section. Repo layout section documents the `docgen/` vs. `pap_pipeline/` split explicitly.
- **HACKATHON.md rewrite** — the judging-criteria mapping table becomes concrete instead of aspirational. Reuse declaration section already drafted in Phase 1 is finalized here.
- **Deploy resolution** _(see conflict note below)_ — get the hackathon-demo URL live.
- **Test the golden path and 3–4 edge cases** in a browser end-to-end. Verify determinism: same email in twice, byte-identical letter out both times.
- **Author the second-program rules pack for the cameo.** Four YAML rules for a contrasting program (working name: `oncobridge-pap-2026`) with a stricter income cap so the same demo patient flips `approve → deny` on the income rule alone. Lands in `src/pap_pipeline/rules/` alongside the primary program's rules; the existing `program` field on `Rule` namespaces retrieval — no code changes. Templates for this program are stubs only; the cameo does not render the reply, it stops at the Decision.

**Go/no-go gate:** a stranger can be handed the demo URL and get from "empty inbox" to "rendered approval letter" without the maintainer touching the machine. Boundary test still green.

## Phase 4 — Video + submission (Weeks 6–7, ~13 days including buffer)

**Goal: submit by Oct 30 with margin to spare.**

- **Record the video** — plan for 3–5 takes over 2–3 days. Use the 60-second architecture-beat narration in `video-narration-60s.md`; write the intake + outbound beats to match its register. Includes the second-program cameo beat (~15 sec, immediately after the architecture beat) — script in the same file.
- **Edit to under 3 min.** YouTube upload as unlisted first; flip public when ready.
- **Flip repo to public** — currently private per project memory; must be public by Oct 30.
- **Devpost submission** with all required fields.
- **Buffer** — at least 5 days between "everything done" and the Oct 30 deadline. Something will break.

## Explicitly NOT in scope for this window

- Full Nebius Serverless Jobs integration (leave the subprocess proxy in place; name Jobs as the deploy-time flip in the video)
- Fine-tuning a Nemotron LoRA per tenant (name it as the roadmap slide at 2:30 of the video)
- Deploying the full Enterprise RAG blueprint (call NeMo Retriever NIMs directly; blueprint is pattern-inspiration only)
- Publishing ABS (Agent Blueprint Spec) or referencing it in submission materials
- Multi-language support (English only for demo)
- More than two demo programs — the primary (Cardiovita) is end-to-end; the second (OncoBridge) exists only as a rules-pack cameo to show the platform-vs-bespoke framing (same pipeline, different config, different verdict). See Phase 3.
- Fully automating human-in-the-loop review UI beyond what already exists
- Promoting `pap_pipeline/` to its own repo — that's future work; for this submission, monorepo with subpackage is the right shape

## Open conflict to resolve in Phase 1

Project memory records that the Nebius VM deploy was deferred to mid-November — but the hackathon rules require a public demo URL by Oct 30. Two paths:

1. **Move the production deploy up.** Execute the pre-flighted runbook in Phase 3 (mid-October). Runbook is ready per memory, so this is execution not build.
2. **Ship a minimal hackathon-demo deploy** on Nebius that is not the production-grade version. Same runbook, weaker SLA, tear down after submission if desired.

Recommended: option 2 — decouple "demo URL that satisfies hackathon" from "production-grade deploy for post-hackathon work." Prevents mid-Nov slippage from cascading into hackathon compliance risk.

## Companion documents in this folder

- `architecture-justification.md` — the pipeline-vs-agents piece; drives the README architecture section and the video's core beat
- `judge-qa-briefing.md` — sharper live-Q&A material kept out of public-facing docs
- `video-narration-60s.md` — the 60-second spoken script for the architecture beat, with delivery + visual sync notes
