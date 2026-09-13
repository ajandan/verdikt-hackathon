# Verdikt

**AI-in, deterministic-out.** A decision pipeline for regulated document workflows.

Verdikt reads inbound documents, retrieves the human-authored rules that apply,
and issues a structured decision — `approve`, `deny`, or `missing_info` — with
citations back to the rules that fired. The reply letter that leaves the
building is a byte-identical merge of a frozen, human-approved template with
that decision. **No language model sits on the code path that produces the
audited artifact.**

> **Nebius × NVIDIA Global AI Hackathon submission.** Three NVIDIA models
> composed one-per-stage on Nebius Token Factory / Nebius AI Cloud: `nv-ingest`
> for multimodal extraction, NeMo Retriever NIMs (`nv-embedqa-e5-v5` +
> `nv-rerankqa-mistral-4b-v3`) for rule retrieval, Nemotron Super for
> structured-JSON reasoning. See [`docs/HACKATHON.md`](docs/HACKATHON.md) for
> the full submission write-up, the reuse declaration, and the deploy runbook.

- **AI where it earns its place**: extraction over messy multimodal PDFs, retrieval over versioned rules, reasoning to a structured verdict.
- **Deterministic where it must be**: pure template-engine merge for `.docx`, canvas-overlay merge for `.pdf`. No model in the loop at render time.
- **Programs as data**: a new PAP program is a `programs` row (extraction schema + rules pack + reply template), not a build. The OncoBridge cameo demonstrates the same code, opposite verdict.
- **Auditable end-to-end**: every stage's output is a structured artifact — intake object, retrieved rules with rerank scores, decision with citations, rendered letter. A reviewer can read the whole path from the admin UI.
- **Multi-tenant**: row-level `tenant_id` scoping, API-key auth per tenant, CI-enforced [tenant-scope audit](scripts/tenant_scope_audit.py).

## Architecture at a glance

Verdikt is the submission-window pipeline (`src/pap_pipeline/`) composed onto a
pre-existing deterministic-render doc-gen kernel (`src/docgen/`, historically
"Quire"). The split is enforced mechanically:

- `tests/test_boundaries.py::test_docgen_never_imports_pap_pipeline` fails the build if the kernel ever reaches back into Verdikt.
- `tests/test_boundaries.py::test_core_never_imports_ai_layer` fails the build if the deterministic render path ever imports an AI provider.

The reuse declaration lives in [`docs/HACKATHON.md`](docs/HACKATHON.md) with a
mechanical proof (git log + boundary tests).

## Live demo

- **Hosted demo**: <https://verdikt.<yourdomain>/ui/> (URL published in [`docs/HACKATHON.md`](docs/HACKATHON.md) at submission time)
- **Demo video (< 3 min)**: linked from [`docs/HACKATHON.md`](docs/HACKATHON.md)

## Quickstart (< 5 minutes, local)

You need: **Docker Desktop** (or a Docker Engine + `docker compose` CLI). No Python required on the host.

```bash
git clone https://github.com/ajandan/quire-hackathon.git
cd quire-hackathon
cp .env.example .env

# Generate the two required secrets and paste into .env:
python -c "import secrets; print('DOCGEN_SESSION_SECRET=', secrets.token_urlsafe(48), sep='')"
python -c "import secrets; print('DOCGEN_WEBHOOK_SIGNING_SECRET=', secrets.token_urlsafe(48), sep='')"

# Optional but recommended — wire up the design-time LLM:
#   1. Get a Nebius Token Factory API key at https://studio.nebius.com/
#   2. In .env, set: DOCGEN_AI_PROVIDER=nebius and DOCGEN_AI_API_KEY=<your key>
# (Skip this step to run with DOCGEN_AI_PROVIDER=stub — the deterministic
#  placeholder matcher, useful for offline dev.)

# Bring the stack up.
docker compose up -d postgres redis minio minio-init
docker compose run --rm web alembic upgrade head
docker compose up -d web worker
```

Open **<http://localhost:8000/ui/>**. Sign in as `admin` (dev mode; set
`DOCGEN_ADMIN_PASSWORD_HASH` before you point this at anything real — see
`.env.example`).

On the empty templates list, click **"Or try a sample →"** — pick the
**EOB (real-world)** button to load `sample_templates/insurance_eob.docx`,
an Explanation of Benefits letter that exercises scalar fields, a
service-line `{% for %}` loop, and an `{% if is_denied %}` block so you can
see the mapping wizard's type inference and reasoning trace in one screen.
The **DOCX** button loads the shorter denial-letter sample if you want a
simpler walk-through. Preflight runs on upload; from there fill a couple of
fields, freeze the version, publish it, then hit **Render** to download a
filled `.docx`.

That's the doc-gen kernel loop: upload → preflight → freeze → publish → render.
For the full Verdikt pipeline (inbox → extract → retrieve → reason → reply),
walk through [`docs/HACKATHON.md`](docs/HACKATHON.md).

### Where to go next

- **Hackathon submission notes**: [`docs/HACKATHON.md`](docs/HACKATHON.md) — architecture, reuse declaration, judging-criteria mapping
- **Deploy on Nebius AI Cloud**: [`docs/deploy-nebius.md`](docs/deploy-nebius.md) — VM-to-public-URL runbook
- **REST reference**: <http://localhost:8000/docs> (Swagger, worked examples)
- **MCP endpoint**: `http://localhost:8000/mcp/` (streamable-http)
- **MinIO console**: <http://localhost:9011> (`minioadmin` / `minioadmin`)
- **Roadmap + design notes**: [`docs/roadmap.md`](docs/roadmap.md)
- **Design system reference**: [`docs/design-system.html`](docs/design-system.html) (open directly — self-contained, no server needed)

## Development

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install                              # ruff + tenant-scope audit on every commit
pytest -v                                       # unit + boundary + golden tests
ruff check src tests scripts                    # linter (CI runs this too)
python scripts/tenant_scope_audit.py            # multi-tenancy hygiene
```

CI runs lint, tests, license scan, and the tenant-scope audit on every push —
see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Repo layout

```
src/
  docgen/           deterministic-render doc-gen kernel (the "Quire" layer)
    api/            FastAPI REST routes
    core/           render pipeline (docx + pdf), preflight, mapping, sanitize
    designtime/     upload / freeze / publish / audit / caller-profile mapping
    jobs/           Dramatiq workers (render, webhooks, watchdog)
    providers/      design-time AI adapters (stub, openai-compat, Nebius / Nemotron)
    store/          SQLAlchemy models + S3-compat blob helpers
    ui/             admin portal (server-rendered, Tailwind precompiled)
  pap_pipeline/     Verdikt — the three-stage decision pipeline (extract → retrieve → reason)
    stages/         extract / retrieve / reason
    schemas/        Intake / Decision / Rule pydantic contracts
    rules/          human-authored YAML rules packs (Cardiovita, OncoBridge)
    templates/      reply-letter templates (approve / deny / missing_info)
alembic/            DB migrations
scripts/            CI + local ops helpers (license_check, tenant_scope_audit, …)
sample_templates/   first-run demo document(s)
deploy/             production compose (Traefik / Caddy + Postgres + Redis + object store)
```

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE).
