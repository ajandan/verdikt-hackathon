"""Stage 2 — rule retrieval.

Two-step retrieval over the eligibility rule corpus:

  1. **Embed** the intake as a query and every candidate rule with an
     NVIDIA embedding NIM served through Nebius Token Factory. Rank by
     cosine similarity, keep the top-N candidates.
  2. **Rerank** the surviving candidates against the query with an NVIDIA
     rerank NIM. The rerank score is preserved through to the Decision so
     the reply letter can order citations by confidence.

The corpus itself is loaded from `src/pap_pipeline/rules/*.yaml` via
`pap_pipeline.rules.load_rules(program)`. A Phase-3 pgvector rewrite will
back this with a persistent store; at demo scale a per-call embed of all
rules is fine and keeps the stage independently testable — pass in a fresh
rule list, get back the reranked top-k.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from pap_pipeline.rules import load_rules
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.schemas.rule import Rule


@dataclass(frozen=True)
class RetrievedRule:
    """A rule that survived retrieval, with the reranker's relevance score.

    The score flows through Stage 3 into `Citation.score` unchanged so the
    reply letter can order citations by confidence.
    """

    rule: Rule
    score: float


@dataclass(frozen=True)
class RetrieveClientConfig:
    """Client config for the Nebius Token Factory embed + rerank endpoints.

    Split into embed_model / rerank_model because the two NIMs are separate
    on Nebius Token Factory — one endpoint, two model ids. Kept explicit so
    Stage 2 stays independently testable.
    """

    base_url: str
    api_key: str
    embed_model: str = "nvidia/nv-embedqa-e5-v5"
    # None disables the rerank step and uses embed cosine as the final score.
    # Set for deployments (e.g. Nebius Token Factory) that don't serve a
    # reranker — the embed cosine ordering is a strict downgrade but keeps
    # the pipeline running end-to-end.
    rerank_model: str | None = "nvidia/nv-rerankqa-mistral-4b-v3"
    timeout: float = 60.0


async def retrieve(
    intake: Intake,
    *,
    config: RetrieveClientConfig,
    program: str | None = None,
    rules: list[Rule] | None = None,
    k: int = 4,
    embed_top_n: int | None = None,
) -> list[RetrievedRule]:
    """Retrieve the top-k eligibility rules relevant to this intake.

    Parameters
    ----------
    intake
        The extracted intake object — Stage 2 builds a query string from it.
    program
        Namespace filter. When set, only rules matching this program are
        considered. When None (and `rules` is None), every rule under
        `pap_pipeline/rules/` is considered.
    rules
        Explicit rule corpus. Overrides the YAML loader — used by tests and
        by the orchestrator when the rule set has already been staged.
    k
        Number of reranked rules to return.
    embed_top_n
        First-stage cut. When None, defaults to `max(k * 3, len(corpus))` so
        the demo scale (four rules) always sees every rule in the reranker.
    """
    corpus = rules if rules is not None else load_rules(program)
    if not corpus:
        return []

    query = _build_query(intake)
    n = embed_top_n if embed_top_n is not None else max(k * 3, len(corpus))

    async with httpx.AsyncClient(timeout=config.timeout) as client:
        scored = await _embed_and_rank(client, config, query, corpus, top_n=n)
        if config.rerank_model is None:
            # No reranker available — fall back to embed cosine as the score.
            return [RetrievedRule(rule=r, score=s) for r, s in scored[:k]]
        candidates = [r for r, _ in scored]
        return await _rerank(client, config, query, candidates, k=k)


def _build_query(intake: Intake) -> str:
    """Serialize the intake into a natural-language query for the retriever.

    NIM embedders are trained on prose, not JSON. The retriever's job here
    is to surface rules that talk about the *shape* of the applicant — their
    income, insurance, residency, and prescription — so the query names
    those dimensions in the same vocabulary the rules use.
    """
    parts: list[str] = ["Patient assistance program eligibility inquiry."]
    if intake.household_size is not None or intake.annual_household_income is not None:
        parts.append(
            "Financial context: household of "
            f"{intake.household_size or 'unknown'}, annual income "
            f"{intake.annual_household_income or 'unknown'} USD."
        )
    if intake.insurance_status is not None:
        parts.append(
            f"Insurance status: {intake.insurance_status}"
            + (
                f", denial letter on file: {intake.has_denial_letter}."
                if intake.has_denial_letter is not None
                else "."
            )
        )
    if intake.country_of_residence is not None or intake.mailing_state is not None:
        parts.append(
            "Residency: "
            f"{intake.country_of_residence or 'unknown country'}, "
            f"{intake.mailing_state or 'unknown state'}."
        )
    if intake.prescribed_drug is not None or intake.prescriber_npi is not None:
        parts.append(
            f"Clinical: prescribed {intake.prescribed_drug or 'unknown drug'} "
            f"by NPI {intake.prescriber_npi or 'unknown'} on "
            f"{intake.prescription_date or 'unknown date'}."
        )
    return " ".join(parts)


async def _embed_and_rank(
    client: httpx.AsyncClient,
    config: RetrieveClientConfig,
    query: str,
    corpus: list[Rule],
    *,
    top_n: int,
) -> list[tuple[Rule, float]]:
    """First-stage retrieval: embed query + corpus, return top_n (rule, cosine)."""
    texts = [query] + [r.text.strip() for r in corpus]
    resp = await client.post(
        f"{config.base_url.rstrip('/')}/embeddings",
        headers=_headers(config),
        json={"model": config.embed_model, "input": texts},
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        vectors = [item["embedding"] for item in data["data"]]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"Unexpected embedder response shape: {data}") from e
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"Embedder returned {len(vectors)} vectors for {len(texts)} inputs."
        )

    q_vec = vectors[0]
    scored = [
        (rule, _cosine(q_vec, rule_vec))
        for rule, rule_vec in zip(corpus, vectors[1:])
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]


async def _rerank(
    client: httpx.AsyncClient,
    config: RetrieveClientConfig,
    query: str,
    candidates: list[Rule],
    *,
    k: int,
) -> list[RetrievedRule]:
    """Second-stage rerank via NVIDIA rerank NIM (Cohere-compat wire shape).

    Cohere-compat body: {model, query, documents: [str]}. Nebius Token
    Factory serves the NVIDIA rerank NIM under this shape. Response carries
    `results: [{index, relevance_score}]` — one entry per document, in
    descending relevance order.
    """
    if not candidates:
        return []

    resp = await client.post(
        f"{config.base_url.rstrip('/')}/rerank",
        headers=_headers(config),
        json={
            "model": config.rerank_model,
            "query": query,
            "documents": [r.text.strip() for r in candidates],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        results = data["results"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"Unexpected reranker response shape: {data}") from e

    reranked: list[RetrievedRule] = []
    for item in results[:k]:
        idx = item["index"]
        score = float(item["relevance_score"])
        reranked.append(RetrievedRule(rule=candidates[idx], score=score))
    return reranked


def _headers(config: RetrieveClientConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


__all__ = [
    "RetrievedRule",
    "RetrieveClientConfig",
    "retrieve",
]
