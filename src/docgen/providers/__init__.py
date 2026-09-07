from typing import Any

from docgen.config import get_settings
from docgen.providers.base import AIProvider, MappingProposal
from docgen.providers.openai_compat import OpenAICompatProvider
from docgen.providers.stub import StubProvider


def get_provider(tenant_config: dict[str, Any] | None = None) -> AIProvider:
    """Instantiated per call — cheap; keeps config swappable at runtime.

    If `tenant_config` is provided (from Tenant.ai_provider_config), each key
    overlays the global env-derived setting. Missing keys fall back to global.
    Recognized keys: `provider` (stub|openai_compat), `base_url`, `api_key`,
    `model`, `timeout_seconds`.
    """
    s = get_settings()
    cfg = tenant_config or {}

    provider_name = cfg.get("provider") or s.ai_provider
    base_url = cfg.get("base_url") or s.ai_base_url
    api_key = cfg.get("api_key") or s.ai_api_key
    model = cfg.get("model") or s.ai_model
    timeout = float(cfg.get("timeout_seconds") or s.ai_timeout_seconds)

    if provider_name == "openai_compat":
        if not base_url or not api_key:
            raise RuntimeError(
                "AI provider 'openai_compat' requires base_url and api_key "
                "(via DOCGEN_AI_BASE_URL/DOCGEN_AI_API_KEY or Tenant.ai_provider_config)."
            )
        return OpenAICompatProvider(base_url=base_url, api_key=api_key, model=model, timeout=timeout)
    return StubProvider()


__all__ = ["AIProvider", "MappingProposal", "get_provider"]
