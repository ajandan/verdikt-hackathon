# Quire — Improvement Roadmap

_Last updated: 2026-09-03 (PDF-as-template feature — PR1 + PR2 shipped, PR3 in flight)._

Prioritized by impact ÷ effort. Each item is a concrete change, not a
research topic. When you pick one up, cross it off and add a link to the
PR / commit.

---

## PDF templates (kind='pdf')

New in 2026-09: PDF is a first-class template kind alongside DOCX.
Uploaded PDFs go through their own preflight/render pipeline (no DOCX
→ PDF conversion path — that experiment was reverted 2026-08-31).
Two flavors:

- **`acroform`** — source PDF has fillable widgets. Preflight harvests
  each widget's page + rect into initial placements, so the template
  is renderable immediately.
- **`flat`** — no widgets. Placements start empty; the user must open
  the placement editor before the template will render.

Both flavors render through a unified overlay pipeline (reportlab draws
values, pypdf merges them onto the source page, form widgets are
stripped from the output).

### Shipped

- ~~**PR1 — Upload + render pipeline.**~~ **Done 2026-09-01.**
  `Template.kind`, `TemplateVersion.pdf_flavor`,
  `TemplateVersion.pdf_field_placements` columns + migration `0011`.
  `designtime/preflight_pdf.py` classifies flavor and harvests
  AcroForm widget rects into placements as page-relative percentages
  (top-left origin). `designtime/upload.py::upload_pdf_template` mirrors
  the DOCX upload path and enforces kind-fixed-per-template on new
  versions. `core/render_pipeline_pdf.py` runs the reportlab-overlay
  + pypdf-merge pipeline and strips `/AcroForm` from the output.
  `core/render_pipeline.py` dispatches by `tv.pdf_flavor is not None`
  so all four render entry points (REST / UI / MCP / worker) route
  PDF templates automatically. UI upload dropzone accepts `.pdf`;
  `/ui/versions/{id}/map` short-circuits with a friendly redirect
  for PDF templates (no Jinja tags to align — placement name IS the
  payload key). Coverage in `tests/test_pdf_template.py`.

- ~~**PR2 — Flat placement editor.**~~ **Done 2026-09-03.**
  `freeze_pdf_version` in `designtime/freeze.py` builds the schema from
  placements, hashes source + canonical placements, and rejects an empty
  list up-front. `GET /ui/versions/{id}/pdf-page/{n}.png` in `ui/routes.py`
  rasterizes via pymupdf @ 150 DPI and caches PNGs to blob storage under
  `previews/{template_id}/v{version}/page_{n}.png` (1-hour Cache-Control).
  `ui/templates/placement_editor.html` renders a per-page canvas with
  click-to-place, drag / resize, keyboard nudge (arrow keys · 0.5 px,
  Shift · 5 px), dedicated text and square-locked checkbox tools, and a
  name / font-size / anchor form; save is `POST /versions/{id}/placements`
  (POST rather than PATCH to sit inside the existing CSRF path — auto-
  creates a sub-version if the current tip is frozen). AcroForm and flat
  PDFs share the same editor via a `PDF_FLAVOR` branch. Template detail
  gets a placements table + "Edit placements" jump per version.
  Empty-placements render fails clean with 422 at `api/routes.py:396+`
  before the job enqueues (covered by
  `test_render_endpoint_422_when_flat_has_no_placements`).
  `GET /ui/versions/{id}/pdf-preview-merged` renders a sample payload
  through the real pipeline for preview-at-freeze. Bonus beyond the
  original scope: `/placements/ai-draw` and `/placements/ai-detect`
  endpoints propose boxes for flat PDFs (deterministic pymupdf geometry
  + text-only NVIDIA Nemotron labeler on Nebius Token Factory).
  Coverage in `tests/test_pdf_template.py`
  (`test_pdf_page_preview_endpoint_returns_png`, `test_placements_save_persists`,
  `test_freeze_pdf_happy_path`, `test_freeze_pdf_rejects_empty_placements`,
  `test_pdf_preview_merged_endpoint`, `test_placements_ai_map_slugs_labels`).

### Pending

- **PR3 — AI re-alignment on PDF re-upload.**
  - ~~**Phase A: heuristic re-align.**~~ **Done 2026-09-03.**
    `designtime/realign_pdf.py` runs on every PDF re-upload
    (`upload_pdf_template` when `template_id` is set). Two heuristics:
    (1) name-match prior placements to new AcroForm widget rects
    (confidence 1.0, status "auto") — operator's label/font_size/type
    stay put, only geometry updates; (2) preserve prior page-relative
    coords when no name match (confidence 0.5, status "needs_review").
    New widgets appearing in the re-upload get appended as fresh
    placements. Prior placements on pages that no longer exist get
    dropped. The `RealignReport` (matched_by_name /
    preserved_by_coords / added_from_new / dropped_off_page) is
    stashed under `pdf_field_placements["realign"]` and surfaced in
    the editor as an amber banner. Placements with
    `realign_status="needs_review"` get a dashed amber outline + `?`
    badge; any drag/resize/edit clears the flag. Save path preserves
    the realign metadata so partial reviews don't get wiped.
    Coverage in `tests/test_realign_pdf.py` (11 tests: name match,
    drift-flip-to-review, no-drift-stays-auto, prior_coords stash,
    coords preserve, added widget, dropped page, case sensitivity,
    empty prior, malformed prior, prior-h_pct fallback,
    prior-type-wins).
  - ~~**Phase B: diff review UI.**~~ **Done 2026-09-03.** Name-matched
    placements whose new coords drift more than 1% on any axis now
    stamp `prior_coords` and flip to `needs_review`. The editor draws
    a dashed grey "was here" ghost outline behind the live pill, and
    the sidebar shows a review block with Accept (clears the flag)
    and Revert (restores prior coords) buttons. Save path validates
    and preserves `prior_coords` so round-trips don't lose the ghost.
    Ghost only renders when drift is meaningful — preserved-coords
    placements (where prior == current) skip the outline to avoid
    visual noise.
  - ~~**Phase C: AI fallback for heavy drift.**~~ **Done 2026-09-03.**
    New endpoint `POST /versions/{id}/placements/ai-realign` runs
    `detect_fields_in_pdf` + `label_fields_in_pdf` on the current
    version's stored PDF, then pairs the freshly-detected fields to
    needs-review placements by normalized-label Jaccard similarity
    (threshold 0.6, greedy 1:1 per page). The realign banner shows a
    "Try AI re-detect" button whenever any needs-review placement has
    a label to match on. On success the editor updates coords in
    place, stamps `realign_source="ai_label_match"` and flips status
    → auto. Unit-tested via `match_placements_by_label` in
    `test_realign_pdf.py` (exact match, punctuation/case tolerance,
    threshold behavior, same-page filter, label-less skip, greedy
    tiebreak). No test coverage for the endpoint itself — it's a thin
    orchestrator around detect + label + match, all of which are
    covered independently.

### Deferred (called out but not scheduled)

- Non-text widget types.
  - ~~**Checkbox proper render.**~~ **Done 2026-09-03.** Placements now
    carry an explicit `type` field (with backward-compat fallback to
    the legacy `field_type` / `payload_type` signals), the render
    pipeline dispatches by type, and checkbox render draws a stroked
    box frame plus the centered check glyph — so flat-PDF checkboxes
    are visible even without a source widget behind them.
  - ~~**Radio buttons.**~~ **Done 2026-09-03.** New `addRadioBtn` tool in
    the placement editor drops square placements with `type: "radio"`;
    render pipeline's `_draw_radio` draws a circle outline (always) plus
    a smaller filled inner circle when the payload value is truthy —
    reuses the checkbox truthy rules so selection semantics stay in sync.
    Save path preserves the `type` field so radios survive a round-trip
    through `POST /versions/{id}/placements`. Coverage in
    `tests/test_pdf_template.py`
    (`test_render_pdf_radio_draws_outline_when_unselected`,
    `test_render_pdf_radio_draws_filled_dot_when_selected`,
    `test_render_pdf_radio_reuses_checkbox_truthy_rules`).
  - **Signature fields** — still coerced to text. Needs a product call
    on the payload format (base64 PNG data URL? blob key? URL?) and a
    fallback UX for missing values before the render side is meaningful.

---

## Ship-first (best impact ÷ effort)

1. ~~**Precompile Tailwind at build time.**~~ **Done 2026-08-24.**
   Standalone `tailwindcss` binary (no Node, bundles forms + typography)
   compiles `src/docgen/ui/tailwind.src.css` → `static/app.css` (~17 KB
   minified, vs ~370 KB from the CDN). Dockerfile builds it in a
   `css-builder` stage; dev uses `scripts/build-css.{ps1,sh}` (with
   `--watch`); FastAPI mounts `/static`; auth middleware bypasses
   `/static/*`.
2. ~~**E2E happy-path test: upload → preflight → freeze → render → download.**~~
   **Done 2026-08-24.** `tests/test_e2e_pipeline.py` walks upload → map →
   freeze → publish → render → download via TestClient against the real
   compose stack (Postgres + MinIO). Pivoted away from Playwright — same
   "lock the loop" guarantee at ~⅕ the effort, no new deps, matches the
   existing integration-test style. Asserts DOCX zip magic + rendered field
   values in the downloaded artifact.
3. ~~**CSP + hardened session cookies.**~~ **Done 2026-08-24.**
   `SecurityHeadersMiddleware` stamps CSP (with `frame-ancestors 'none'`,
   `object-src 'none'`), HSTS, `X-Content-Type-Options`, `X-Frame-Options:
   DENY`, `Referrer-Policy`, and a minimal `Permissions-Policy` on every
   response. `/docs` gets a relaxed CSP for Swagger's CDN bundle. New
   `secure_cookie_flag()` helper computes `Secure` from request scheme,
   `X-Forwarded-Proto` (behind a TLS-terminating proxy), or the
   `DOCGEN_SECURE_COOKIES` / `DOCGEN_HSTS_ENABLED` overrides; wired into all
   session + CSRF cookie writes. Cookies remain `HttpOnly; SameSite=Lax`.
4. ~~**Rate-limit `/ui/login` and audit failed attempts.**~~ **Done 2026-08-24.**
   Sliding-window limiter in `docgen.ratelimit` (`check_login_attempt` /
   `reset_login_attempts`), keyed on `(client_ip, username)`, backed by a
   Redis sorted set with a 15-minute window and a 10-attempt cap. Wired into
   `login_submit` in `docgen/ui/routes.py`: the check runs after the admin
   user/hash lookup but before the password compare, so a tripped bucket
   returns a 429 template response with a `Retry-After` header even when the
   password is correct. Successful login calls `reset_login_attempts` to wipe
   the bucket. `client_ip` trusts the first hop of `X-Forwarded-For` so the
   limiter keys on the real caller behind the proxy. Fail-open on Redis
   errors — an outage doesn't lock the admin out of their own console.
   Failed attempts are logged (`admin_login_failed`) with `username` + `ip`
   + `request_id` for audit. Coverage in `tests/test_login_ratelimit.py`
   (6 tests: window not tripped by N-1, 429 on N+1 even with correct
   password, bucket reset on success, per-IP isolation, fail-open on Redis
   down, log payload shape).
5. ~~**Pagination on templates & renders lists.**~~ **Done 2026-08-24.**
   New `docgen.pagination` module provides opaque base64 `(timestamp, id)`
   cursors + a `keyset_where(ts, id, cursor)` helper, so both the templates
   list (`/ui/`) and the new renders list (`/ui/renders`) key-set-paginate
   at constant DB cost regardless of page depth. `?limit=` capped at 200,
   default 50. Malformed cursors fall back to page 1 rather than 400ing —
   stale bookmarks stay soft-fail. `/ui/renders` is a new operator page
   backed by the same cursor infra; state (`?state=succeeded|failed|…`)
   and tenant filters survive across pages via the Next link. Sidebar
   gets a "Renders" entry between Dashboard and Upload. Coverage in
   `tests/test_pagination.py` (14 tests: cursor encode/decode round-trip,
   malformed-cursor fallback, page-size clamp, `Page.from_rows` sentinel
   slicing, and integration walks that seed 12 rows into an isolated
   tenant and prove every row appears exactly once across ≥ 3 pages with
   filters preserved).
6. ~~**Empty-state onboarding.**~~ **Done 2026-09-03.** Empty templates list
   now shows a secondary "Or try a sample →" button next to the "Upload
   your first template" primary CTA. Button POSTs to `/ui/upload/sample`,
   which reads `sample_templates/denial_letter.docx` off disk and pushes
   it through the same `upload_template()` pipeline as an operator upload
   (so preflight + sanitize + audit all fire). Tenant is inherited from
   the sidebar picker's cookie via a hidden form field. New tenants get
   from empty state → working template in one click.
7. **Command palette (⌘K).** Jump to any template, upload, open the mapping
   wizard, or switch tenant. Table view is the bottleneck once you pass
   ~20 templates.

## Reliability

- ~~Idempotency keys on `POST /render` and `POST /render/mapped` (persist
  `request-id → response` for 24 h).~~ **Done 2026-09-03.** Both routes
  now honor `Idempotency-Key`: replay with same key + same fingerprint
  returns the original `RenderResponse`; same key + different payload is
  a 409 (`IdempotencyRaceMismatch`). `/render/mapped` fingerprints the
  *mapped* payload (post-profile-translation) rather than raw `body.data`
  so a profile edit doesn't silently share an idempotency window with
  the pre-edit call. Storage + race-safe record path lives in
  `docgen.core.idempotency`; helpers `_normalize_idempotency_key` and
  `_handle_idempotent_replay` are shared between the two routes in
  `api/routes.py`.
- ~~DLQ visibility page — async failures are currently visible only in
  logs.~~ **Superseded 2026-09-03; already shipped under a different
  name.** Dashboard's "Failing jobs (last 7d)" widget in
  `ui/routes.py::dashboard` surfaces the last 10 failures with
  template + error inline. `/ui/renders?state=failed` gives the full
  paginated triage view (state chip filter in `renders.html:45`) with
  the error text in the last column (`fail-cell` truncated + tooltip
  full text). Watchdog (`docgen/jobs/watchdog.py`) reaps orphaned
  `state='running'` rows into `state='failed'` on worker boot so the
  view stays accurate. What's *not* in yet — a "Retry" action, error
  grouping / trending, and failure-rate alerting — belongs in
  Observability below, not here.
- ~~Contract test: a caller profile whose template version got bumped must
  fail loud, not silently backfill.~~ **Done 2026-09-03.** New test
  `test_profile_version_pin_survives_template_bump` in
  `tests/test_render_mapped.py` seeds v1 + v2 of the same template
  (both frozen, `current_version` bumped to 2), creates a
  `CallerProfile` pinned to v=1, calls `POST /render/mapped`, and
  asserts the resulting `RenderJob.template_version_id` matches the
  v1 `tv.id` — regression guard against `_resolve_frozen_version`
  ever silently substituting `template.current_version` for
  `profile.version`.
- Snapshot-test the rendered DOCX vs a golden fixture per sample template.

## Security

- ~~Hash API keys at rest (SHA-256) with a return-once-on-creation UX.~~
  **Already shipped** — verified 2026-09-03. `docgen/auth.py::hash_api_key`
  is SHA-256, `ApiKey.key_hash` is the only stored form; middleware looks
  up by digest at `auth.py:165`. Creation flow in
  `ui/routes.py::admin_create_key` returns the raw key via a redirect
  `?new_key=` param and `admin_keys.html` displays it inside a
  copy-once banner ("this is the only time it will be shown"). Nothing
  to do here — bullet was stale.
- ~~Verify uploaded `.docx` by magic bytes and strip macros before persisting.~~ **Done 2026-09-03.**
  `docgen/core/docx_sanitize.py` runs first in both `upload_template` and
  `upload_locale_variant`: rejects payloads without the `PK\x03\x04` ZIP
  magic or without `[Content_Types].xml`, and strips `word/vbaProject.bin`
  + `word/vbaData.xml` from the archive before persistence. Stripping
  chosen over rejection so an operator uploading a mis-saved `.docm→.docx`
  still gets a usable template (macros dropped, body intact); the audit
  event records which parts were stripped. UI and API upload paths both
  translate `InvalidDocxError` → user-facing flash / HTTP 400.
  Coverage in `tests/test_docx_sanitize.py` (7 tests: non-zip, missing
  manifest, corrupt zip, no-op pass-through, single-part strip, both
  macro parts stripped, unrelated parts untouched).
- Dependency CVE scan in CI (`pip-audit` or Trivy). **Blocked on dep
  cleanup** — dry-run `pip-audit --skip-editable` in the current env
  surfaces 231 CVEs across 41 packages (pypdf, aiohttp, pyjwt,
  cryptography, starlette, sqlparse, urllib3, weasyprint, and the
  full `mcp` transitive fan-out). Landing `pip-audit --strict` in CI
  today would wedge every PR. Sequence: (1) upgrade in-place fixes
  (sqlparse 0.6, urllib3 2.7, pyjwt 2.13, python-multipart 0.0.31,
  cryptography 46.0.7) — no breaking changes; (2) evaluate
  `starlette 1.x` bump against FastAPI compatibility (this is the
  breaking one); (3) evaluate whether `mcp` fat-transitives
  (chromadb, aiohttp, authlib) can be pinned or the `mcp` dep swapped
  for a slimmer client; (4) land the CI step with `--skip-editable`
  (editable installs of `docgen`, `agent-blueprint-spec`, etc. are
  legit — not on PyPI). We deliberately don't ship an `--ignore-vuln`
  allowlist as a stopgap; that hides real security debt and rots.
- ~~Threat-model the caller-profile mapping engine — source paths are
  user-supplied strings; confirm there is no eval / injection path.~~
  **Reviewed 2026-09-03; no injection path.** `docgen/core/mapping.py`
  is intentionally small and closed:
  - `resolve_path` accepts only `$( .name | [N] )*` via a strict regex
    (`\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]`). No attribute access,
    no `getattr`, no dict-method dispatch. Malformed segments fall
    through silently (returning None for that path) — no code path
    parses caller input as Python.
  - `apply_transform` dispatches via an explicit if/elif chain on a
    hardcoded name set (`number`, `date`, `join`, `split`, `upper`,
    `lower`, `default`); anything else raises. `date` runs the arg
    through `_to_strftime` which regex-substitutes a fixed token map
    before calling `strftime` — no `%`-format injection surface
    beyond what Python's `strftime` already tolerates.
  - `apply_mapping` reads only known keys (`constant`, `source`,
    `transform`) from each spec dict.
  - No eval, exec, `__getattr__` traversal, or dynamic dispatch
    anywhere in the module.

  Non-security notes captured for later hardening (not vulnerabilities,
  just DoS-adjacent bounds worth adding when we get profile-size
  limits): no cap on mapping-entry count, no cap on source-path length,
  no cap on `constant` size. The regex uses simple alternation so no
  catastrophic backtracking. Leaving these open — the auth + rate
  limit layer bounds volume long before they'd matter.

## Performance

- N+1 audit on `/ui/dashboard` and `/ui/templates` — **audited
  2026-09-03; no true N+1 found.** Both routes are already single-query
  aggregations (dashboard: recent-renders, recent-publishes, failing-jobs,
  top-templates all use explicit JOINs; index: template list uses a
  grouped join with `func.count(TemplateVersion.id)` and a
  `last_render_sq` subquery). The one waste we did find was `_base_ctx`
  re-querying the tenant list even when the route had already fetched
  it at the top (dashboard, index, renders, upload_get all did this) —
  fixed by having `_base_ctx` reuse `extra["all_tenants"]` when the
  caller provides it. Saves one `SELECT name, id FROM tenants ORDER BY
  name` per hit on those 4 pages. Deferring the SQLAlchemy
  `selectinload` sweep since the current queries hydrate exactly what
  the templates render — no lazy-load traps found.
- ~~Cache `GET /schema/{template_id}` — schemas change only on freeze.~~
  **Done 2026-09-03.** Endpoint stamps `ETag: "{version_hash}"` and
  handles `If-None-Match` → 304. Version hashes are content-addressed
  and immutable per frozen version, so an ETag hit skips the JSON-schema
  derivation (the expensive part). `Cache-Control` is set based on
  whether the caller pinned a version: `?version=N` requests get
  `max-age=86400, immutable`; latest-version lookups get
  `max-age=60, must-revalidate` so clients notice new freezes within
  a minute. The DB lookup for tenant + version resolution still
  happens on every request — indexed and cheaper than the schema
  derivation, plus skipping it would open a cross-tenant leak surface.
- Cache PDF page-preview PNGs to blob storage keyed by version hash —
  pymupdf rasterization is the placement-editor's hot path.

## Observability

- OpenTelemetry traces end-to-end (FastAPI → SQLAlchemy → boto3 →
  pypdf / reportlab render span).
- Per-tenant metrics: renders/hr, preflight-failure rate, P99 render
  latency.
- Alerts on queue depth over threshold and DLQ growth.

## Multi-tenancy hygiene

- ~~Automated audit: grep every SQLAlchemy query for a missing
  `Template.tenant_id ==` filter.~~ **Done 2026-09-03.**
  `scripts/tenant_scope_audit.py` walks every `.py` under `src/docgen/`
  and flags any `select|update|delete(Model...)` or
  `session.get(Model, ...)` call against a direct tenant-scoped model
  (Template, RenderJob, CallerProfile, ApiKey, IdempotencyKey) that
  lacks a `tenant_id` reference within 40 lines. Suppression via
  inline `# tenant-audit: allow (<reason>)` (or module-level
  `# tenant-audit: file-allow` for admin-superuser modules like
  `ui/routes.py` where cross-tenant is by design). Wired into CI
  after the license scan — a new violation fails the build. Current
  suppressions: `ui/routes.py` (whole admin portal), `jobs/render.py`
  + `jobs/webhooks.py` + `jobs/watchdog.py` (worker actors trust
  queue-side scoping), the three `Template.id ==` FOR UPDATE
  row-locks in `designtime/freeze.py` and `designtime/publish.py`
  (PK-only, called from already-scoped REST handlers), and the
  `_bump_last_used(api_key_id)` timestamp write in `auth.py` (the
  api-key row was already resolved by hash before tenant was
  known). Indirectly-scoped models (TemplateVersion → Template,
  Provenance → RenderJob, etc.) are out of scope for this first
  pass — join structure varies too much to check mechanically
  without noise.
- Per-tenant quotas: renders/day, storage MB, active templates.
- Tenant-scoped API key visibility surface — decide what non-admins see.

## UX polish

- Responsive tables → card view on mobile (< 720 px).
- Live template preview on the schema / mapping-wizard screens.
- Glossary in the docs: "preflight", "freeze", "publish", "profile" — the
  vocabulary is unfamiliar to new users.
- Keyboard traversal audit through the mapping wizard.
- ~~`focus-visible` ring + skip-to-content link.~~ **Done 2026-09-03.**
  `base.html` now emits a "Skip to main content" link as the first
  focusable element on every non-bare page — hidden off-screen until
  Tab focuses it, then snaps into the top-left corner. Target is
  `<main id="main-content" tabindex="-1">`. Global `:focus-visible`
  rule paints a 2 px brand-color outline (+2 px offset) on links,
  buttons, `.btn`, `.icon-btn`, `.nav-link`, and `[role="button"]`;
  form inputs keep their existing focus treatment (border + soft
  shadow). Chose `:focus-visible` over `:focus` so pointer users
  don't see a ring on every click. Both selectors work in the
  skip-link so keyboard focus wins in every browser.

## Docs & brand

- ~~README quickstart: `docker compose up` → open `/ui/` → sample-template
  walkthrough in under 5 minutes.~~ **Done 2026-09-03.** New `README.md`
  at repo root: one-paragraph pitch, three-bullet why, then a copy-pasteable
  block that clones → generates secrets → brings up the compose stack →
  runs migrations → opens `/ui/`. Walks the reader through the empty-state
  sample-template button (shipped 2026-09-03) so first render happens
  inside 5 minutes of clone. Includes a dev section (pytest, ruff,
  pre-commit, tenant audit) and a repo-layout tree so a first-time
  contributor knows where things live.
- Marketing landing at `/` (currently redirects to `/ui/login`) with the
  Q hero.
- 1200 × 630 OG image using the Q mark for social share.
- API reference via Scalar / Redoc with worked examples per endpoint.

## DX

- `mypy --strict` on new modules; ratchet coverage upward.
- ~~Pre-commit hooks: ruff, mypy, prettier for HTML/CSS.~~ **Partially done
  2026-09-03.** `.pre-commit-config.yaml` runs ruff (`--fix`), standard
  hygiene (trailing whitespace, EOF newline, YAML/TOML validity, large-file
  guard, merge-conflict marker), and the tenant-scope audit locally on
  every commit. `pre-commit` added to `[dev]` deps; devs run
  `pre-commit install` once per clone. License scan intentionally
  CI-only — `pip-licenses` scans the active interpreter and false-fails in
  dev venvs that hold unrelated projects. mypy + prettier deferred until
  we have a mypy config worth enforcing and the HTML templates settle.
  Landing this surfaced 24 pre-existing ruff violations that CI had been
  hitting on ruff 0.16 (E402/E501/E702/B007/B904/RUF003/RUF005/RUF059);
  fixed in the same commit so pre-commit doesn't false-fail on install.
- `scripts/dev-bootstrap.sh` — one command from clone to running.
- Contributing guide + GitHub issue templates.

## Later / speculative

- i18n for the admin UI itself (the product already supports localized
  template variants — the console does not).
- Self-hosted docker image release cadence + upgrade guide.
- Public status page (probably premature).
- Terms / Privacy / DPA templates for B2B onboarding.
