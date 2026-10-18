# Running Verdikt yourself

The hosted demo at <https://verdikt.aismith.ai> needs no setup; see the [README](../README.md#try-it). This page is for running the stack on your own machine or server.

## Quickstart (< 5 minutes)

You need: a container runtime with the `docker compose` CLI. No Python required on the host.

```bash
git clone https://github.com/ajandan/verdikt-hackathon.git
cd verdikt-hackathon
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

Open **<http://localhost:8100/ui/>**. Sign in as `admin` (dev mode; set
`DOCGEN_ADMIN_PASSWORD_HASH` before you point this at anything real — see
`.env.example`).

## First-run walkthrough (doc-gen kernel)

On the empty templates list, click **"Or try a sample →"** and pick one:

- **EOB (real-world)** loads `sample_templates/insurance_eob.docx`, an Explanation of Benefits letter that exercises scalar fields, a service-line `{% for %}` loop, and an `{% if is_denied %}` block — so you see the mapping wizard's type inference and reasoning trace in one screen.
- **DOCX** loads the shorter denial-letter sample if you want a simpler walk-through.

Preflight runs on upload; from there fill a couple of fields, freeze the version, publish it, then hit **Render** to download a filled `.docx`.

That's the doc-gen kernel loop: **upload → preflight → freeze → publish → render**. For the full Verdikt pipeline (**inbox → extract → retrieve → reason → reply**), follow the email test in the [README](../README.md#test-it-by-email) against your local stack, or read [`HACKATHON.md`](HACKATHON.md).

## Where to go next

- **Hackathon submission notes**: [`HACKATHON.md`](HACKATHON.md) — architecture, reuse declaration, judging-criteria mapping
- **Deploy**: self-hosted. Model inference runs on Nebius Token Factory; [`deploy-nebius.md`](deploy-nebius.md) keeps the Nebius AI Cloud VM runbook for reference.
- **REST reference**: <http://localhost:8100/docs> (OpenAPI, worked examples)
- **MCP endpoint**: `http://localhost:8100/mcp/` (streamable-http)
- **Object-store console**: <http://localhost:9011> (`minioadmin` / `minioadmin`)
- **Roadmap + design notes**: [`roadmap.md`](roadmap.md)
- **Design system reference**: [`design-system.html`](design-system.html) (open directly — self-contained, no server needed)

## Development

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install                              # ruff + tenant-scope audit on every commit
pytest -v                                       # unit + boundary + golden tests
ruff check src tests scripts                    # linter (CI runs this too)
python scripts/tenant_scope_audit.py            # multi-tenancy hygiene
```

CI runs lint, tests, license scan, CVE scan, and the tenant-scope audit on
every push — see [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
