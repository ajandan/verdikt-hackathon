"""Per-tenant AI provider config (§12): Tenant.ai_provider_config overlays global env."""
from __future__ import annotations

import pytest

from docgen.config import get_settings
from docgen.providers import get_provider
from docgen.providers.openai_compat import OpenAICompatProvider
from docgen.providers.stub import StubProvider


@pytest.fixture(autouse=True)
def _isolate_from_env(monkeypatch):
    """Force each test to see the built-in Settings defaults, ignoring any
    ambient .env that a developer might have populated for real dev work.
    Pydantic-settings reads .env directly, so we also disable env_file.
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


def test_get_provider_defaults_to_env_stub() -> None:
    assert isinstance(get_provider(), StubProvider)


def test_tenant_config_overrides_to_openai_compat() -> None:
    p = get_provider(
        {
            "provider": "openai_compat",
            "base_url": "https://tenant-openai.example/v1",
            "api_key": "sk-tenant",
            "model": "gpt-4o-mini",
        }
    )
    assert isinstance(p, OpenAICompatProvider)
    assert p._base_url == "https://tenant-openai.example/v1"
    assert p._api_key == "sk-tenant"
    assert p._model == "gpt-4o-mini"


def test_tenant_openai_compat_requires_base_and_key() -> None:
    with pytest.raises(RuntimeError, match="base_url and api_key"):
        get_provider({"provider": "openai_compat"})


def test_partial_overlay_falls_back_to_env() -> None:
    # provider not overridden → stub (from env default), config ignored
    assert isinstance(get_provider({"model": "unused"}), StubProvider)
