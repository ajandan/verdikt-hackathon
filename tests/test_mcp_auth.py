"""MCP surface must enforce the same API-key auth as REST (§12 tenant isolation)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from docgen.main import app


def test_mcp_root_requires_auth() -> None:
    """Any request to /mcp/* without a Bearer key must be 401 — no info leak."""
    with TestClient(app) as client:
        # Streamable-http MCP: unauthenticated POST/GET should be rejected by the
        # ApiKeyAuthMiddleware before FastMCP ever sees it.
        r_post = client.post("/mcp/", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert r_post.status_code == 401
    assert r_post.json() == {"detail": "unauthorized"}


def test_mcp_invalid_key_rejected() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/mcp/",
            headers={"Authorization": "Bearer dgk_not_a_real_key"},
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
    assert r.status_code == 401
