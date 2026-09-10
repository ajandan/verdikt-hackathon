"""Backend picker for the text-only Nemotron PDF field labeler.

The design-time editor's "AI Label fields" / "AI Realign" endpoints and the
inbox agent's PDF ingest branch all call `label_fields_in_pdf`, which needs
an NVIDIA Nemotron chat model hosted on Nebius Token Factory (or any
OpenAI-compatible endpoint). Living here — rather than in the UI module —
keeps the worker code path free of a UI import.
"""
from __future__ import annotations

from typing import Any

LABELER_UNCONFIGURED = (
    "PDF labeler not configured. Set DOCGEN_AI_PROVIDER=nebius and "
    "DOCGEN_AI_API_KEY in .env."
)


def pick_pdf_labeler_backend(settings: Any) -> dict[str, Any] | None:
    """Return kwargs for `label_fields_in_pdf`, or `None` if the AI provider
    isn't configured.

    Text-only: reuses `DOCGEN_AI_API_KEY` and `DOCGEN_AI_BASE_URL` (or the
    Nebius default) alongside `settings.pdf_labeler_model` — a Nemotron
    variant chosen for structured-JSON reasoning.
    """
    if settings.ai_provider != "nebius" or not settings.ai_api_key:
        return None
    return {
        "provider": "nebius",
        "api_key": settings.ai_api_key,
        "text_model": settings.pdf_labeler_model,
        "vision_model": getattr(settings, "pdf_vision_ocr_model", "") or "",
        "base_url": settings.ai_base_url or "https://api.studio.nebius.com/v1",
        "timeout": settings.ai_timeout_seconds,
        # Nemotron reasoning models default to a thinking preamble that
        # burns the JSON output budget on `<think>...</think>` and truncates
        # the actual labels dict. Silence it. vLLM-hosted models accept this
        # top-level knob; non-thinking models silently ignore it.
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
