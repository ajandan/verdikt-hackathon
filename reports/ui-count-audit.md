# Verdikt Admin UI — Count/Badge Audit

- **Generated:** 2026-09-16 (clean-state pass)
- **HEAD:** `853a4886b3fd55c99ee25a8a570185486872347d`
- **Tenant kept:** Alderwood Specialty Pharmacy (`7b473f7c-78a1-4af6-9d85-809e1be8e485`)
- **Scope:** every visible count / badge / summary on 12 admin pages, compared to a direct psql query. Read-only pass — no POSTs, no code changes.
- **Login bypass:** `docker exec quire-hackathon-web-1 python -c "…sign_session('admin')"` → `docgen_session` cookie + static `docgen_csrf=t`. Worked on all 12 pages.
- **All pages returned HTTP 200. No 5xx blockers.**

## 1. Ground truth (single psql pass)

| Metric | SQL used | Value |
|---|---|---|
| `templates_total` | `SELECT count(*) FROM templates WHERE is_active = true` | 6 |
| `templates_frozen` | `SELECT count(*) FROM templates WHERE is_active = true AND current_version IS NOT NULL` | 4 |
| `templates_draft` (chip) | derived: `total − frozen` | 2 |
| `templates_inactive` | `SELECT count(*) FROM templates WHERE is_active = false` | 0 |
| `pending_drafts` | `_core_stats` pending-draft query (routes.py:562-591) | 0 |
| `warnings_count` | `_core_stats` warn query (routes.py:593-605) | 0 |
| `renders_24h_total` | `SELECT count(*) FROM render_jobs WHERE submitted_at >= NOW() - INTERVAL '24 hours'` | 0 |
| `render_jobs_all` | `SELECT count(*) FROM render_jobs` | 0 |
| `publish_events` | `SELECT count(*) FROM publish_events` | 4 |
| `programs` | `SELECT count(*) FROM programs` | 1 |
| `rules` | `SELECT count(*) FROM rules` | 10 |
| `tenants` | `SELECT count(*) FROM tenants` | 1 |
| `api_keys` | `SELECT count(*) FROM api_keys` | 1 |
| `static_documents` | `SELECT count(*) FROM static_documents` | 5 |
| `doc_packages` | `SELECT count(*) FROM doc_packages` | 3 |
| `doc_packages.items` per row | `SELECT jsonb_array_length(items) FROM doc_packages ORDER BY name` | 3 / 2 / 2 |
| `inbox_events` | `SELECT count(*) FROM inbox_events` | 0 |
| `humanloop_queue` | `SELECT count(*) FROM inbox_events WHERE review_state='pending_review' AND reply_message_id IS NULL` | 0 |
| `admin_users` | `SELECT count(*) FROM admin_users` | 1 |
| `rules.attached_programs` (per rule) | `SELECT r.rule_id, count(prl.rule_id) FROM rules r LEFT JOIN program_rule_links prl ON prl.rule_id=r.id GROUP BY r.rule_id` (matches route counts_stmt — no `is_active` filter) | CV-*: 1 each · OB-*: 0 each |

## 2. UI vs DB matrix

| # | Page | Metric | UI value | DB value | ✓/✗ | Notes |
|---|---|---|---|---|---|---|
| 1 | `/ui/` | Stat card — Templates total | **6** | 6 | ✓ | `stats.templates_total`, routes.py:544 |
| 1 | `/ui/` | Stat card — Frozen | **4** | 4 | ✓ | `stats.templates_frozen`, routes.py:545 |
| 1 | `/ui/` | Stat card — Draft | **2** | 2 | ✓ | derived `total − frozen`, routes.py:610 |
| 1 | `/ui/` | Stat card — Inactive | (hidden, 0) | 0 | ✓ | template hides when zero (index.html:272) |
| 1 | `/ui/` | Stat card — Pending drafts | **0** | 0 | ✓ | `stats.pending_drafts`, routes.py:591 |
| 1 | `/ui/` | Stat card — Renders 24h | **0** | 0 | ✓ | `stats.renders_24h_total`, routes.py:614 |
| 1 | `/ui/` | Stat card — Warnings | **0** | 0 | ✓ | `stats.warnings_count`, routes.py:605 |
| 1 | `/ui/` | Filter chip "All" | **6** | 6 | ✓ | `templates\|length`, index.html:325 |
| 1 | `/ui/` | Filter chip "Frozen" | **4** | 4 | ✓ | index.html:326 |
| 1 | `/ui/` | Filter chip "Draft" | **2** | 2 | ✓ | index.html:327 (see note A) |
| 1 | `/ui/` | Toolbar "N shown" | **6 shown** | 6 | ✓ | index.html:342 |
| 1 | `/ui/` | Pager summary | (no pager) | rows ≤ page size | ✓ | `_pagination.html:47` skips when `total_pages ≤ 1` |
| 1 | `/ui/` | Row "No frozen version" badges | 2 | 2 (templates w/ `current_version IS NULL`) | ✓ | see finding F-1 below (semantics, not miscount) |
| 2 | `/ui/dashboard` | Stat card — Templates total | **6** | 6 | ✓ | `data-live="templates_total"` |
| 2 | `/ui/dashboard` | Stat card — Pending drafts | **0** | 0 | ✓ | |
| 2 | `/ui/dashboard` | Stat card — Renders 24h | **0** | 0 | ✓ | |
| 2 | `/ui/dashboard` | Stat card — Warnings | **0** | 0 | ✓ | |
| 2 | `/ui/dashboard` | Attention rail | (not rendered) | 0+0+0=0 | ✓ | `_needs_attention == 0` (dashboard.html:291) |
| 2 | `/ui/dashboard` | Recent renders | 0 rows + "last 0" | 0 | ✓ | routes.py:648-665 |
| 2 | `/ui/dashboard` | Recent publishes | 4 rows + "last 4" | 4 | ✓ | routes.py:667-684 |
| 2 | `/ui/dashboard` | Failing jobs (7d) | 0 rows | 0 | ✓ | routes.py:687-704 |
| 2 | `/ui/dashboard` | Top templates (30d) | 0 rows | 0 | ✓ | routes.py:706-721 |
| 2 | `/ui/dashboard` | Sparkline | present | n/a (presence only) | ✓ | |
| 3 | `/ui/programs` | Badge "N programs" | **1 program** | 1 | ✓ | admin_programs.html:41 |
| 3 | `/ui/programs` | tbody row count | 1 | 1 | ✓ | |
| 3 | `/ui/programs` | Pager | (no pager) | rows ≤ page size | ✓ | |
| 4 | `/ui/rules` | Badge "N rules" | **10 rules** | 10 | ✓ | admin_rules.html:76 |
| 4 | `/ui/rules` | tbody row count | 10 | 10 | ✓ | |
| 4 | `/ui/rules` | Per-row attached_programs | CV-* = 1 each · OB-* = 0 each | same | ✓ | routes.py:6715-6719 (counts *all* links, no is_active filter — see finding F-2) |
| 4 | `/ui/rules` | Pager | (no pager) | rows ≤ page size | ✓ | |
| 5 | `/ui/admin/tenants` | Badge "N tenants" | **1 tenant** | 1 | ✓ | admin_tenants.html:100 |
| 5 | `/ui/admin/tenants` | tbody row count | 1 | 1 | ✓ | |
| 5 | `/ui/admin/tenants` | Pager | (no pager) | rows ≤ page size | ✓ | |
| 6 | `/ui/admin/tenants/{id}/keys` | "N total" | **1 total** | 1 | ✓ | admin_keys.html:165 |
| 6 | same | tbody row count | 1 | 1 | ✓ | |
| 6 | same | Pager | (no pager) | rows ≤ page size | ✓ | |
| 7 | `/ui/static-documents` | Badge "N documents" | **5 documents** | 5 | ✓ | admin_static_documents.html:31 |
| 7 | same | tbody row count | 5 | 5 | ✓ | |
| 7 | same | Pager | (no pager) | rows ≤ page size | ✓ | |
| 8 | `/ui/packages` | Badge "N packages" | **3 packages** | 3 | ✓ | admin_doc_packages.html:34 |
| 8 | same | tbody row count | 3 | 3 | ✓ | |
| 8 | same | item_count badges | 3, 2, 2 | 3, 2, 2 (from `jsonb_array_length`) | ✓ | routes_doc_packages.py:262 |
| 8 | same | Pager | (no pager) | rows ≤ page size | ✓ | |
| 9 | `/ui/inbox` | Row count | 0 (empty state "No emails") | 0 | ✓ | |
| 9 | `/ui/inbox` | Status filter chips | present, no counts | n/a (no per-chip counters in template) | ✓ | inbox_list.html:369-372 |
| 9 | `/ui/inbox` | Review filter chips | present, no counts | n/a | ✓ | inbox_list.html:378-381 |
| 9 | `/ui/inbox` | Pager | (no pager) | 0 rows | ✓ | |
| 10 | `/ui/inbox/humanloop` | Sidebar "HumanLoop" badge | (not rendered) | 0 | ✓ | `humanloop_count == 0` → badge omitted (routes.py:243-254) |
| 10 | `/ui/inbox/humanloop` | Page "N waiting" | (empty state — "Nothing is waiting for review") | 0 | ✓ | inbox_humanloop.html:104-106 hidden when empty |
| 10 | `/ui/inbox/humanloop` | tbody row count | 0 | 0 | ✓ | |
| 11 | `/ui/renders` | Row count | 0 | 0 | ✓ | |
| 11 | `/ui/renders` | Status filter chips | present, no counts | n/a (chips have no counters — see finding F-3) | — | renders.html:132-142 |
| 11 | `/ui/renders` | Pager | (no pager) | 0 rows | ✓ | |
| 12 | `/ui/admin/users` | Badge "N users" | **1 user** | 1 | ✓ | admin_users.html:57 |
| 12 | `/ui/admin/users` | tbody row count | 1 | 1 | ✓ | |
| 12 | `/ui/admin/users` | Pager | (no pager) | rows ≤ page size | ✓ | |

**Result: 0 count discrepancies.** Every visible number matches its DB source of truth.

## 3. Findings

No count mismatches. The three notes below are worth flagging separately because they surfaced during the audit and could confuse an operator, but they are **not** UI-vs-DB miscounts.

### F-1 — Underlying data inconsistency: two templates carry frozen versions but `templates.current_version IS NULL` (informational, not a UI bug)

- **Where:** `/ui/` templates list, and it propagates to the "Frozen (4)" / "Draft (2)" stat card + chip counts.
- **DB state:**
  ```
  alderwood-pap-approval-ASP-201  current_version = NULL   template_versions: v1 is_frozen=t status=frozen
  alderwood-pap-denial-ASP-202    current_version = NULL   template_versions: v1 is_frozen=t status=frozen
  ```
- **UI shows:** rows render the "No frozen version" badge (index.html:385) and the stat/chip aggregates count them as draft. The UI is **faithful** to what `current_version` says.
- **Why noteworthy:** these rows have a frozen v1 in `template_versions` but the parent row wasn't updated to point at it, so operators see "Draft" on a row that actually has a frozen version. This is a DB integrity issue (not a route-count bug) — the purge left `current_version` dangling.
- **DB proof:**
  ```sql
  SELECT t.name, t.current_version, tv.version, tv.is_frozen, tv.status
  FROM templates t JOIN template_versions tv ON tv.template_id = t.id
  WHERE t.current_version IS NULL;
  ```

### F-2 — `/ui/rules` "attached programs" counts every link, including `is_active=false` (informational)

- **Where:** each row of `/ui/rules` shows the count from `routes.py:6715-6719`:
  ```python
  counts_stmt = (
      select(ProgramRuleLink.rule_id, func.count(ProgramRuleLink.id))
      .group_by(ProgramRuleLink.rule_id)
  )
  ```
  No `is_active` filter.
- **Current DB effect:** for rule `CV-HIS-01`, `CV-HIS-02`, `CV-INC-01`, `CV-RES-01` there is a link with `is_active=false` — the UI still shows "1" but the operator's mental model ("how many active programs will an edit ripple to") sees 0. In the clean state, this affects 4 of 6 CV rules.
- **Screenshot:** `reports/ui-audit-shots/04-rules.png`
- Not a mismatch against the current source — every displayed digit matches what the query computes — but flagging so the reader knows the metric semantics.

### F-3 — `/ui/renders` and `/ui/inbox` status/state chips have no counters

- Renders and inbox chips render as `<a class="chip">…</a>` with no `<span class="count">` (renders.html:132-142; inbox_list.html:369-381), unlike the templates chip group (index.html:324-327).
- Nothing to verify — just calling out the asymmetry versus other list pages so future audits don't look for counts that were never emitted.

## 4. Praise (matched perfectly)

- `/ui/` — 4 stat cards, 3 chip counts, "N shown", and 6 row-level frozen/draft badges all agree with DB.
- `/ui/dashboard` — every stat card, "last N" subtitle on recent-publishes card, and every empty-state exactly matches an empty table.
- `/ui/programs`, `/ui/rules`, `/ui/admin/tenants`, `/ui/admin/tenants/{id}/keys`, `/ui/static-documents`, `/ui/packages`, `/ui/admin/users` — badges and tbody row counts all equal their `SELECT count(*)`.
- `/ui/packages` — per-row `item_count` badges (3, 2, 2) match `jsonb_array_length(items)` for each package.
- `/ui/inbox` and `/ui/inbox/humanloop` — empty states render correctly; sidebar HumanLoop badge correctly hidden at 0.
- `/ui/renders` — empty list, no zombie counts.

## Screenshots

11 in `reports/ui-audit-shots/` (the templates-list capture was withdrawn because it displayed a personal owner address):

| # | File |
|---|---|
| 02 | `ui-audit-shots/02-dashboard.png` |
| 03 | `ui-audit-shots/03-programs.png` |
| 04 | `ui-audit-shots/04-rules.png` |
| 05 | `ui-audit-shots/05-admin-tenants.png` |
| 06 | `ui-audit-shots/06-admin-keys.png` |
| 07 | `ui-audit-shots/07-static-documents.png` |
| 08 | `ui-audit-shots/08-packages.png` |
| 09 | `ui-audit-shots/09-inbox.png` |
| 10 | `ui-audit-shots/10-inbox-humanloop.png` |
| 11 | `ui-audit-shots/11-renders.png` |
| 12 | `ui-audit-shots/12-admin-users.png` |
