"""PipelineConfig assembly from Settings + Tenant + Program.

Extracted from :mod:`pap_pipeline.inbox_hook`. Owns the "settings +
tenant + program → runtime pipeline config" translation:

* :func:`build_pipeline_config` walks the 3-level model-override chain
  (program → tenant → settings; see ADR-0036) and stamps out one
  :class:`PipelineConfig` per invocation. The three AI stages share
  ``ai_base_url`` + ``ai_api_key`` because they all target Nebius
  Token Factory; the reranker slot stays empty because Nebius doesn't
  host NVIDIA's rerank NIM (the NVIDIA-model requirement is met by
  the reason stage's Nemotron).

* :func:`build_mdm_config` maps ``Tenant.mdm_config`` (JSONB) onto
  :class:`MdmSourceConfig`. Empty / missing dict falls back to the
  simulated source so the demo works out-of-the-box. Numeric fields
  are coerced with a safe fallback + warning so a stray string in
  the DB can't crash the pipeline at startup.

Kept as a separate module so both the email intake (``inbox_hook``)
and the JSON intake (``api_hook``) can pull the same builder without
transitively importing all of ``inbox_hook``.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

from docgen.config import Settings
from docgen.store.models import Program, Tenant
from pap_pipeline.orchestrator import PipelineConfig
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.mdm_lookup import MdmSourceConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig

log = structlog.get_logger(__name__)


def build_pipeline_config(
    settings: Settings,
    tenant: Tenant | None = None,
    program: Program | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig from docgen Settings + the tenant's MDM config.

    The three AI stages share `ai_base_url` + `ai_api_key` because they all
    target Nebius Token Factory. The MDM slot is per-tenant: each tenant
    owns its own connector config on the `Tenant.mdm_config` JSONB column
    (ADR-0004). When the tenant isn't in scope (rare — the JSON-in API path
    resolves it) or hasn't wired a config, we fall back to the simulated
    source so the demo works out-of-the-box.

    Per-stage model overrides walk a 3-level chain (0036):

        program.<stage>_model  →  tenant.<stage>_model  →  settings.<STAGE>_MODEL

    So the same override slot exists at both the tenant level (a default
    for every program in the tenant) and the program level (a per-program
    exception). NULL at both falls back to the settings default. Resolved
    by :func:`pap_pipeline.config_resolver.resolve_pipeline_models`.
    """
    from pap_pipeline.config_resolver import resolve_pipeline_models
    from pap_pipeline.stages.triage import TriageConfig

    base_url = (settings.ai_base_url or "https://api.studio.nebius.com/v1").rstrip("/")
    api_key = settings.ai_api_key or ""
    resolved = resolve_pipeline_models(program=program, tenant=tenant, settings=settings)
    extract_verifier = (
        ExtractClientConfig(
            base_url=base_url,
            api_key=api_key,
            vision_model=resolved.verifier,
            timeout=settings.ai_timeout_seconds,
        )
        if resolved.verifier
        else None
    )
    declared_outcomes = (
        list(program.outcomes) if program is not None and program.outcomes else None
    )
    triage_config = TriageConfig(
        enabled=True,
        # Routing table (case_type → {route, decision_needed}) comes from
        # the resolved 3-level chain; per-program or per-tenant overrides
        # win over the hardcoded platform default.
        routing=resolved.triage_routing,
        # LLM classifier creds — same Nebius endpoint + key as the other
        # stages. When any of these is blank the stage falls back to the
        # deterministic rule-based classifier.
        base_url=base_url,
        api_key=api_key,
        model=resolved.triage,
        timeout=settings.ai_timeout_seconds,
    )
    return PipelineConfig(
        extract=ExtractClientConfig(
            base_url=base_url,
            api_key=api_key,
            vision_model=resolved.extract,
            timeout=settings.ai_timeout_seconds,
        ),
        retrieve=RetrieveClientConfig(
            base_url=base_url,
            api_key=api_key,
            # Nebius Token Factory doesn't host the NVIDIA embed/rerank NIMs
            # yet, so use their sole embed model and skip rerank. The NVIDIA
            # requirement is met by the reason stage's Nemotron.
            embed_model="Qwen/Qwen3-Embedding-8B",
            rerank_model=None,
            timeout=settings.ai_timeout_seconds,
        ),
        reason=ReasonClientConfig(
            base_url=base_url,
            api_key=api_key,
            model=resolved.reason,
            timeout=settings.ai_timeout_seconds,
        ),
        mdm=build_mdm_config(tenant),
        extract_verifier=extract_verifier,
        declared_outcomes=declared_outcomes,
        triage=triage_config,
    )


# Numeric fields on MdmSourceConfig — coerced from JSON with a safe
# fallback so a stray string in `tenant.mdm_config` can't crash the
# whole pipeline. Missing keys inherit the dataclass default.
_MDM_NUMERIC_KEYS = ("max_window_days", "max_events", "timeout")


def build_mdm_config(tenant: Tenant | None) -> MdmSourceConfig:
    """Map ``tenant.mdm_config`` (JSONB) → :class:`MdmSourceConfig`.

    Empty / missing dict → ``provider="simulated"``. Operators opt out
    explicitly with ``{"provider": "null"}`` — silently falling back to
    "null" would hide misconfiguration, which is worse than showing a
    demo scenario the operator didn't ask for.

    ``api_key_env`` holds the *name* of an env var; the actual secret
    never lands in the DB. Missing env var → api_key stays ``None`` and
    the connector fails at lookup time with a clear message rather than
    running with an empty bearer token.
    """
    cfg = dict((tenant.mdm_config if tenant is not None else None) or {})
    provider = str(cfg.get("provider") or "").strip().lower() or "simulated"

    api_key: str | None = None
    env_name = cfg.get("api_key_env")
    if isinstance(env_name, str) and env_name:
        api_key = os.environ.get(env_name) or None
        if api_key is None:
            log.warning(
                "pap_mdm_api_key_env_missing",
                tenant_id=str(tenant.id) if tenant else None,
                env_name=env_name,
            )

    families = cfg.get("program_families") or ()
    if isinstance(families, list):
        families = tuple(str(f) for f in families)
    elif not isinstance(families, tuple):
        families = ()

    numeric: dict[str, Any] = {}
    defaults = {"max_window_days": 1825, "max_events": 200, "timeout": 30.0}
    for key in _MDM_NUMERIC_KEYS:
        raw_val = cfg.get(key)
        if raw_val is None:
            continue
        try:
            numeric[key] = type(defaults[key])(raw_val)
        except (TypeError, ValueError):
            log.warning(
                "pap_mdm_config_bad_numeric",
                tenant_id=str(tenant.id) if tenant else None,
                key=key,
                value=raw_val,
            )

    return MdmSourceConfig(
        provider=provider,
        program_families=families,
        base_url=cfg.get("base_url") or None,
        api_key=api_key,
        **numeric,
    )


__all__ = ["build_mdm_config", "build_pipeline_config"]
