"""Rate limiter: burst past renders_per_minute → 429."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def low_render_limit(monkeypatch) -> int:
    """Override the render limit to 2/min for the duration of the test."""
    monkeypatch.setenv("DOCGEN_RATE_LIMIT_RENDERS_PER_MINUTE", "2")
    # Clear the cached settings so the new value takes effect.
    from docgen.config import get_settings
    get_settings.cache_clear()
    yield 2
    get_settings.cache_clear()


def test_render_burst_hits_429(low_render_limit: int, auth_headers) -> None:
    # Import app AFTER settings cache is cleared.
    from docgen.main import app

    payload = {
        "template_id": "00000000-0000-0000-0000-0000000deadff",
        "data": {},
    }
    statuses: list[int] = []
    with TestClient(app) as client:
        for _ in range(low_render_limit + 2):
            r = client.post("/render", json=payload, headers=auth_headers)
            statuses.append(r.status_code)
    # At least one of the extra requests must be 429 (limit exceeded).
    assert 429 in statuses, f"expected a 429 in {statuses}"
    # The 429 response must carry Retry-After.
    with TestClient(app) as client:
        for _ in range(low_render_limit + 3):
            r = client.post("/render", json=payload, headers=auth_headers)
            if r.status_code == 429:
                assert "Retry-After" in r.headers
                assert "X-RateLimit-Limit" in r.headers
                assert "X-RateLimit-Remaining" in r.headers
                assert "X-RateLimit-Reset" in r.headers
                return
    # If we didn't return, something went wrong.
    raise AssertionError("Expected a 429 with Retry-After header")
