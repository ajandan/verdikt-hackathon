"""Shared vision-backend selection for PDF AI detect/label paths.

Both the UI's `/placements/ai-detect` and `/placements/ai-draw` endpoints
and the inbox agent's PDF ingest branch need the same "pick Nebius or
Mistral, or give up" logic. Living here avoids importing the UI module
from the worker code path.
"""
from __future__ import annotations

from typing import Any

PDF_VISION_UNCONFIGURED = (
    "Vision provider not configured. Set DOCGEN_AI_PROVIDER=nebius plus "
    "DOCGEN_AI_API_KEY (recommended) or DOCGEN_MISTRAL_API_KEY in .env."
)


def pick_pdf_vision_backend(settings: Any) -> dict[str, Any] | None:
    """Pick the vision backend for the AI PDF endpoints, or None if neither
    is configured.

    Preference order:
      1. Nebius Token Factory (reuses `DOCGEN_AI_API_KEY`). Only wins when
         `ai_provider == "nebius"` AND the key is set — otherwise a Nebius
         key configured for text-only Nemotron use wouldn't accidentally be
         reused against a vision model that isn't on the account.
      2. Legacy Mistral (`DOCGEN_MISTRAL_API_KEY`). Preserved so existing
         deploys keep working.

    Returns a dict with the kwargs `detect_fields_in_pdf` / `label_fields_in_pdf`
    expect, plus a `provider` tag for log lines. `None` means the caller
    should surface the standard "not configured" error.
    """
    if settings.ai_provider == "nebius" and settings.ai_api_key:
        return {
            "provider": "nebius",
            "api_key": settings.ai_api_key,
            "chat_model": settings.nebius_vision_model,
            "base_url": settings.nebius_vision_base_url,
            "timeout": settings.nebius_vision_timeout_seconds,
            "ocr_via_vision_chat": True,
            "ocr_concurrency": settings.nebius_vision_ocr_concurrency,
            # Kept only so the label kwargs dict is uniform; ignored when
            # ocr_via_vision_chat is True.
            "ocr_model": "",
            # MiniCPM-V-4.5 (and other open-source VLMs served by vLLM on
            # Nebius) default to a reasoning mode that emits
            # `<think>...</think>` before the JSON body and often truncates
            # mid-preamble on dense pages. `chat_template_kwargs` is the
            # vLLM-flavored knob to suppress it — Nebius accepts it and
            # non-thinking models silently ignore it.
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
    if settings.mistral_api_key:
        return {
            "provider": "mistral",
            "api_key": settings.mistral_api_key,
            "chat_model": settings.mistral_chat_model,
            "ocr_model": settings.mistral_ocr_model,
            "base_url": "https://api.mistral.ai/v1",
            "timeout": settings.mistral_timeout_seconds,
            "ocr_via_vision_chat": False,
            "ocr_concurrency": 4,
            # Mistral rejects unknown top-level params — leave empty.
            "extra_body": None,
        }
    return None
