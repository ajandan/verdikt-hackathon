"""Tenant resolution for inbound emails.

Pure unit tests — no DB, no network. Constructs bare Tenant instances
with `inbound_config` and exercises the resolver's rule ordering:
subject_tag beats sender_domain, subdomains match parent, misses return
None.
"""
from __future__ import annotations

import uuid

from docgen.inbox.resolver import (
    auto_approve_threshold,
    notify_addresses,
    resolve_tenant,
)
from docgen.store.models import Tenant


def _tenant(name: str, **cfg) -> Tenant:
    # Bypass DB defaults — we only need in-memory attribute access.
    t = Tenant(id=uuid.uuid4(), name=name, ai_provider_config={})
    t.inbound_config = cfg
    return t


def test_subject_tag_beats_domain_match():
    acme = _tenant("acme", sender_domains=["shared.example"], subject_tag="[tenant:acme]")
    beta = _tenant("beta", sender_domains=["shared.example"], subject_tag="[tenant:beta]")
    match = resolve_tenant(
        [acme, beta],
        from_addr="user@shared.example",
        subject="Please onboard this [tenant:beta] template",
    )
    assert match is not None
    assert match.tenant.name == "beta"
    assert match.reason == "subject_tag"


def test_sender_domain_matches_exact():
    acme = _tenant("acme", sender_domains=["acme.com"])
    match = resolve_tenant([acme], from_addr="alice@acme.com", subject="hi")
    assert match is not None
    assert match.reason == "sender_domain"


def test_sender_domain_matches_subdomain():
    acme = _tenant("acme", sender_domains=["acme.com"])
    match = resolve_tenant([acme], from_addr="alice@billing.acme.com", subject="x")
    assert match is not None
    assert match.tenant.name == "acme"


def test_sender_domain_case_insensitive():
    acme = _tenant("acme", sender_domains=["Acme.COM"])
    match = resolve_tenant([acme], from_addr="alice@ACME.com", subject="x")
    assert match is not None


def test_no_match_returns_none():
    acme = _tenant("acme", sender_domains=["acme.com"])
    match = resolve_tenant([acme], from_addr="alice@other.com", subject="no tags here")
    assert match is None


def test_no_match_when_config_missing():
    t = _tenant("empty")
    assert resolve_tenant([t], from_addr="x@y.com", subject="hi") is None


def test_similar_but_not_subdomain_rejected():
    # notacme.com should NOT match acme.com — the boundary is a literal '.'
    acme = _tenant("acme", sender_domains=["acme.com"])
    assert resolve_tenant([acme], from_addr="x@notacme.com", subject="hi") is None


def test_auto_approve_threshold_default():
    t = _tenant("acme")
    assert auto_approve_threshold(t) == 99.0


def test_auto_approve_threshold_clamped():
    t = _tenant("acme", auto_approve_threshold=500)
    assert auto_approve_threshold(t) == 100.0
    t2 = _tenant("beta", auto_approve_threshold=-10)
    assert auto_approve_threshold(t2) == 0.0


def test_auto_approve_threshold_bad_value_falls_back():
    t = _tenant("acme", auto_approve_threshold="not-a-number")
    assert auto_approve_threshold(t) == 99.0


def test_notify_addresses_filters_junk():
    t = _tenant("acme", notify_addresses=["ops@acme.com", "", 123, "  qa@acme.com  "])
    addrs = notify_addresses(t)
    assert addrs == ["ops@acme.com", "qa@acme.com"]
