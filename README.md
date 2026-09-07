# Quire

Template-based document generation with **design-time AI, deterministic render**.

You give Quire a `.docx` or `.pdf` with blanks. A large language model reads
the template once — during design time — and proposes where the fields go and
what type each one should be. From then on, every render is a pure,
reproducible, side-effect-free merge: same inputs, byte-identical output.
Audit-friendly. No model in the render path.

> **Nebius × NVIDIA Global AI Hackathon submission.** The design-time LLM
> runs against an **NVIDIA Nemotron** model hosted on **Nebius Token
> Factory**; the whole stack is deployed on **Nebius AI Cloud**. See
> [`docs/HACKATHON.md`](docs/HACKATHON.md) for what was significantly
> updated during the submission window and how to reproduce the demo.

- **Design-time AI**: preflight, field detection, Jinja-tag suggestion, caller-profile mapping — all bounded to the moment a template is first mapped.
- **Deterministic render**: pure template-engine merge for `.docx`, canvas-overlay merge for `.pdf`. No model in the loop at render time.
- **Draft → freeze → publish** lifecycle: version-pinned, rollback-safe, content-addressed blobs.
- **Multi-tenant**: row-level `tenant_id` scoping, API-key auth per tenant, CI-enforced [tenant-scope audit](scripts/tenant_scope_audit.py).
- **Admin portal + REST + MCP** all sit on the same domain layer — one code path, three transports.

## Live demo

- **Hosted demo**: <https://quire.<yourdomain>/ui/> (see [`docs/HACKATHON.md`](docs/HACKATHON.md) for the actual URL published at submission time)
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

That's the full loop: upload → preflight → freeze → publish → render.

### Where to go next

- **Hackathon submission notes**: [`docs/HACKATHON.md`](docs/HACKATHON.md) — what changed for the hackathon, judging-criteria mapping
- **Deploy on Nebius AI Cloud**: [`docs/deploy-nebius.md`](docs/deploy-nebius.md) — VM-to-public-URL runbook
- **REST reference**: <http://localhost:8000/docs> (Swagger, worked examples)
- **MCP endpoint**: `http://localhost:8000/mcp/` (streamable-http)
- **MinIO console**: <http://localhost:9011> (`minioadmin` / `minioadmin`)
- **Roadmap + design notes**: [`docs/roadmap.md`](docs/roadmap.md)

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
src/docgen/
  api/          FastAPI REST routes
  core/         render pipeline (docx + pdf), preflight, mapping, sanitize
  designtime/   upload / freeze / publish / audit / caller-profile mapping
  jobs/         Dramatiq workers (render, webhooks, watchdog)
  providers/    design-time AI adapters (stub, openai-compat, Nebius / Nemotron)
  store/        SQLAlchemy models + S3-compat blob helpers
  ui/           admin portal (server-rendered, Tailwind precompiled)
alembic/        DB migrations
scripts/        CI + local ops helpers (license_check, tenant_scope_audit, …)
sample_templates/  first-run demo document(s)
deploy/         production compose (Traefik / Caddy + Postgres + Redis + object store)
```

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE).
