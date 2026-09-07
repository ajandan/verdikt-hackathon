# Design-time preflight as a Nebius Serverless Job

Quire's design-time inference (preflight + Jinja-tag + typed-field proposal)
runs off the request path as a **Nebius Serverless Job**. The web app
never blocks on a Nemotron round-trip during template upload — it kicks
off a job, returns immediately, and picks up the result JSON when the
job completes.

The render path is unchanged and has no dependency on this job.

## Contract

The job container reads a blank template file and writes a self-contained
result JSON.

- **Entry point**: `python -m docgen.designtime.preflight_job`
- **Input**: `--input <path>` — path to a `.docx` template inside the container
- **Output**: `--out <path>` — path to write result JSON
- **Provider config**: `--provider-config <path>` to a JSON file **or**
  the same `DOCGEN_AI_*` env vars the web app reads
- **Result shape** (`docs/HACKATHON.md` referenced by the app):

  ```json
  {
    "kind": "docx",
    "template_hash": "<sha256>",
    "elapsed_seconds": 12.3,
    "provider": "nebius",
    "preflight": { "outcome": "accept", "findings": [], "detected_variables": [...] },
    "mapping":   { "suggestions": [...], "notes": null }
  }
  ```

## Local build + smoke test

```bash
# Build the job image (~600 MB — will slim in follow-up).
docker build -f Dockerfile.job -t quire-preflight-job:local .

# Smoke test with the stub provider, no network required.
mkdir -p /tmp/qjob-in /tmp/qjob-out
cp sample_templates/denial_letter.docx /tmp/qjob-in/template.docx
docker run --rm \
  -v /tmp/qjob-in:/work/input:ro \
  -v /tmp/qjob-out:/work/output \
  -e DOCGEN_AI_PROVIDER=stub \
  quire-preflight-job:local

cat /tmp/qjob-out/result.json | jq '{provider, elapsed_seconds, fields: [.mapping.suggestions[].field.name]}'
```

Expected: `provider: "stub"`, 8 fields from the sample template, sub-second elapsed.

Swap to Nebius for a real inference smoke:

```bash
docker run --rm \
  -v /tmp/qjob-in:/work/input:ro \
  -v /tmp/qjob-out:/work/output \
  -e DOCGEN_AI_PROVIDER=nebius \
  -e DOCGEN_AI_API_KEY="$NEBIUS_TOKEN_FACTORY_KEY" \
  quire-preflight-job:local
```

## Publish to a Nebius-reachable registry

Nebius Serverless Jobs pulls images from a registry the project has
credentials for. Common choices: **Nebius Container Registry**,
**Docker Hub** (public), **GHCR**.

```bash
# Tag for your registry (example: Docker Hub).
docker tag quire-preflight-job:local <your-user>/quire-preflight-job:v0.1.0
docker push <your-user>/quire-preflight-job:v0.1.0
```

If the registry is private, register the credentials with Nebius AI Cloud
first (see the Nebius console → Project → Registry credentials).

## Register the Serverless Job

Use the Nebius console or CLI. Concept:

- **Image**: `<your-user>/quire-preflight-job:v0.1.0`
- **CPU / RAM**: 2 vCPU / 4 GiB is plenty — this container is a thin
  Python process; the inference happens on the Token Factory endpoint,
  not in the job.
- **Env vars** (secret):
  - `DOCGEN_AI_PROVIDER=nebius`
  - `DOCGEN_AI_API_KEY=<Token Factory key>`
  - (optional) `DOCGEN_AI_MODEL=<Nemotron variant>`
- **Volumes**:
  - Read-only input volume mounted at `/work/input`
  - Writable output volume mounted at `/work/output`
- **Command override**: leave empty to use the image's default CMD, or
  point `--input` / `--out` at the paths your platform mounts.

## Wire the web app to submit + poll

The web app currently runs the design-time mapping inline in
`src/docgen/designtime/mapper.py::propose_mapping_for_version`. To move
it off the request path:

1. On upload, put the template blob in the object store where the job
   can read it (already true — `store/blob.put_object`).
2. Submit a job run via the Nebius Jobs API with `--input` pointing at
   the blob URL and `--out` pointing at a fresh result URL.
3. Persist the job run ID next to the `TemplateVersion` row.
4. A Dramatiq worker (or webhook, once Nebius supports one) polls the
   job status; on success, downloads the result JSON, and calls the
   existing `_backfill_orphan_suggestions` path to write the mapping.

Keep `mapper.propose_mapping_for_version` on-tree as the fallback: if
`DOCGEN_PREFLIGHT_BACKEND=inline`, the app runs it in-process (used by
tests and the offline-safe demo). Prod defaults to `nebius_job`.

## Observability

- Every result JSON carries `template_hash`, `elapsed_seconds`, and
  `provider`. Log them at the audit boundary alongside the existing
  `template_uploaded` structured log.
- The job image runs as UID 10001 — same convention as the web image —
  so container runtime logs stay consistent.

## What this is *not*

- **Not a render worker.** The Quire render path is a pure, in-process
  merge with no model in the loop. This job only fires at design time.
- **Not the vision path.** The PDF placement editor's AI Draw / OCR
  Label / AI Realign buttons still call the Nebius Token Factory
  chat/completions endpoint directly from the web app; extracting the
  vision path into a second job image is a follow-up.
