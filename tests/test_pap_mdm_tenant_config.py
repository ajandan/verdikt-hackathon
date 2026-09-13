"""`Tenant.mdm_config` → :class:`MdmSourceConfig` resolution (ADR-0004).

The resolver is the seam that lets each tenant point at a different MDM
without a redeploy. Regressions here break tenant isolation of the
history-retrieval path — worth their own suite even though the mapper is
small.

The fallback behaviour matters as much as the mapping: a fresh tenant
(empty `mdm_config`) must resolve to the simulated adapter so the demo
still works end-to-end without operator setup. Silently falling through
to "null" would break the "MDM is always on" invariant the ledger
depends on for its snapshot hash.
"""
from __future__ import annotations

import types

import pytest

from pap_pipeline.inbox_hook import _mdm_config_for_tenant
from pap_pipeline.stages.mdm_lookup import (
    MdmSourceConfig,
    SimulatedMdmSource,
    build_mdm_source,
)


def _fake_tenant(mdm_config: dict | None) -> types.SimpleNamespace:
    """Duck-typed stand-in for Tenant.

    `_mdm_config_for_tenant` only reads `.id` and `.mdm_config`, so
    SimpleNamespace is enough — avoids spinning up a DB session just to
    exercise the mapper.
    """
    return types.SimpleNamespace(id="tnt-x", mdm_config=mdm_config)


def test_missing_tenant_falls_back_to_simulated() -> None:
    """No tenant in scope (API path before resolution, or a bare unit
    test) resolves to simulated — same code path a fresh tenant hits."""
    cfg = _mdm_config_for_tenant(None)
    assert cfg.provider == "simulated"
    assert isinstance(build_mdm_source(cfg), SimulatedMdmSource)


def test_empty_mdm_config_falls_back_to_simulated() -> None:
    """New tenant, no operator setup → still gets a working MDM slice.
    The 'MDM is always on' invariant is what lets the ledger carry
    `mdm_snapshot_hash` for every case."""
    cfg = _mdm_config_for_tenant(_fake_tenant({}))
    assert cfg.provider == "simulated"


def test_explicit_null_provider_opts_out() -> None:
    """Operators disable MDM by wiring `{"provider": "null"}` explicitly.
    The reason "why is history not showing up" answers to a config value,
    not a silent fallback."""
    cfg = _mdm_config_for_tenant(_fake_tenant({"provider": "null"}))
    assert cfg.provider == "null"


def test_http_provider_reads_transport_fields() -> None:
    """The provider-specific fields (base_url, timeout, window, cap) are
    lifted onto the :class:`MdmSourceConfig` dataclass so the connector
    doesn't have to re-parse the JSON at request time."""
    cfg = _mdm_config_for_tenant(_fake_tenant({
        "provider": "state-mdm-v1",
        "base_url": "https://mdm.example.com/api",
        "program_families": ["cardiovita", "oncobridge"],
        "max_window_days": 900,
        "max_events": 50,
        "timeout": 15,
    }))
    assert cfg.provider == "state-mdm-v1"
    assert cfg.base_url == "https://mdm.example.com/api"
    assert cfg.program_families == ("cardiovita", "oncobridge")
    assert cfg.max_window_days == 900
    assert cfg.max_events == 50
    assert cfg.timeout == 15.0  # coerced to float


def test_api_key_env_resolves_from_environment(monkeypatch) -> None:
    """`api_key_env` names an env var — the raw secret never sits in
    the DB. Same pattern as the existing `Tenant.key_ref` for AI creds."""
    monkeypatch.setenv("TENANT_ACME_MDM_KEY", "s3cret")
    cfg = _mdm_config_for_tenant(_fake_tenant({
        "provider": "state-mdm-v1",
        "base_url": "https://x",
        "api_key_env": "TENANT_ACME_MDM_KEY",
    }))
    assert cfg.api_key == "s3cret"


def test_missing_env_leaves_api_key_none(monkeypatch) -> None:
    """A misconfigured env var name shouldn't silently ship an empty
    bearer token. Leave api_key=None so the connector's own guard
    surfaces the error at lookup time."""
    monkeypatch.delenv("TENANT_ACME_MDM_KEY", raising=False)
    cfg = _mdm_config_for_tenant(_fake_tenant({
        "provider": "state-mdm-v1",
        "base_url": "https://x",
        "api_key_env": "TENANT_ACME_MDM_KEY",
    }))
    assert cfg.api_key is None


def test_bad_numeric_field_falls_back_to_dataclass_default() -> None:
    """A stray string in a numeric field (bad admin form, hand-edited
    JSON) shouldn't crash the whole pipeline. The mapper logs + keeps
    the dataclass default so the case still runs."""
    cfg = _mdm_config_for_tenant(_fake_tenant({
        "provider": "simulated",
        "max_events": "not-a-number",
    }))
    assert cfg.provider == "simulated"
    assert cfg.max_events == 200  # MdmSourceConfig default


def test_provider_string_is_normalized_lowercase() -> None:
    """Provider strings are matched case-insensitively by
    `build_mdm_source` — normalize at the tenant seam so a
    `"Simulated"` typed by an operator still routes right."""
    cfg = _mdm_config_for_tenant(_fake_tenant({"provider": "  SIMULATED "}))
    assert cfg.provider == "simulated"
    assert isinstance(build_mdm_source(cfg), SimulatedMdmSource)


def test_mdm_source_config_is_a_frozen_dataclass_clone() -> None:
    """The mapper builds a fresh :class:`MdmSourceConfig` — mutating the
    tenant's JSON dict later can't reach back and mutate a live config
    object elsewhere in the process."""
    tenant = _fake_tenant({"provider": "simulated"})
    cfg = _mdm_config_for_tenant(tenant)
    tenant.mdm_config["provider"] = "http"
    assert cfg.provider == "simulated"
    # And the returned config is a real MdmSourceConfig.
    assert isinstance(cfg, MdmSourceConfig)
