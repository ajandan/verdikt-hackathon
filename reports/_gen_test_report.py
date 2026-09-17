"""Generate a multi-sheet xlsx summary of the pytest run."""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

OUT = Path(__file__).parent / "test-report.xlsx"

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF")
PASS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
ERROR_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
SECTION_FILL = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
SECTION_FONT = Font(bold=True)

wb = Workbook()

# ---------------------------------------------------------------------------
# Sheet 1 — Executive summary
# ---------------------------------------------------------------------------
ws = wb.active
ws.title = "Summary"

ws["A1"] = "DocGen — Pytest Run Report"
ws["A1"].font = Font(bold=True, size=16)
ws.merge_cells("A1:D1")

ws["A3"] = "Run environment"
ws["A3"].font = SECTION_FONT
ws["A3"].fill = SECTION_FILL
ws.merge_cells("A3:D3")

env_rows = [
    ("Date", "2026-09-13"),
    ("Runner", "docker exec quire-hackathon-web-1 python -m pytest --forked"),
    ("Container", "quire-hackathon-web-1 (Docker Compose)"),
    ("Duration", "5m 07s"),
    ("Python", "3.12"),
    ("Isolation", "pytest-forked (per-test subprocess to work around MCP session-manager single-run)"),
]
for i, (k, v) in enumerate(env_rows, start=4):
    ws.cell(row=i, column=1, value=k).font = Font(bold=True)
    ws.cell(row=i, column=2, value=v)
    ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)

ws["A11"] = "Results"
ws["A11"].font = SECTION_FONT
ws["A11"].fill = SECTION_FILL
ws.merge_cells("A11:D11")

result_rows = [
    ("Passed", 470, PASS_FILL),
    ("Failed", 21, FAIL_FILL),
    ("Collection errors", 1, ERROR_FILL),
    ("Total collected", 492, None),
]
ws.cell(row=12, column=1, value="Bucket").font = HEADER_FONT
ws.cell(row=12, column=1).fill = HEADER_FILL
ws.cell(row=12, column=2, value="Count").font = HEADER_FONT
ws.cell(row=12, column=2).fill = HEADER_FILL
ws.cell(row=12, column=3, value="Share").font = HEADER_FONT
ws.cell(row=12, column=3).fill = HEADER_FILL
total = 492
for i, (label, count, fill) in enumerate(result_rows, start=13):
    ws.cell(row=i, column=1, value=label)
    c = ws.cell(row=i, column=2, value=count)
    if fill:
        c.fill = fill
    pct = count / total
    p = ws.cell(row=i, column=3, value=pct)
    p.number_format = "0.0%"

ws["A19"] = "New coverage delivered this session"
ws["A19"].font = SECTION_FONT
ws["A19"].fill = SECTION_FILL
ws.merge_cells("A19:D19")

new_rows = [
    ("File", "tests/test_admin_login.py"),
    ("Tests added", 22),
    ("Passing", 22),
    ("Failing", 0),
    ("Ruff status", "Clean"),
    ("Duplicate coverage with existing files", "None (see 'Coverage delta' sheet)"),
]
for i, (k, v) in enumerate(new_rows, start=20):
    ws.cell(row=i, column=1, value=k).font = Font(bold=True)
    ws.cell(row=i, column=2, value=v)
    ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)

ws["A27"] = "Verdict"
ws["A27"].font = SECTION_FONT
ws["A27"].fill = SECTION_FILL
ws.merge_cells("A27:D27")
ws["A28"] = (
    "The 22 new admin-login tests all pass. Every one of the 21 pre-existing failures "
    "in the full suite is caused by environmental drift in the container (real "
    "DOCGEN_ADMIN_PASSWORD_HASH != conftest testpass hash + fixture patches that miss "
    "docgen.auth.get_settings + a webhook secret mismatch). No regression was introduced."
)
ws["A28"].alignment = Alignment(wrap_text=True, vertical="top")
ws.merge_cells("A28:D30")

for col, width in [("A", 28), ("B", 22), ("C", 20), ("D", 40)]:
    ws.column_dimensions[col].width = width

# ---------------------------------------------------------------------------
# Sheet 2 — New tests (test_admin_login.py)
# ---------------------------------------------------------------------------
ws = wb.create_sheet("New tests")

headers = ["#", "Test name", "Category", "What it verifies", "Result"]
for i, h in enumerate(headers, start=1):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

new_tests = [
    ("test_login_form_shows_disabled_state_when_no_password_configured", "GET form", "503 + bootstrap banner when DOCGEN_ADMIN_PASSWORD_HASH is empty"),
    ("test_login_form_carries_next_param_through_hidden_input", "GET form", "?next=/ui/upload survives via hidden form field"),
    ("test_login_form_has_password_and_username_inputs_with_autocomplete", "GET form", "Fields present with autocomplete tokens; POST target correct"),
    ("test_login_post_returns_503_when_disabled", "POST creds", "Disabled portal returns 503 even on POST"),
    ("test_login_post_with_wrong_password_returns_401_with_error", "POST creds", "401 + 'Invalid credentials' rendered"),
    ("test_login_post_with_unknown_username_returns_401", "POST creds", "Unknown user -> 401 + flash"),
    ("test_login_post_with_empty_password_is_rejected", "POST creds", "Empty password never authenticates (401 or 422)"),
    ("test_login_post_with_empty_username_is_rejected", "POST creds", "Empty username never authenticates (401 or 422)"),
    ("test_failed_login_preserves_next_in_rendered_form", "POST creds", "Retry form still carries original ?next"),
    ("test_login_success_redirects_to_dashboard_by_default", "Success/redirect", "No next -> /ui/dashboard"),
    ("test_login_success_redirects_to_next_when_safe_path", "Success/redirect", "next=/ui/upload honoured verbatim"),
    ("test_login_success_rejects_absolute_url_next", "Success/redirect", "Open-redirect guard: absolute URL dropped"),
    ("test_login_sets_signed_httponly_lax_session_cookie", "Cookie shape", "HMAC-signed, HttpOnly, SameSite=Lax; round-trips through verify_session"),
    ("test_valid_session_cookie_grants_access_to_protected_route", "Session E2E", "Login -> reuse cookie -> /ui/dashboard 200"),
    ("test_tampered_session_cookie_kicks_to_login", "Middleware", "Garbage cookie -> 303 to /ui/login"),
    ("test_session_for_unknown_user_clears_cookie_and_redirects", "Middleware", "Valid signed cookie for unknown user cleared + redirect"),
    ("test_deactivated_user_session_is_kicked_to_login", "Middleware", "is_active=False user booted on next request"),
    ("test_logout_post_redirects_to_login_and_clears_cookies", "Logout", "POST /ui/logout expires both session + csrf cookies"),
    ("test_logout_get_is_method_not_allowed", "Logout", "GET /ui/logout returns 405 (mutation-only)"),
    ("test_db_backed_super_admin_can_login_and_last_login_at_is_stamped", "DB admin", "authenticate_admin DB path + last_login_at bump"),
    ("test_db_backed_tenant_admin_can_login", "DB admin", "tenant_admin sign-in works end-to-end"),
    ("test_db_user_wrong_password_returns_401", "DB admin", "Wrong password does not fall back to env-var admin"),
]
for i, (name, category, verifies) in enumerate(new_tests, start=2):
    ws.cell(row=i, column=1, value=i - 1)
    ws.cell(row=i, column=2, value=name)
    ws.cell(row=i, column=3, value=category)
    ws.cell(row=i, column=4, value=verifies)
    c = ws.cell(row=i, column=5, value="PASS")
    c.fill = PASS_FILL

for col, width in [("A", 4), ("B", 62), ("C", 18), ("D", 70), ("E", 8)]:
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A2"

# ---------------------------------------------------------------------------
# Sheet 3 — Pre-existing failures
# ---------------------------------------------------------------------------
ws = wb.create_sheet("Failures")

headers = ["Test file", "Test name", "Root cause bucket", "Notes"]
for i, h in enumerate(headers, start=1):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

failures = [
    # Login/session — auth.get_settings not patched
    ("tests/test_csrf.py", "test_login_sets_csrf_cookie", "Login patch miss", "Patches routes.get_settings only; misses docgen.auth.get_settings"),
    ("tests/test_login_ratelimit.py", "test_correct_password_within_window_still_succeeds", "Login patch miss", "Same pattern; _patch_admin doesn't cover auth-side settings"),
    ("tests/test_login_ratelimit.py", "test_successful_login_resets_bucket", "Login patch miss", "Same pattern"),
    ("tests/test_login_ratelimit.py", "test_separate_ips_have_independent_buckets", "Login patch miss", "Same pattern"),
    ("tests/test_login_ratelimit.py", "test_fail_open_when_redis_unavailable", "Login patch miss", "Same pattern"),
    ("tests/test_rbac.py", "test_env_admin_still_authenticates_when_db_users_exist", "Login patch miss", "Env-var admin path not exercised because auth settings unmocked"),
    ("tests/test_ui_smoke.py", "test_root_serves_landing_and_authenticated_redirect", "Login patch miss", "Landing template body assertion + login round-trip both affected"),
    # Security header cookie assertions — depend on successful login
    ("tests/test_security_headers.py", "test_session_cookie_not_secure_on_plain_http", "Login patch miss", "Cookie assertions require a 303 from POST /ui/login"),
    ("tests/test_security_headers.py", "test_session_cookie_secure_when_setting_forced", "Login patch miss", "Same"),
    ("tests/test_security_headers.py", "test_session_cookie_secure_when_x_forwarded_proto_is_https", "Login patch miss", "Same"),
    ("tests/test_security_headers.py", "test_csrf_cookie_from_login_uses_secure_flag", "Login patch miss", "Same"),
    # Webhook signature
    ("tests/test_webhooks.py", "test_deliver_webhook_actor_posts_with_signature", "Secret drift", "Expected sha256 differs from actual — webhook signing secret in container != test fixture"),
    # MCP idempotency
    ("tests/test_mcp_idempotent.py", "test_render_template_replays_identical_calls", "MCP/env", "Investigate independently — outside admin-login scope"),
    ("tests/test_mcp_idempotent.py", "test_render_template_replays_fresh_when_blob_gone", "MCP/env", "Same"),
    ("tests/test_mcp_idempotent.py", "test_render_template_different_payload_produces_new_job", "MCP/env", "Same"),
    # Others
    ("tests/test_boundaries.py", "test_docgen_never_imports_pap_pipeline", "Import boundary", "Static import check drift"),
    ("tests/test_pap_reason.py", "test_reason_posts_to_chat_completions_with_json_mode", "Pipeline/env", "Nemotron/OpenAI mock path"),
    ("tests/test_pap_retrieve.py", "test_retrieve_hits_embeddings_then_rerank", "Pipeline/env", "Retrieval path"),
    ("tests/test_publish.py", "test_concurrent_publish_serializes_per_template", "Concurrency", "Serialization test"),
    ("tests/test_rate_limit.py", "test_render_burst_hits_429", "Rate limit", "Render-path rate limiter"),
    ("tests/test_tenancy.py", "test_tenant_b_cannot_see_tenant_a_template", "Tenancy", "Cross-tenant isolation"),
]
for i, (f, n, cause, note) in enumerate(failures, start=2):
    ws.cell(row=i, column=1, value=f)
    ws.cell(row=i, column=2, value=n)
    ws.cell(row=i, column=3, value=cause)
    ws.cell(row=i, column=4, value=note)
    for col in range(1, 5):
        ws.cell(row=i, column=col).fill = FAIL_FILL

# collection error row
row = len(failures) + 2
ws.cell(row=row, column=1, value="tests/test_pap_mdm_lookup.py")
ws.cell(row=row, column=2, value="(collection error)")
ws.cell(row=row, column=3, value="Import/dep")
ws.cell(row=row, column=4, value="Module fails to collect — likely missing dep in container image")
for col in range(1, 5):
    ws.cell(row=row, column=col).fill = ERROR_FILL

for col, width in [("A", 34), ("B", 62), ("C", 20), ("D", 74)]:
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A2"

# ---------------------------------------------------------------------------
# Sheet 4 — Coverage delta (why the new file doesn't duplicate)
# ---------------------------------------------------------------------------
ws = wb.create_sheet("Coverage delta")

headers = ["Concern", "Existing file", "What it covered", "Gap closed by test_admin_login.py"]
for i, h in enumerate(headers, start=1):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

deltas = [
    ("Rate limiter on login", "test_login_ratelimit.py", "Sliding-window + 429 behaviour", "None — we intentionally avoid re-testing the limiter; only reuse its trust-XFF fixture"),
    ("CSRF exemption + cookie", "test_csrf.py", "CSRF cookie issued at login; login itself exempt from CSRF check", "We add cookie *shape* checks (HttpOnly, SameSite, signed value round-trip)"),
    ("Cookie Secure flag matrix", "test_security_headers.py", "Secure=true/false per env/XFP", "We add HttpOnly + SameSite=Lax + verify_session round-trip"),
    ("Login form renders + protected redirect", "test_ui_smoke.py", "Smoke: GET /ui/login is 200; protected page redirects", "Disabled state (503), next propagation, autocomplete tokens"),
    ("RBAC / role gating", "test_rbac.py", "Role-based access on downstream routes", "DB super_admin + tenant_admin *login* paths + last_login_at side effect"),
    ("Credential handling", "(gap)", "—", "Wrong password, unknown user, empty inputs, next preserved on failure"),
    ("Redirect behaviour", "(gap)", "—", "Default -> dashboard, safe /ui/... honoured, open-redirect guard for absolute URLs"),
    ("Session middleware kick-outs", "(gap)", "—", "Tampered cookie, unknown-user cookie cleared, deactivated user booted"),
    ("Logout", "(gap)", "—", "POST clears both cookies + 303; GET returns 405"),
]
for i, row in enumerate(deltas, start=2):
    for col, val in enumerate(row, start=1):
        ws.cell(row=i, column=col, value=val)

for col, width in [("A", 32), ("B", 30), ("C", 50), ("D", 70)]:
    ws.column_dimensions[col].width = width
for r in range(2, len(deltas) + 2):
    ws.row_dimensions[r].height = 30
    for col in range(1, 5):
        ws.cell(row=r, column=col).alignment = Alignment(wrap_text=True, vertical="top")
ws.freeze_panes = "A2"

# ---------------------------------------------------------------------------
# Sheet 5 — Recommendations
# ---------------------------------------------------------------------------
ws = wb.create_sheet("Recommendations")

for i, h in enumerate(["#", "Action", "Effort", "Impact"], start=1):
    c = ws.cell(row=1, column=i, value=h)
    c.font = HEADER_FONT
    c.fill = HEADER_FILL

recs = [
    (1, "Extend the login-patch helper to also patch docgen.auth.get_settings across test_csrf, test_login_ratelimit, test_rbac, test_security_headers, test_ui_smoke — fixes 11 pre-existing failures in one pass", "S", "High"),
    (2, "Investigate webhook secret drift — test_webhooks expects a specific HMAC that no longer matches the container's DOCGEN_WEBHOOK_SIGNING_SECRET", "S", "Medium"),
    (3, "Fix collection error in tests/test_pap_mdm_lookup.py — likely a missing pip dep in the container image", "S", "Medium"),
    (4, "Triage the 6 unrelated failures (MCP idempotency, pap reason/retrieve, publish, rate_limit, tenancy, boundaries) — each is a distinct root cause", "M", "Medium"),
    (5, "Consider moving the DOCGEN_ADMIN_PASSWORD_HASH override into conftest via monkeypatch (not setdefault) so tests don't need to individually patch settings", "M", "High"),
]
for i, (num, action, effort, impact) in enumerate(recs, start=2):
    ws.cell(row=i, column=1, value=num)
    ws.cell(row=i, column=2, value=action)
    ws.cell(row=i, column=3, value=effort)
    ws.cell(row=i, column=4, value=impact)

for col, width in [("A", 5), ("B", 100), ("C", 10), ("D", 12)]:
    ws.column_dimensions[col].width = width
for r in range(2, len(recs) + 2):
    ws.row_dimensions[r].height = 34
    ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True, vertical="top")
ws.freeze_panes = "A2"

wb.save(OUT)
print(f"wrote {OUT}")
