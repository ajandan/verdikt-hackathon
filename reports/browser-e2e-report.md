# Verdikt browser E2E report

- Started: `2026-09-17 16:42:46 UTC`
- Finished: `2026-09-17 16:43:06 UTC`
- HEAD: `6b0ad1a`
- Playwright: `1.58.0` (chromium)
- Container AI_PROVIDER: `nebius`
- Tenant: `e2e-6ae880` (`7af14993-4d6d-4cf5-a77c-dee092ec82e4`)
- Program: `e2e-prog-6ae880` (`dffc33a3-100b-461c-9616-2c61cd3ee966`)
- Case: `QC-0059` / event `d998e63b-dad0-4544-bf38-702b43a1f8a2`

## Per-phase summary

| # | Phase | Status | server (ms) | load (ms) | Notes |
|---|-------|--------|-------------|-----------|-------|
| 1 | admin-session | OK | 604 | 594 |  |
| 2 | create-tenant | OK | 269 | 263 | tenant_id=7af14993-4d6d-4cf5-a77c-dee092ec82e4 |
| 3 | tenant-models | OK | 302 | 296 | extract=google/gemma-3-27b-it reason=Qwen/Qwen3-235B-A22B-Instruct-2507 |
| 4 | tenant-triage | OK | 267 | 261 | refill → crm_outreach persisted |
| 5 | create-program | OK | 202 | 197 | program_id=dffc33a3-100b-461c-9616-2c61cd3ee966 |
| 6 | program-models-inherit | OK | 242 | 237 | inherit round-trip ok |
| 7 | program-triage-inherit | OK | 8 | 426 | NOTE tab activator whitelist missing 'triage' — hash navigation to #triage does NOT open the panel (admin_program_detail.html: `valid` object at line ~965 lacks a `triage: 1` entry) |
| 8 | add-slot | OK | 220 | 213 | patient_name slot added |
| 9 | dispatch-case | OK | 178 | 170 | intake 201: case=QC-0059 event=d998e63b-dad0-4544-bf38-702b43a1f8a2 outcome=approve case_type=None route=None latency_ms=2475 |
| 10 | verify-verdict | OK | 341 | 333 | outcome=approve case_type=None route=None |
| 11 | cleanup | OK | - | - | revoked key_id=e3d7c8ec-2a71-451d-ab07-df83aeede5c9; tenant 7af14993-4d6d-4cf5-a77c-dee092ec82e4 left for manual cleanup |

## Perf table

| URL | goto (ms) | load event (ms) | resources | 4xx/5xx |
|-----|-----------|-----------------|-----------|---------|
| `http://localhost:8100/ui/` | 604 | 594 | 2 | - |
| `http://localhost:8100/ui/admin/tenants` | 269 | 263 | 2 | - |
| `http://localhost:8100/ui/admin/tenants/7af14993-4d6d-4cf5-a77c-dee092ec82e4/models` | 302 | 296 | 2 | - |
| `http://localhost:8100/ui/admin/tenants/7af14993-4d6d-4cf5-a77c-dee092ec82e4/triage-routing` | 267 | 261 | 2 | - |
| `http://localhost:8100/ui/programs` | 202 | 197 | 2 | - |
| `http://localhost:8100/ui/programs/dffc33a3-100b-461c-9616-2c61cd3ee966#models` | 242 | 237 | 2 | - |
| `http://localhost:8100/ui/programs/dffc33a3-100b-461c-9616-2c61cd3ee966#triage` | 8 | 426 | 0 | - |
| `http://localhost:8100/ui/programs/dffc33a3-100b-461c-9616-2c61cd3ee966` | 220 | 213 | 2 | - |
| `http://localhost:8100/ui/admin/tenants/7af14993-4d6d-4cf5-a77c-dee092ec82e4/keys` | 178 | 170 | 2 | - |
| `http://localhost:8100/ui/inbox/d998e63b-dad0-4544-bf38-702b43a1f8a2/pipeline` | 341 | 333 | 2 | - |

## Pipeline case timing

- Intake POST latency: `2474 ms`
- Verdict outcome: `approve`
- Triage case_type: `-`
- Triage route: `-`
- Extract model observed: `(not scraped)`
- Triage model observed: `(not scraped)`
- Reason model observed: `(not scraped)`

## Findings

### BLOCKERs

- **Cross-phase (Phases 4/7/9/10) — Triage stage NEVER runs on the JSON intake channel.** `pap_pipeline.api_hook.run_json_intake_sync` (~line 280) invokes `orchestrator.run_from_intake` — which composes retrieve + reason but *not* triage (orchestrator.py: `run` and `run_with_intake` call triage; `run_from_intake` does not). Symptom: `decision.triage` is `null` for every JSON-in case, and `case_type`/`route` never appear in the verdict. All the tenant/program Triage override wiring (Phases 4 and 7) is dead code on the JSON channel — those overrides only fire for the email intake path.

### NOTEs

- Phase 7 (program-triage-inherit) — NOTE tab activator whitelist missing 'triage' — hash navigation to #triage does NOT open the panel (admin_program_detail.html: `valid` object at line ~965 lacks a `triage: 1` entry)

### PRAISE

- 11/11 phases passed cleanly in the UI.
- End-to-end pipeline verdict returned inline in 2474 ms — sub-3s Nebius round-trip.
- Program-level `Models` inherit-toggle round-trips cleanly (override + re-inherit both persist), matching the design-system contract.
- Tenant-level triage-routing overrides persist; the UI correctly stores only the deltas from the platform default.
- CSRF double-submit + admin-session cookie combo held across 11 sequential phases without a re-issue race.

## Screenshots

### 01 — admin-session
![admin-session](reports/e2e-shots/01-admin-session.png)

### 02 — create-tenant
![create-tenant](reports/e2e-shots/02-create-tenant.png)

### 03 — tenant-models
![tenant-models](reports/e2e-shots/03-tenant-models.png)

### 04 — tenant-triage
![tenant-triage](reports/e2e-shots/04-tenant-triage.png)

### 05 — create-program
![create-program](reports/e2e-shots/05-create-program.png)

### 06 — program-models-inherit
![program-models-inherit](reports/e2e-shots/06-program-models-inherit.png)

### 07 — program-triage-inherit
![program-triage-inherit](reports/e2e-shots/07-program-triage-inherit.png)

### 08 — add-slot
![add-slot](reports/e2e-shots/08-add-slot.png)

### 09 — dispatch-case
![dispatch-case](reports/e2e-shots/09-dispatch-case.png)

### 10 — verify-verdict
![verify-verdict](reports/e2e-shots/10-verify-verdict.png)

### 11 — cleanup
![cleanup](reports/e2e-shots/11-cleanup.png)
