# Quire — Nebius × NVIDIA Global AI Hackathon submission

**Track:** Best Apps & Agents (productivity tools / copilots / workflows).
**Repo:** <https://github.com/ajandan/quire-hackathon> (public, MIT licensed).
**Submission window opened:** 2026-08-26. This document lists what was
significantly updated during the submission window.

## The one-line pitch

Quire turns any `.docx` or `.pdf` with blanks into a governed, deterministic
document API. AI reads the template once at design time, proposes typed fields
and Jinja tags for review, and never runs again — every subsequent render is a
byte-identical, audit-friendly merge.

## What the hackathon requires, and where it's satisfied

| Requirement | Where it lives |
| --- | --- |
| Runs on Nebius Token Factory / Nebius AI Cloud | Design-time LLM routes through Nebius Token Factory (`src/docgen/providers/nebius.py`); full stack deploys to a Nebius AI Cloud VM per [`docs/deploy-nebius.md`](deploy-nebius.md). |
| ≥ 1 NVIDIA open-source model | Default model is an NVIDIA Nemotron variant published under the `nvidia/` namespace on Nebius Token Factory. Enforced by `tests/test_provider_nebius.py::test_nebius_defaults_target_nvidia_nemotron` (default model must start with `nvidia/`). |
| Approved open-source license | MIT — see [`LICENSE`](../LICENSE). |
| Public repo, public demo URL, README with run steps | This repo + [`README.md`](../README.md) + the URL published in the Devpost submission. |
| YouTube video < 3 min | Script in [`docs/demo-script.md`](demo-script.md); link added to Devpost submission and back into this file when recorded. |

## What was significantly updated during the submission window

1. **Nebius Token Factory provider** — new
   `src/docgen/providers/nebius.py` wraps the design-time LLM path against
   Nebius Token Factory with an NVIDIA Nemotron default model. Wired through
   the existing per-tenant config overlay so any tenant can flip provider
   without redeploying.
2. **Config surface for the compliance model** —
   `Settings.ai_provider` gained a `"nebius"` literal
   (`src/docgen/config.py`); `providers/__init__.py` routes it. Existing
   `openai_compat` and `stub` paths still work; the `stub` provider remains
   the offline-safe default so tests and air-gapped demos keep working.
3. **Test coverage for the compliance surface** —
   `tests/test_provider_nebius.py` asserts the default model is
   NVIDIA-published and the default base URL points at Nebius. This is a
   guard rail: a bad refactor can't silently drift the demo off-compliance.
4. **Nebius AI Cloud deploy runbook** — [`docs/deploy-nebius.md`](deploy-nebius.md)
   walks a Nebius VM to a public, TLS-terminated Quire instance. Env template
   and Compose file were updated so `DOCGEN_AI_PROVIDER=nebius` is the
   documented primary path.
5. **README and public docs scrub** — capability-level descriptions of the
   render pipeline (no specific renderer/parsing library names) so the
   submission reads as a product, not a stack list.
6. **Demo script** — [`docs/demo-script.md`](demo-script.md) — < 3 min beats
   for the required YouTube video, tuned to the four judging criteria
   (technical implementation, design, potential impact, quality of idea).

## What was kept, and why

- **Document ingest / OCR** stays on Mistral OCR. NVIDIA's open-source
  vision-language models are strong on natural-image tasks but weaker on the
  dense-table and multi-column-layout PDFs that document generation depends
  on. Swapping ingest would regress product quality without changing
  hackathon compliance — the `≥ 1 NVIDIA OSS model` requirement is already
  satisfied by the design-time LLM. Kept as-is.
- **Deterministic render path** is unchanged and has no dependency on any
  AI provider. The package boundary is enforced in tests
  (`core/` does not import `providers/`).

## Judging-criteria mapping

- **Technical implementation.** Clean separation of design-time AI from
  render — the model literally cannot be reached during a render call. The
  compliance model is behind a provider abstraction with per-tenant
  override, so operators can A/B different Nemotron variants without code
  changes. Multi-tenancy is enforced by a CI-gated tenant-scope audit.
- **Design (complete product).** Admin UI, REST, and MCP all sit on the
  same domain layer. A first-run "try a sample" path takes you from empty
  state to a rendered document in under a minute.
- **Potential impact.** Template-driven document generation is one of the
  highest-frequency workflows in regulated industries (letters, statements,
  disclosures, filings). AI-in-the-loop mapping cuts the design-time cost
  of new templates from hours to minutes; determinism at render time keeps
  the output auditable — the combination is the actual product wedge.
- **Quality of idea.** Most doc-gen "AI" products put the model in the hot
  path and eat unpredictable outputs. Quire flips it: use AI where it's
  cheap and reviewable (once, at design time), and be boring where it
  matters (every render, forever after).

## Running the hackathon deploy locally

```bash
cp .env.example .env
# fill DOCGEN_SESSION_SECRET, DOCGEN_WEBHOOK_SIGNING_SECRET
# set DOCGEN_AI_PROVIDER=nebius and DOCGEN_AI_API_KEY=<Nebius Token Factory key>
docker compose up -d postgres redis minio minio-init
docker compose run --rm web alembic upgrade head
docker compose up -d web worker
open http://localhost:8000/ui/
```

For the public deploy, follow [`docs/deploy-nebius.md`](deploy-nebius.md).
