"""Prometheus metrics — per-tenant counters + histograms + gauges.

Everything the ops dashboard needs to answer "who is this render load
coming from, how big is the queue, what's the P99 latency". Wired at the
seams that already produce structured log events (``render_succeeded``,
``render_failed``, ``preflight_rejected``), so this module is an
additional consumer of the same signal — logs stay the audit trail,
metrics stay the time series.

Cardinality choices
-------------------
Every counter carries a ``tenant_id`` label so an ops on-call can slice
"renders/hour" by tenant without joining another table. The upper bound
on cardinality is the tenant count (single-digit at demo scale, low
thousands at production scale) which stays well under Prometheus'
comfort threshold. Format labels (``docx``/``pdf``/``html``) and outcome
labels (``succeeded``/``failed``) are enumerated small sets. Anything
per-template or per-user would blow the cardinality budget, so those
stay in structured logs.

Registry model
--------------
Uses the default global registry so ``prometheus_client.generate_latest()``
picks up everything on ``/metrics`` without extra plumbing. In tests the
registry is process-global too, which means each test either resets its
own counter delta by reading .value before/after, or uses a fresh
CollectorRegistry via ``get_metrics_registry`` for isolation.
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Bucket boundaries in seconds. Small buckets clustered under 5s cover
# the DOCX/PDF fast path; the tail up to 60s covers the async render
# path that goes through the worker + blob-store round trip. Tuned once
# from the render_completed log's duration_ms distribution — revisit
# only if a legitimate slow path breaks the 60s cap.
_RENDER_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


renders_total = Counter(
    "docgen_renders_total",
    "Terminal render outcomes by tenant + format + state.",
    labelnames=("tenant_id", "format", "state"),
)

render_latency_seconds = Histogram(
    "docgen_render_latency_seconds",
    "Wall-clock seconds from render enqueue to terminal state.",
    labelnames=("tenant_id", "format", "state"),
    buckets=_RENDER_LATENCY_BUCKETS,
)

# Queue-depth gauges. Sampled by the /metrics scrape handler (see main.py)
# on each pull rather than incremented at enqueue/dequeue time so the
# view stays consistent with the DB even if a worker crash left a job
# stuck in "running". Alerts fire on the gauge, not on the counter.
render_queue_depth = Gauge(
    "docgen_render_queue_depth",
    "Render jobs sitting in queued / running state.",
    labelnames=("state",),
)

preflight_rejections_total = Counter(
    "docgen_preflight_rejections_total",
    "Design-time preflight rejections by tenant + reason code.",
    labelnames=("tenant_id", "reason"),
)


def observe_render(
    *, tenant_id: str | None, format: str, state: str, duration_seconds: float
) -> None:
    """Record one terminal render for the counter + latency histogram.

    Called from both the inline (/render + /ui/render) and async (worker
    actor) paths — every render terminates through one of these seams,
    and both call this helper so a switch between modes doesn't leak a
    metric. ``tenant_id`` is stringified because Prometheus labels are
    strings; None → "anonymous" so the label stays present rather than
    becoming a series-splitting empty value.
    """
    tid = str(tenant_id) if tenant_id else "anonymous"
    renders_total.labels(tenant_id=tid, format=format, state=state).inc()
    render_latency_seconds.labels(
        tenant_id=tid, format=format, state=state
    ).observe(duration_seconds)


def observe_preflight_rejection(*, tenant_id: str | None, reason: str) -> None:
    """Record one preflight rejection.

    Reason codes must come from the finite ``PdfFinding.code`` /
    ``DocxFinding.code`` enumerations — passing a free-form message here
    would blow the cardinality budget. Callers that don't have a code
    handy should pass a stable fallback like ``"other"``.
    """
    tid = str(tenant_id) if tenant_id else "anonymous"
    preflight_rejections_total.labels(tenant_id=tid, reason=reason).inc()


def sample_queue_depth_from_session(session) -> None:
    """Refresh the ``render_queue_depth`` gauges from the DB.

    Sync session for uniform use across the scrape endpoint (which runs
    in the request loop) and any future watchdog callers. Two labels for
    now — ``queued`` and ``running`` — since those are the states an
    on-call actually alerts on. ``failed`` and ``succeeded`` are
    terminal and their counts live in the counter, not the gauge.
    """
    from sqlalchemy import func, select

    from docgen.store.models import RenderJob

    for state in ("queued", "running"):
        n = session.execute(
            select(func.count()).select_from(RenderJob).where(RenderJob.state == state)
        ).scalar_one()
        render_queue_depth.labels(state=state).set(int(n or 0))


def render_metrics(registry: CollectorRegistry | None = None) -> tuple[bytes, str]:
    """Return the Prometheus exposition text + its content type.

    Split out from the HTTP handler so unit tests can call it without
    spinning up a FastAPI app. Defaults to the process-global registry
    so scrapes see every metric this module defined at import time."""
    body = generate_latest(registry or REGISTRY)
    return body, CONTENT_TYPE_LATEST


__all__ = [
    "CONTENT_TYPE_LATEST",
    "observe_preflight_rejection",
    "observe_render",
    "preflight_rejections_total",
    "render_latency_seconds",
    "render_metrics",
    "render_queue_depth",
    "renders_total",
    "sample_queue_depth_from_session",
]
