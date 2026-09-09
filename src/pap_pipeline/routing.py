"""Per-call instrumentation for the routing table.

Wraps each model call in the pipeline and records:
  - model name (nv-ingest / nvidia/nemotron-3-super-120b-a12b / etc.)
  - task shape (extract | retrieve-embed | retrieve-rerank | reason)
  - tokens in / out
  - wall-clock latency
  - per-call cost (derived from published Nebius pricing at call time)

The persisted records are the data source for the /ui/admin/routing page
built in Phase 3. Storage TBD in Phase 2 — likely an append-only table via
the existing docgen.store SQLAlchemy engine.
"""
