# Verdikt

**AI-in, deterministic-out.** A decision pipeline for regulated document workflows — starting with US patient-assistance-program eligibility.

|  |  |
|---|---|
| **License** | MIT |
| **Track** | Best Apps & Agents |
| **Video** | [https://youtu.be/EleEatbaEWM](https://youtu.be/EleEatbaEWM) (under 3 minutes) |
| **Live demo** | [https://verdikt.aismith.ai](https://verdikt.aismith.ai) |
| **NVIDIA models on Nebius Token Factory** | `nemotron-3-super-120b-a12b` (Nemotron Super, verdict — non-reasoning mode for schema-conformant JSON) · Nemotron Nano (triage, with a rule-based fallback) |
| **Also on Nebius Token Factory** | `openbmb/MiniCPM-V-4_5` (Apache-2.0 vision model, extract stage) · `Qwen/Qwen3-Embedding-8B` (Apache-2.0 embedding model, retrieve stage) |
| **Domain** | US patient-assistance-program (PAP) eligibility |

Verdikt reads inbound documents, retrieves the human-authored rules that apply,
and issues a structured decision — `approve`, `deny`, or `missing_info` — with
citations back to the rules that fired. The reply letter that leaves the
building is a byte-identical merge of a frozen, human-approved template with
that decision. **No language model sits on the code path that produces the
audited artifact.**

> **Nebius × NVIDIA Global AI Hackathon submission.** Three models composed
> across three pipeline stages, all served via one Nebius Token Factory
> endpoint / one API key:
> `openbmb/MiniCPM-V-4_5` (Apache-2.0) for multimodal extraction,
> `Qwen/Qwen3-Embedding-8B` (Apache-2.0) for dense-vector rule retrieval,
> and NVIDIA `nemotron-3-super-120b-a12b` (Nemotron Super) in non-reasoning
> mode for structured-JSON verdict emission. Each stage's model is
> tenant-configurable — swap Nemotron Super for Nemotron Ultra 550B or
> Nemotron Nano 30B per program with no code change. See
> [`docs/HACKATHON.md`](docs/HACKATHON.md) for the full submission write-up,
> the reuse declaration, and the deploy runbook.

- **AI where it earns its place**: extraction over messy multimodal PDFs, retrieval over versioned rules, reasoning to a structured verdict.
- **Deterministic where it must be**: pure template-engine merge for `.docx`, canvas-overlay merge for `.pdf`. No model in the loop at render time.
- **Programs as data**: a new PAP program is a `programs` row (extraction schema + rules pack + reply template), not a build. The OncoBridge cameo demonstrates the same code, opposite verdict.
- **Auditable end-to-end**: every stage's output is a structured artifact — intake object, retrieved rules with rerank scores, decision with citations, rendered letter. A reviewer can read the whole path from the admin UI.
- **Multi-tenant**: row-level `tenant_id` scoping, API-key auth per tenant, CI-enforced [tenant-scope audit](scripts/tenant_scope_audit.py).

## Pipeline shape

```
  Inbox  (fax · mail · email · REST · MCP)
    │
    ▼
  ┌───────────────────── AI ZONE ─────────────────────┐
  │                                                   │
  │   1. Extract    MiniCPM-V-4_5 (Apache-2.0)        │
  │        │                                          │
  │        ▼                                          │
  │   2. Retrieve   Qwen3-Embedding-8B (dense)        │
  │        │                                          │
  │        ▼                                          │
  │   3. Reason     Nemotron Super (JSON verdict)     │
  │                                                   │
  └────────────────────── STOP ───────────────────────┘
    │
    ▼
  ┌──────────────── DETERMINISTIC ZONE ───────────────┐
  │                                                   │
  │      Verdict JSON  ⊕  Frozen Template             │
  │               │                                   │
  │               ▼                                   │
  │        Reply Letter   (byte-identical merge)      │
  │        Doc Package    (sha256 audit hash)         │
  │                                                   │
  │        NO MODEL RUNS AT RENDER TIME.              │
  └───────────────────────────────────────────────────┘
```

## Architecture at a glance

Verdikt is the submission-window pipeline (`src/pap_pipeline/`) composed onto a
pre-existing deterministic-render doc-gen kernel (`src/docgen/`). The split is
enforced mechanically:

- `tests/test_boundaries.py::test_docgen_never_imports_pap_pipeline` fails the build if the kernel ever reaches back into the pipeline layer.
- `tests/test_boundaries.py::test_core_never_imports_ai_layer` fails the build if the deterministic render path ever imports an AI provider.

The reuse declaration lives in [`docs/HACKATHON.md`](docs/HACKATHON.md) with a
mechanical proof (git log + boundary tests).

## Try it

No setup needed: the full pipeline is live.

- **Live demo:** [https://verdikt.aismith.ai](https://verdikt.aismith.ai). On the sign-in page, click **Sign in as demo user** for a judge account scoped to the fictional Alderwood pharmacy.
- **Demo video (< 3 min):** [https://youtu.be/EleEatbaEWM](https://youtu.be/EleEatbaEWM)

### Test it by email

The real intake path: email an enrollment packet, and Verdikt reads it, decides against the program's rules, renders the reply letter and holds it for a reviewer.

1. From any email account, send a message to `quire.template.agent@gmail.com` with the subject
   `[pap:cardiovita-pap-2026] Alderwood — enrollment for Maria Chen`.
2. Attach the two sample PDFs from [`submission/test-demo/`](submission/test-demo/):
   `ASP-100-enrollment-maria-chen.pdf` and `ASP-110-income-worksheet-maria-chen.pdf`.
3. Within a couple of minutes the case appears at [verdikt.aismith.ai](https://verdikt.aismith.ai) under **Cases**, with every stage, the rules that fired and the rendered reply.

| Test | Attach | Expected |
| --- | --- | --- |
| 1 | both PDFs | **approve**: $60,000 is under the program's income cap of $86,560 for a household of two |
| 2 | ASP-100 only | **missing info**: no income worksheet, so Verdikt asks for it instead of guessing a denial |

Nothing is sent back until a reviewer approves the case; then the decision letter is emailed to the address you sent from. The step-by-step guide, with the exact email body and troubleshooting, is [`submission/test-demo/verdikt-email-test-guide.pdf`](submission/test-demo/verdikt-email-test-guide.pdf) (source: [`docs/verdikt-email-test-guide.html`](docs/verdikt-email-test-guide.html); routing details in [`docs/demo-scripts/email-inbox-test.md`](docs/demo-scripts/email-inbox-test.md)).

## Run it yourself

Verdikt runs on Docker Compose with no Python on the host: clone the repo, copy `.env.example` to `.env`, add two secrets and a Nebius Token Factory key, and bring the stack up. Full steps, a first-run walkthrough and the development setup are in [`docs/run-locally.md`](docs/run-locally.md). The production compose file and deploy runbook are in [`deploy/`](deploy/) and [`docs/deploy-nebius.md`](docs/deploy-nebius.md).

## Repo layout

```
src/
  docgen/           deterministic-render doc-gen kernel
    api/            REST routes
    core/           render pipeline (docx + pdf), preflight, mapping, sanitize
    designtime/     upload / freeze / publish / audit / caller-profile mapping
    jobs/           background job workers (render, webhooks, watchdog)
    providers/      design-time AI adapters (stub, OpenAI-compatible, Nebius / Nemotron)
    store/          ORM models + S3-compatible blob helpers
    ui/             admin portal (server-rendered, pre-compiled CSS)
  pap_pipeline/     Verdikt — the decision pipeline (ingest → triage → decide → deliver)
    stages/         extract / triage / retrieve / reason
    schemas/        Intake / Decision / Rule pydantic contracts
    rules/          human-authored YAML rules packs (Cardiovita, OncoBridge)
    templates/      reply-letter templates (approve / deny / missing_info)
alembic/            DB migrations
scripts/            CI + local ops helpers (license_check, tenant_scope_audit, …)
sample_templates/   first-run demo document(s)
deploy/             production compose (reverse proxy + database + queue + object store)
```

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE).
