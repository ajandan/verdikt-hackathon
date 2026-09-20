"""Three-level fallback resolver for per-stage pipeline models + routing.

    program.<stage>_model  →  tenant.<stage>_model  →  settings.<STAGE>_MODEL

Same pattern for ``triage_routing`` (case_type → route dict).

The runtime (``pap_pipeline.pipeline_config.build_pipeline_config``)
and the admin UI's "effective chain" panel both call
:func:`resolve_pipeline_models` so a single source of truth answers "why
did we pick this model for this case."

Kept in ``pap_pipeline`` (not ``docgen``) so a caller that wants to reuse
the pipeline without the docgen substrate still has resolution logic.
The tenant/program types are :class:`docgen.store.models.Tenant` /
``Program`` but we accept them as ``Any`` at the seam so the pipeline
package doesn't take a docgen import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pap_pipeline.stages.triage import _DEFAULT_ROUTING, normalize_routing


@dataclass(frozen=True)
class ResolvedModels:
    """Effective per-stage model names after walking program → tenant → settings.

    ``source`` maps each stage name to ``"program" | "tenant" | "settings"``
    telling the admin UI where the winning value came from — that's what
    powers the "Inherited from tenant" / "Program override" indicator.

    ``triage_routing`` is the resolved case_type → route dict; NULL at both
    the program and tenant level falls through to the platform default
    hardcoded in :mod:`pap_pipeline.stages.triage`.
    """

    extract: str
    triage: str
    retrieve: str | None  # None until Nebius hosts a rerank NIM (embed_model is separate)
    reason: str
    verifier: str | None  # optional dual-VLM cross-check; None = disabled
    # Full case_type → {route, decision_needed} table after 3-level merge.
    # Values include the ``decision_needed`` flag so the orchestrator can
    # decide whether to run Decide or skip straight to the downstream route.
    triage_routing: dict[str, dict[str, Any]]
    source: dict[str, str]


def _pick(
    program_value: str | None,
    tenant_value: str | None,
    settings_value: str,
    stage_name: str,
    source: dict[str, str],
) -> str:
    """Walk program → tenant → settings, recording which level won."""
    if program_value:
        source[stage_name] = "program"
        return program_value
    if tenant_value:
        source[stage_name] = "tenant"
        return tenant_value
    source[stage_name] = "settings"
    return settings_value


def resolve_pipeline_models(
    *,
    program: Any | None,
    tenant: Any | None,
    settings: Any,
) -> ResolvedModels:
    """Resolve the effective model for every pipeline stage.

    ``program`` and ``tenant`` may each be None (unscoped or unknown tenant).
    ``settings`` is the docgen ``Settings`` instance — reads ``triage_model``,
    ``pdf_vision_ocr_model``, ``ai_verdict_model``.

    Returns a fully-resolved :class:`ResolvedModels` — no None strings for
    stages that have a platform default. ``verifier`` stays None when the
    tenant hasn't opted in (dual-VLM is per-tenant insurance, not default).
    """
    source: dict[str, str] = {}

    extract = _pick(
        getattr(program, "extract_model", None),
        getattr(tenant, "extract_model", None),
        getattr(settings, "pdf_vision_ocr_model", "openbmb/MiniCPM-V-4_5"),
        "extract",
        source,
    )
    triage = _pick(
        getattr(program, "triage_model", None),
        getattr(tenant, "triage_model", None),
        getattr(settings, "triage_model", "nvidia/nemotron-nano-3-6b"),
        "triage",
        source,
    )
    reason = _pick(
        getattr(program, "reason_model", None),
        getattr(tenant, "reason_model", None),
        getattr(settings, "ai_verdict_model", "nvidia/nemotron-3-super-120b-a12b"),
        "reason",
        source,
    )
    # retrieve_model isn't wired into a stage yet (Nebius doesn't host a
    # rerank NIM); the column exists on tenants for symmetry. Resolve
    # anyway so the UI's "effective chain" panel has a value to show.
    retrieve_val = (
        getattr(program, "retrieve_model", None)
        or getattr(tenant, "retrieve_model", None)
    )
    source["retrieve"] = "program" if getattr(program, "retrieve_model", None) else (
        "tenant" if getattr(tenant, "retrieve_model", None) else "unset"
    )

    # Verifier is opt-in: NULL at both levels means "no dual-VLM check".
    # No settings fallback — a platform default would silently double the
    # extract cost for every case.
    verifier_val = (
        getattr(program, "verifier_extract_model", None)
        or getattr(tenant, "verifier_model", None)
    )
    source["verifier"] = "program" if getattr(program, "verifier_extract_model", None) else (
        "tenant" if getattr(tenant, "verifier_model", None) else "disabled"
    )

    # Triage routing: JSONB → dict merge, program overrides tenant, tenant
    # overrides platform default. Each layer only supplies the entries it
    # wants to change, so a program can override ONE case_type without
    # restating the others. ``normalize_routing`` upgrades any old-shape
    # dict-of-strings entries to the new dict-of-dicts shape on read.
    resolved_routing: dict[str, dict[str, Any]] = {
        ct: dict(cfg) for ct, cfg in _DEFAULT_ROUTING.items()
    }
    tenant_norm = normalize_routing(getattr(tenant, "triage_routing", None))
    for ct, cfg in tenant_norm.items():
        resolved_routing[ct] = cfg
    program_norm = normalize_routing(getattr(program, "triage_routing", None))
    for ct, cfg in program_norm.items():
        resolved_routing[ct] = cfg

    return ResolvedModels(
        extract=extract,
        triage=triage,
        retrieve=retrieve_val,
        reason=reason,
        verifier=verifier_val,
        triage_routing=resolved_routing,
        source=source,
    )


__all__ = ["ResolvedModels", "resolve_pipeline_models"]
