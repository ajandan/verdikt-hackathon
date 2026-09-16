"""Per-program model overrides land in the pipeline config.

Locks the contract that `program.extract_model` / `program.reason_model`
flow into ExtractClientConfig / ReasonClientConfig when set, and fall back
to the settings-wide defaults when NULL.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from docgen.config import Settings
from pap_pipeline.inbox_hook import _pipeline_config


def _settings() -> Settings:
    """Bare Settings with the AI defaults populated — represents a normal
    deploy where DOCGEN_AI_BASE_URL / DOCGEN_AI_API_KEY / model defaults
    are set via env."""
    return Settings(
        ai_base_url="https://api.studio.nebius.com/v1",
        ai_api_key="test-key",
        pdf_vision_ocr_model="openbmb/MiniCPM-V-4_5",
        ai_verdict_model="nvidia/nemotron-3-super-120b-a12b",
    )


def test_pipeline_uses_defaults_when_program_is_none():
    """No program row passed (rare — happens for legacy inbox events
    without a program tag). Both stages fall back to settings defaults."""
    cfg = _pipeline_config(_settings(), tenant=None, program=None)
    assert cfg.extract.vision_model == "openbmb/MiniCPM-V-4_5"
    assert cfg.reason.model == "nvidia/nemotron-3-super-120b-a12b"


def test_pipeline_uses_defaults_when_program_columns_are_null():
    """Program row exists but hasn't had per-program overrides set —
    the Models admin tab hasn't been touched. Both stages fall back."""
    program = MagicMock(extract_model=None, reason_model=None)
    cfg = _pipeline_config(_settings(), tenant=None, program=program)
    assert cfg.extract.vision_model == "openbmb/MiniCPM-V-4_5"
    assert cfg.reason.model == "nvidia/nemotron-3-super-120b-a12b"


def test_pipeline_honors_program_extract_override():
    """Operator picked Gemma-3 as the Extract VLM for this tenant."""
    program = MagicMock(
        extract_model="google/gemma-3-27b-it",
        reason_model=None,
    )
    cfg = _pipeline_config(_settings(), tenant=None, program=program)
    assert cfg.extract.vision_model == "google/gemma-3-27b-it"
    # Reason still falls back to the settings default.
    assert cfg.reason.model == "nvidia/nemotron-3-super-120b-a12b"


def test_pipeline_honors_program_reason_override():
    """High-stakes tenant picked Nemotron Ultra 550B for reasoning."""
    program = MagicMock(
        extract_model=None,
        reason_model="nvidia/Nemotron-3-Ultra-550b-a55b",
    )
    cfg = _pipeline_config(_settings(), tenant=None, program=program)
    assert cfg.extract.vision_model == "openbmb/MiniCPM-V-4_5"
    assert cfg.reason.model == "nvidia/Nemotron-3-Ultra-550b-a55b"


def test_pipeline_honors_both_overrides_independently():
    """Both stages overridden — the pipeline threads each to the
    matching client config without cross-contamination."""
    program = MagicMock(
        extract_model="google/gemma-3-27b-it",
        reason_model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    )
    cfg = _pipeline_config(_settings(), tenant=None, program=program)
    assert cfg.extract.vision_model == "google/gemma-3-27b-it"
    assert cfg.reason.model == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def test_retrieve_config_is_not_tenant_overridable():
    """Retrieve intentionally stays at the process-wide model — embed model
    swaps live in code, not tenant config, until we add a Retrieve dropdown."""
    program = MagicMock(
        extract_model="google/gemma-3-27b-it",
        reason_model="nvidia/Nemotron-3-Ultra-550b-a55b",
    )
    cfg = _pipeline_config(_settings(), tenant=None, program=program)
    assert cfg.retrieve.embed_model == "Qwen/Qwen3-Embedding-8B"
    assert cfg.retrieve.rerank_model is None
