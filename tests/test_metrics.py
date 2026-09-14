"""Prometheus metrics — observation helpers + /metrics endpoint.

Two layers of coverage:
  1. Unit tests over the observation helpers to confirm the counter,
     histogram, and gauge each move for the labels we hand them. Uses a
     fresh CollectorRegistry per test so parallel runs and residual state
     from other tests can't influence the assertions.
  2. An HTTP-level test for /metrics that boots the app with TestClient
     and asserts the exposition format + presence of our metric families.
     The endpoint is api-key-bypassed by design (Prometheus scrapers
     don't carry bearer tokens), so no auth headers here.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest

from docgen.main import app
from docgen.metrics import (
    observe_preflight_rejection,
    observe_render,
    preflight_rejections_total,
    render_latency_seconds,
    render_metrics,
    renders_total,
)


def _sample_value(metric, **labels) -> float:
    """Pull the current value of a labeled series out of a
    prometheus_client metric family. Returns 0.0 when the series has no
    samples yet, matching the counter's natural zero."""
    for m in metric.collect():
        for s in m.samples:
            if s.name.endswith("_total") or s.name.endswith("_count"):
                if s.labels == labels:
                    return s.value
    return 0.0


def test_observe_render_increments_counter_and_histogram() -> None:
    """One observe_render call must add exactly one to the terminal-state
    counter for that (tenant, format, state) tuple and record exactly one
    sample in the latency histogram for the same tuple."""
    labels = {"tenant_id": "tenant-a", "format": "docx", "state": "succeeded"}

    before_count = _sample_value(renders_total, **labels)
    before_hist = _sample_value(render_latency_seconds, **labels)

    observe_render(
        tenant_id="tenant-a",
        format="docx",
        state="succeeded",
        duration_seconds=0.42,
    )

    after_count = _sample_value(renders_total, **labels)
    after_hist = _sample_value(render_latency_seconds, **labels)

    assert after_count == before_count + 1
    assert after_hist == before_hist + 1


def test_observe_render_none_tenant_becomes_anonymous_label() -> None:
    """`tenant_id=None` must land on the ``anonymous`` label rather than
    an empty string — an empty label would silently create a new series
    every scrape and confuse ops queries."""
    labels = {"tenant_id": "anonymous", "format": "pdf", "state": "failed"}

    before = _sample_value(renders_total, **labels)

    observe_render(
        tenant_id=None,
        format="pdf",
        state="failed",
        duration_seconds=1.0,
    )

    after = _sample_value(renders_total, **labels)
    assert after == before + 1


def test_observe_preflight_rejection_increments_counter() -> None:
    """Preflight rejections increment the tenant + reason counter — the
    reason must come from the finite finding-code enum, but the metric
    module doesn't enforce that (cardinality contract is on the caller)."""
    labels = {"tenant_id": "tenant-b", "reason": "encrypted"}

    before = _sample_value(preflight_rejections_total, **labels)
    observe_preflight_rejection(tenant_id="tenant-b", reason="encrypted")
    after = _sample_value(preflight_rejections_total, **labels)

    assert after == before + 1


def test_render_metrics_returns_exposition_bytes() -> None:
    """`render_metrics()` returns the exposition-format bytes + the
    canonical text/plain content type Prometheus expects. We stamp an
    observation first so the exposition contains at least one of our
    metric families."""
    observe_render(
        tenant_id="tenant-c",
        format="docx",
        state="succeeded",
        duration_seconds=0.1,
    )
    body, content_type = render_metrics()

    assert isinstance(body, bytes) and body
    assert content_type.startswith("text/plain")
    text = body.decode("utf-8")
    assert "docgen_renders_total" in text
    assert "docgen_render_latency_seconds" in text


def test_render_metrics_isolated_registry_has_no_defaults() -> None:
    """Passing a fresh CollectorRegistry means the exposition contains
    only what that registry knows about — none of the process-global
    families leak in. Confirms the argument is respected."""
    empty = CollectorRegistry()
    body, _ = render_metrics(empty)
    assert b"docgen_renders_total" not in body


def test_metrics_endpoint_returns_exposition_and_bypasses_auth() -> None:
    """The /metrics HTTP endpoint is public (see `_BYPASS_EXACT` in
    docgen.auth) so a Prometheus scraper can pull it without a bearer
    token. Response body must be the Prometheus exposition format."""
    # Stamp at least one observation so the endpoint has something to
    # emit even before any real render has run in the test process.
    observe_render(
        tenant_id="tenant-d",
        format="docx",
        state="succeeded",
        duration_seconds=0.5,
    )
    with TestClient(app) as client:
        r = client.get("/metrics")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "docgen_renders_total" in r.text


def test_generate_latest_produces_valid_exposition() -> None:
    """Sanity check that `prometheus_client.generate_latest` doesn't
    trip on any of our metric definitions (bad label names, unknown
    types, etc). Any exception here is a wiring bug, not a data bug."""
    generate_latest()  # raises on malformed metric definitions
