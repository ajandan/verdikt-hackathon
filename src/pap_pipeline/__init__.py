"""PAP intake pipeline — new during the 2026 submission window.

Composes three NVIDIA-family models into a decision pipeline:

    extract (nv-ingest) → retrieve (NeMo Retriever NIMs) → reason (Nemotron)

The pipeline consumes Quire (docgen.*) as a library; docgen.* never imports
from this package. Enforced by tests/test_boundaries.py.

Wire-up point: docgen.inbox.agent calls `orchestrator.run(msg)` when the
`use_pipeline_backend` config flag is set on the tenant. Otherwise the inbox
agent's existing behavior runs unchanged.
"""
