"""Webhook delivery: signing, body shape, actor plumbing."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid as _uuid

from docgen.webhooks import build_body, event_for_state, sign_body


def test_build_body_stable_shape() -> None:
    body = build_body(
        job_id="job-1",
        state="succeeded",
        result_bytes=123,
        error=None,
        output_format="docx",
        template_id="tpl-1",
        version=3,
    )
    obj = json.loads(body)
    assert obj == {
        "job_id": "job-1",
        "state": "succeeded",
        "result_bytes": 123,
        "error": None,
        "output_format": "docx",
        "template_id": "tpl-1",
        "version": 3,
    }
    assert body == json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_sign_body_matches_hmac() -> None:
    body = b'{"foo":"bar"}'
    sig = sign_body(body, secret="test-webhook-secret")
    expected = "sha256=" + hmac.new(
        b"test-webhook-secret", body, hashlib.sha256
    ).hexdigest()
    assert sig == expected


def test_event_for_state_mapping() -> None:
    assert event_for_state("succeeded") == "render.succeeded"
    assert event_for_state("failed") == "render.failed"
    assert event_for_state("queued") == "render.queued"


def test_deliver_webhook_actor_posts_with_signature(monkeypatch, default_tenant, sample_docx_bytes) -> None:
    """Full round-trip: create a job with callback_url, invoke actor, mock httpx."""
    from docgen.designtime.upload import upload_template
    from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
    from docgen.store.models import RenderJob

    async def _prep() -> _uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                _template, version, _report = await upload_template(
                    s, name="wh-target", docx_bytes=sample_docx_bytes, tenant_id=default_tenant
                )
                job = RenderJob(
                    tenant_id=default_tenant,
                    template_version_id=version.id,
                    state="succeeded",
                    payload={},
                    output_format="docx",
                    result_bytes=1024,
                    callback_url="https://example.invalid/hook",
                    webhook_state="pending",
                )
                s.add(job)
                await s.commit()
                await s.refresh(job)
                return job.id
        finally:
            await async_engine.dispose()

    job_id = asyncio.run(_prep())

    calls: list[dict] = []

    class _FakeResp:
        status_code = 200
        text = "ok"

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content, headers):
            calls.append({"url": url, "content": content, "headers": headers})
            return _FakeResp()

    import docgen.jobs.webhooks as wh
    monkeypatch.setattr(wh.httpx, "Client", _FakeClient)
    # This test focuses on signing/plumbing; SSRF URL validation is exercised
    # by dedicated tests. `example.invalid` is unresolvable, so bypass here.
    monkeypatch.setattr(wh, "validate_public_url", lambda _u: None)
    # In the container, DOCGEN_WEBHOOK_SIGNING_SECRET is set to a real value
    # before conftest can override it (env-var setdefault is a no-op then).
    # Pin the actor's view of the secret to what the test signs against.
    real_get_settings = wh.get_settings

    def _pinned_settings():
        s = real_get_settings()
        s.webhook_signing_secret = "test-webhook-secret"
        return s

    monkeypatch.setattr(wh, "get_settings", _pinned_settings)

    # Call the actor's underlying function directly (skip Dramatiq broker/queue).
    wh.deliver_webhook.fn(str(job_id))

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://example.invalid/hook"
    assert "X-DocGen-Signature" in call["headers"]
    assert call["headers"]["X-DocGen-Event"] == "render.succeeded"
    sig = call["headers"]["X-DocGen-Signature"]
    body = call["content"]
    expected = "sha256=" + hmac.new(
        b"test-webhook-secret", body, hashlib.sha256
    ).hexdigest()
    assert sig == expected

    with sync_session() as s:
        job = s.get(RenderJob, job_id)
        assert job.webhook_state == "delivered"
        assert job.webhook_attempts >= 1
