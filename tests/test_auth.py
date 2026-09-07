"""API-key auth: middleware behavior on REST routes."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.main import app
from docgen.store.db import sync_session
from docgen.store.models import ApiKey


def test_missing_authorization_header_returns_401() -> None:
    with TestClient(app) as client:
        r = client.post("/render", json={"template_id": "00000000-0000-0000-0000-000000000000", "data": {}})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_wrong_bearer_scheme_returns_401() -> None:
    with TestClient(app) as client:
        r = client.get("/jobs/00000000-0000-0000-0000-000000000000", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


def test_bogus_key_returns_401() -> None:
    with TestClient(app) as client:
        r = client.get(
            "/jobs/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": "Bearer dgk_nonexistent"},
        )
    assert r.status_code == 401


def test_valid_key_reaches_handler(auth_headers, default_tenant) -> None:
    # /jobs/{id} returns 404 (not 401) for a bogus job id when the key is valid.
    with TestClient(app) as client:
        r = client.get(
            "/jobs/00000000-0000-0000-0000-000000000042",
            headers=auth_headers,
        )
    assert r.status_code == 404


def test_health_bypasses_auth() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200


def test_revoked_key_returns_401(api_key) -> None:
    from docgen.auth import hash_api_key
    digest = hash_api_key(api_key)
    with sync_session() as s:
        res = s.execute(select(ApiKey).where(ApiKey.key_hash == digest))
        k = res.scalar_one()
        k.revoked_at = datetime.now(UTC)
        s.commit()

    with TestClient(app) as client:
        r = client.get(
            "/jobs/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    assert r.status_code == 401


def test_download_token_bypasses_bearer(default_tenant) -> None:
    """A valid ?token= for /jobs/<id>/result reaches the handler without
    a Bearer key. Bogus job_id → 404 from the handler, not 401 from auth."""
    import uuid as _uuid

    from docgen.auth import sign_download_token

    job_id = str(_uuid.uuid4())
    tok = sign_download_token(job_id, str(default_tenant))
    with TestClient(app) as client:
        r = client.get(f"/jobs/{job_id}/result?token={tok}")
    assert r.status_code == 404


def test_download_token_wrong_job_id_rejected(default_tenant) -> None:
    """Token was signed for job A but URL is job B → auth falls back to
    Bearer check and 401s (no header given)."""
    import uuid as _uuid

    from docgen.auth import sign_download_token

    signed_job = str(_uuid.uuid4())
    url_job = str(_uuid.uuid4())
    tok = sign_download_token(signed_job, str(default_tenant))
    with TestClient(app) as client:
        r = client.get(f"/jobs/{url_job}/result?token={tok}")
    assert r.status_code == 401


def test_download_token_garbage_rejected() -> None:
    with TestClient(app) as client:
        r = client.get("/jobs/00000000-0000-0000-0000-000000000042/result?token=garbage")
    assert r.status_code == 401


def test_bump_last_used_is_debounced() -> None:
    """A burst of requests with the same key writes last_used_at at most once
    per debounce window. The gate is pure and deterministic, so we can check
    it directly without spinning up the middleware."""
    from docgen.auth import (
        _LAST_USED_DEBOUNCE_SECONDS,
        _last_used_written_at,
        _should_bump_last_used,
    )

    key = "test-debounce-key"
    _last_used_written_at.pop(key, None)
    assert _should_bump_last_used(key) is True
    # Second call inside the window is suppressed.
    assert _should_bump_last_used(key) is False
    # Rewinding the recorded timestamp past the window re-opens it.
    _last_used_written_at[key] -= _LAST_USED_DEBOUNCE_SECONDS + 1
    assert _should_bump_last_used(key) is True
    _last_used_written_at.pop(key, None)
