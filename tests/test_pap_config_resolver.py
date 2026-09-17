"""3-level fallback resolver — program → tenant → settings.

Locks in the precedence order: a program override wins over a tenant
default which wins over the platform default. Also verifies triage
routing merges correctly (each layer only supplies the entries it wants
to change) and that the ``source`` map reports the winning level so the
UI can render "Inherited from tenant" indicators.
"""
from __future__ import annotations

from dataclasses import dataclass

from pap_pipeline.config_resolver import resolve_pipeline_models


@dataclass
class _StubSettings:
    pdf_vision_ocr_model: str = "settings-extract"
    triage_model: str = "settings-triage"
    ai_verdict_model: str = "settings-reason"


@dataclass
class _StubTenant:
    extract_model: str | None = None
    triage_model: str | None = None
    retrieve_model: str | None = None
    reason_model: str | None = None
    verifier_model: str | None = None
    triage_routing: dict | None = None


@dataclass
class _StubProgram:
    extract_model: str | None = None
    triage_model: str | None = None
    retrieve_model: str | None = None
    reason_model: str | None = None
    verifier_extract_model: str | None = None
    triage_routing: dict | None = None


def test_all_null_falls_through_to_settings() -> None:
    r = resolve_pipeline_models(program=None, tenant=None, settings=_StubSettings())
    assert r.extract == "settings-extract"
    assert r.triage == "settings-triage"
    assert r.reason == "settings-reason"
    assert r.verifier is None  # disabled by default (no settings fallback)
    assert r.source == {
        "extract": "settings",
        "triage": "settings",
        "reason": "settings",
        "retrieve": "unset",
        "verifier": "disabled",
    }


def test_tenant_overrides_settings() -> None:
    tenant = _StubTenant(extract_model="tenant-extract", triage_model="tenant-triage")
    r = resolve_pipeline_models(program=None, tenant=tenant, settings=_StubSettings())
    assert r.extract == "tenant-extract"
    assert r.triage == "tenant-triage"
    assert r.reason == "settings-reason"  # tenant didn't override
    assert r.source["extract"] == "tenant"
    assert r.source["triage"] == "tenant"
    assert r.source["reason"] == "settings"


def test_program_overrides_tenant() -> None:
    tenant = _StubTenant(extract_model="tenant-extract", triage_model="tenant-triage")
    program = _StubProgram(extract_model="program-extract")
    r = resolve_pipeline_models(program=program, tenant=tenant, settings=_StubSettings())
    assert r.extract == "program-extract"
    assert r.source["extract"] == "program"
    # Triage: tenant provides, program doesn't → tenant wins
    assert r.triage == "tenant-triage"
    assert r.source["triage"] == "tenant"


def test_verifier_stays_none_without_explicit_opt_in() -> None:
    """No settings fallback for verifier — opt-in only, or every case
    would silently pay for a dual-VLM cross-check."""
    r = resolve_pipeline_models(program=None, tenant=None, settings=_StubSettings())
    assert r.verifier is None
    r2 = resolve_pipeline_models(
        program=None, tenant=_StubTenant(verifier_model="tenant-verifier"), settings=_StubSettings(),
    )
    assert r2.verifier == "tenant-verifier"
    assert r2.source["verifier"] == "tenant"


def test_triage_routing_defaults_to_platform_map() -> None:
    r = resolve_pipeline_models(program=None, tenant=None, settings=_StubSettings())
    # Full platform default present — dict of {route, decision_needed}
    assert r.triage_routing["new_intake"] == {"route": "decide", "decision_needed": True}
    assert r.triage_routing["refill"] == {"route": "pharmacy_direct", "decision_needed": False}
    assert r.triage_routing["appeal"] == {"route": "crm_appeals", "decision_needed": True}
    assert r.triage_routing["missing_info_reply"] == {"route": "decide", "decision_needed": True}
    assert r.triage_routing["not_decidable"] == {"route": "reject", "decision_needed": False}


def test_triage_routing_tenant_override_merges_with_defaults() -> None:
    """Tenant overrides only the rows they specify; other rows keep the
    platform defaults. Old-shape (str value) auto-upgrades to
    {route, decision_needed} dict on read via normalize_routing."""
    tenant = _StubTenant(triage_routing={"refill": "crm_outreach"})
    r = resolve_pipeline_models(program=None, tenant=tenant, settings=_StubSettings())
    # tenant override: route replaced, decision_needed inherited from platform default (False)
    assert r.triage_routing["refill"]["route"] == "crm_outreach"
    assert r.triage_routing["refill"]["decision_needed"] is False
    # platform defaults retained for untouched rows
    assert r.triage_routing["new_intake"]["route"] == "decide"
    assert r.triage_routing["appeal"]["route"] == "crm_appeals"


def test_triage_routing_program_override_beats_tenant() -> None:
    tenant = _StubTenant(triage_routing={"refill": "crm_outreach"})
    program = _StubProgram(
        triage_routing={"refill": {"route": "reject", "decision_needed": True}}
    )
    r = resolve_pipeline_models(program=program, tenant=tenant, settings=_StubSettings())
    assert r.triage_routing["refill"]["route"] == "reject"
    assert r.triage_routing["refill"]["decision_needed"] is True


def test_new_shape_dict_of_dicts_is_preserved() -> None:
    """Storing in the new dict-of-dicts shape round-trips both fields."""
    tenant = _StubTenant(
        triage_routing={"refill": {"route": "crm_outreach", "decision_needed": True}}
    )
    r = resolve_pipeline_models(program=None, tenant=tenant, settings=_StubSettings())
    assert r.triage_routing["refill"] == {"route": "crm_outreach", "decision_needed": True}


def test_retrieve_stays_none_when_neither_layer_sets_it() -> None:
    r = resolve_pipeline_models(program=None, tenant=None, settings=_StubSettings())
    assert r.retrieve is None
    assert r.source["retrieve"] == "unset"


def test_source_map_covers_every_stage() -> None:
    """The UI expects a source entry for every stage — no missing keys."""
    r = resolve_pipeline_models(program=None, tenant=None, settings=_StubSettings())
    assert set(r.source) == {"extract", "triage", "reason", "retrieve", "verifier"}
