"""Stage 2 (retrieve) — embed + rerank wire contract.

The retrieval stage feeds Stage 3 (reason) with the rules the reasoner
should cite. If the embedder→reranker→top-k plumbing regresses, the
reasoner still emits a Decision but from a stale or incorrect rule slate —
that's a silent quality failure the video would ship unnoticed. These tests
lock the wire shape and the ordering guarantee.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pap_pipeline.demo_data import load_canonical_intake
from pap_pipeline.rules import load_rules
from pap_pipeline.stages import retrieve as retrieve_mod
from pap_pipeline.stages.retrieve import (
    RetrieveClientConfig,
    RetrievedRule,
    retrieve,
)


def _config() -> RetrieveClientConfig:
    return RetrieveClientConfig(
        base_url="https://api.studio.nebius.com/v1",
        api_key="nb-secret",
    )


def _patch_async_client(monkeypatch, handler) -> list[dict[str, Any]]:
    """Route AsyncClient traffic through a MockTransport, record each hit."""
    calls: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append({
            "url": str(request.url),
            "body": json.loads(request.content),
        })
        return handler(request)

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(retrieve_mod.httpx, "AsyncClient", _factory)
    return calls


def _embed_response(n_vectors: int, dim: int = 4) -> httpx.Response:
    """Fabricate an embeddings response with unit vectors along successive axes.

    Deterministic distance ordering: vector i is 1.0 at position i, 0 elsewhere,
    so cosine(v_i, v_j) is 0 for i != j and 1 for i == j.
    """
    vectors = []
    for i in range(n_vectors):
        v = [0.0] * dim
        v[i % dim] = 1.0
        vectors.append(v)
    return httpx.Response(
        200,
        json={"data": [{"embedding": v} for v in vectors]},
    )


def _rerank_response(n: int) -> httpx.Response:
    """Rerank response — assigns descending scores so index 0 wins."""
    results = [
        {"index": i, "relevance_score": 1.0 - (i * 0.1)}
        for i in range(n)
    ]
    return httpx.Response(200, json={"results": results})


@pytest.mark.asyncio
async def test_retrieve_hits_embeddings_then_rerank(monkeypatch) -> None:
    """Two calls, one to /embeddings, one to /rerank — in that order — with
    the right models on each. This is the compliance surface."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            return _embed_response(len(body["input"]))
        if request.url.path.endswith("/rerank"):
            body = json.loads(request.content)
            return _rerank_response(len(body["documents"]))
        return httpx.Response(404)

    calls = _patch_async_client(monkeypatch, _handler)

    intake = load_canonical_intake()
    reranked = await retrieve(intake, config=_config(), k=4)

    assert len(reranked) == 4
    assert all(isinstance(r, RetrievedRule) for r in reranked)
    # Order preserved from reranker (descending relevance_score).
    scores = [r.score for r in reranked]
    assert scores == sorted(scores, reverse=True)

    assert len(calls) == 2
    embed_call, rerank_call = calls
    assert embed_call["url"].endswith("/embeddings")
    assert embed_call["body"]["model"] == "nvidia/nv-embedqa-e5-v5"
    # Query + one text per rule; corpus is the four Cardiovita rules.
    assert len(embed_call["body"]["input"]) == 1 + 4

    assert rerank_call["url"].endswith("/rerank")
    assert rerank_call["body"]["model"] == "nvidia/nv-rerankqa-mistral-4b-v3"
    assert rerank_call["body"]["query"] == embed_call["body"]["input"][0]
    assert len(rerank_call["body"]["documents"]) <= 4


@pytest.mark.asyncio
async def test_retrieve_query_carries_all_intake_dimensions(monkeypatch) -> None:
    """The retriever query must name the four rule dimensions (financial,
    insurance, residency, clinical) so the embedder can surface each rule.
    If the query drops a dimension, that dimension's rule ranks last."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            return _embed_response(len(body["input"]))
        return _rerank_response(4)

    calls = _patch_async_client(monkeypatch, _handler)

    intake = load_canonical_intake()
    await retrieve(intake, config=_config(), k=4)

    query = calls[0]["body"]["input"][0]
    for token in ("household", "Insurance", "Residency", "prescribed"):
        assert token in query, f"query missing dimension {token!r}: {query!r}"


@pytest.mark.asyncio
async def test_retrieve_respects_program_namespace(monkeypatch) -> None:
    """Passing a program filter narrows the corpus before retrieval — the
    embed call must see exactly the rules for that program plus the query."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            return _embed_response(len(body["input"]))
        return _rerank_response(4)

    calls = _patch_async_client(monkeypatch, _handler)

    intake = load_canonical_intake()
    await retrieve(intake, config=_config(), program="cardiovita-pap-2026", k=4)

    # Query + four Cardiovita rules.
    assert len(calls[0]["body"]["input"]) == 5

    calls_b = _patch_async_client(monkeypatch, _handler)
    await retrieve(intake, config=_config(), program="does-not-exist", k=4)
    # No rules → no calls at all; retrieve short-circuits.
    assert calls_b == []


@pytest.mark.asyncio
async def test_retrieve_uses_explicit_rules_when_provided(monkeypatch) -> None:
    """When a caller passes a `rules=` list, retrieve must use it instead of
    reading YAML — Stage 2 must be exerciseable without the filesystem."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            return _embed_response(len(body["input"]))
        return _rerank_response(2)

    calls = _patch_async_client(monkeypatch, _handler)

    intake = load_canonical_intake()
    two_rules = load_rules("cardiovita-pap-2026")[:2]
    reranked = await retrieve(intake, config=_config(), rules=two_rules, k=2)

    assert len(reranked) == 2
    assert len(calls[0]["body"]["input"]) == 3  # query + 2 rules


@pytest.mark.asyncio
async def test_retrieve_skips_rerank_when_disabled(monkeypatch) -> None:
    """rerank_model=None → embed-only path. Cosine scores flow through as
    citation scores; no /rerank call is made. This is the path used by
    deployments (like Nebius Token Factory) without a hosted reranker."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            return _embed_response(len(body["input"]))
        if request.url.path.endswith("/rerank"):
            # Reaching rerank when disabled would be a regression.
            raise AssertionError("rerank called but rerank_model is None")
        return httpx.Response(404)

    calls = _patch_async_client(monkeypatch, _handler)

    config = RetrieveClientConfig(
        base_url="https://api.studio.nebius.com/v1",
        api_key="nb-secret",
        rerank_model=None,
    )
    reranked = await retrieve(load_canonical_intake(), config=config, k=4)

    assert len(reranked) == 4
    assert all(isinstance(r, RetrievedRule) for r in reranked)
    # Scores are cosines, so bounded in [-1, 1] and sorted descending.
    scores = [r.score for r in reranked]
    assert scores == sorted(scores, reverse=True)
    # Exactly one call: /embeddings. No /rerank.
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/embeddings")


@pytest.mark.asyncio
async def test_retrieve_raises_on_embedder_shape_mismatch(monkeypatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": []})
        return _rerank_response(0)

    _patch_async_client(monkeypatch, _handler)

    with pytest.raises(RuntimeError, match="Embedder returned 0 vectors"):
        await retrieve(load_canonical_intake(), config=_config(), k=4)
