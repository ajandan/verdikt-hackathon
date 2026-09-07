# Quire

Template-based document generation with **design-time AI, deterministic render**.

You give Quire a `.docx` or `.pdf` with blanks. It uses an LLM once — during
design time — to figure out where the fields go. From then on, every render is
a pure, reproducible, side-effect-free merge: same inputs, byte-identical
output. Audit-friendly. No model in the render path.

- **Design-time AI**: preflight, field detection, Jinja tag suggestion, caller-profile mapping.
- **Deterministic render**: Jinja/`docxtpl` for `.docx`, pypdf + reportlab overlay for `.pdf`. No AI at render time.
- **Draft → freeze → publish** lifecycle: version-pinned, rollback-safe, content-addressed blobs.
- **Multi-tenant**: row-level `tenant_id` scoping, API-key auth per tenant, CI-enforced [tenant-scope audit](scripts/tenant_scope_audit.py).
- **Admin portal + REST + MCP** all sit on the same domain layer — one code path, three transports.

## Quickstart (< 5 minutes)

You need: **Docker Desktop** (or a Docker Engine + `docker compose` CLI). No Python required on the host.

```bash
git clone https://github.com/ajandan/quire-hackathon.git
cd quire-hackathon
cp .env.example .env

# Generate the two required secrets and paste into .env:
python -c "import secrets; print('DOCGEN_SESSION_SECRET=', secrets.token_urlsafe(48), sep='')"
python -c "import secrets; print('DOCGEN_WEBHOOK_SIGNING_SECRET=', secrets.token_urlsafe(48), sep='')"

# Bring the stack up.
docker compose up -d postgres redis minio minio-init
docker compose run --rm web alembic upgrade head
docker compose up -d web worker
```

Open **<http://localhost:8000/ui/>**. Sign in as `admin` (dev mode; set
`DOCGEN_ADMIN_PASSWORD_HASH` before you point this at anything real — see
`.env.example`).

On the empty templates list, click **"Or try a sample →"** — Quire uploads
`sample_templates/denial_letter.docx`, runs preflight, and drops you into the
mapping wizard. Fill a couple of fields, freeze the version, publish it, then
hit **Render** to download a filled `.docx`.

That's the full loop: upload → preflight → freeze → publish → render.

### Where to go next

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
  store/        SQLAlchemy models + S3-compat blob helpers
  ui/           admin portal (server-rendered, Tailwind precompiled)
alembic/        DB migrations
scripts/        CI + local ops helpers (license_check, tenant_scope_audit, …)
sample_templates/  first-run demo document(s)
deploy/         production compose (Traefik + Postgres + Redis + MinIO)
```

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE).
