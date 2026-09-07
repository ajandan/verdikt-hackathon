"""Nebius Token Factory adapter — NVIDIA Nemotron on Nebius AI Cloud.

Nebius Token Factory speaks the OpenAI chat-completions dialect, so the wire
protocol is identical to :class:`OpenAICompatProvider`. This subclass exists to
(a) preload the Nebius Token Factory base URL when the operator hasn't set one,
and (b) make the hackathon compliance surface explicit in the codebase: the
design-time LLM ships against an NVIDIA open-source model (Nemotron family)
hosted on Nebius.

Design-time only. Never called from the render path — the render pipeline
remains a deterministic Jinja merge with no model in the loop.
"""
from __future__ import annotations

from docgen.providers.openai_compat import OpenAICompatProvider

DEFAULT_BASE_URL = "https://api.studio.nebius.com/v1"
# NVIDIA Nemotron 3 Nano — MoE with ~3B active parameters, 262K context.
# Best price/latency of the four Nemotron variants Nebius currently serves via
# public (serverless) inference. Verified end-to-end for JSON-mode field
# extraction from template text. Override via DOCGEN_AI_MODEL if you want to
# A/B a larger Nemotron variant (e.g. nvidia/nemotron-3-super-120b-a12b).
DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


class NebiusProvider(OpenAICompatProvider):
    name = "nebius"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            base_url=base_url or DEFAULT_BASE_URL,
            api_key=api_key,
            model=model or DEFAULT_MODEL,
            timeout=timeout,
        )
