"""Nebius Token Factory provider — NVIDIA Nemotron on Nebius AI Cloud.

Compliance-critical for the hackathon: the design-time LLM must route to an
NVIDIA open-source model hosted on Nebius.
"""
from __future__ import annotations

import pytest

from docgen.config import get_settings
from docgen.providers import get_provider
from docgen.providers.nebius import DEFAULT_BASE_URL, DEFAULT_MODEL, NebiusProvider


@pytest.fixture(autouse=True)
def _isolate_from_env(monkeypatch):
    """The developer's local .env may set real Nebius credentials; these
    tests assume a clean environment. Wipe the AI vars, disable .env-file
    loading (pydantic-settings reads it directly, not via os.environ), and
    clear the Settings lru_cache so each test sees only tenant_config.
    """
    from docgen.config import Settings

    for var in (
        "DOCGEN_AI_PROVIDER",
        "DOCGEN_AI_API_KEY",
        "DOCGEN_AI_BASE_URL",
        "DOCGEN_AI_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_nebius_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="api_key"):
        get_provider({"provider": "nebius"})


def test_nebius_uses_defaults_when_only_api_key_given() -> None:
    p = get_provider({"provider": "nebius", "api_key": "nb-secret"})
    assert isinstance(p, NebiusProvider)
    assert p._base_url == DEFAULT_BASE_URL
    assert p._model == DEFAULT_MODEL
    assert p._api_key == "nb-secret"


def test_nebius_defaults_target_nvidia_nemotron() -> None:
    # Hackathon rule: at least one NVIDIA open-source model. The default model
    # string must name an NVIDIA-published model so a misconfigured deploy
    # can't silently drift off-compliance.
    assert DEFAULT_MODEL.startswith("nvidia/")
    assert "nebius" in DEFAULT_BASE_URL


def test_nebius_overrides_model_and_url() -> None:
    p = get_provider(
        {
            "provider": "nebius",
            "api_key": "nb-secret",
            "base_url": "https://custom.nebius.example/v1",
            "model": "nvidia/other-nemotron",
        }
    )
    assert isinstance(p, NebiusProvider)
    assert p._base_url == "https://custom.nebius.example/v1"
    assert p._model == "nvidia/other-nemotron"
