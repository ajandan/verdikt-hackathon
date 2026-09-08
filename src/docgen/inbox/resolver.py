"""Resolve which tenant an inbound email belongs to.

Two match rules, in priority order:

  1. Subject tag. Any tenant with `inbound_config.subject_tag` set to a
     substring of the message subject wins. This is the explicit path —
     stakeholders who copy a marker like "[tenant:acme]" into the subject
     line get deterministic routing even if their sender domain is shared
     with another tenant.

  2. Sender domain. Otherwise, the RFC 5321 domain of the From address
     (lower-cased) must appear in the tenant's `inbound_config.sender_domains`
     allow-list. Matches on exact domain OR any strictly-below subdomain
     (e.g. "acme.com" matches "billing.acme.com") so tenants don't have
     to enumerate every subdomain.

Returns None on no match. The caller logs the miss (still persisted as an
InboxEvent for auditability) and drops the message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docgen.store.models import Tenant


@dataclass(frozen=True)
class TenantMatch:
    tenant: Tenant
    reason: str  # "subject_tag" | "sender_domain"


def _extract_domain(addr: str) -> str | None:
    if not addr or "@" not in addr:
        return None
    domain = addr.rsplit("@", 1)[1].strip().rstrip(">").lower()
    return domain or None


def _domain_matches(sender_domain: str, allow: str) -> bool:
    allow = (allow or "").strip().lower().lstrip("@")
    if not allow:
        return False
    return sender_domain == allow or sender_domain.endswith("." + allow)


def _config_of(tenant: Tenant) -> dict[str, Any]:
    return dict(tenant.inbound_config or {})


def resolve_tenant(
    tenants: list[Tenant],
    *,
    from_addr: str,
    subject: str | None,
) -> TenantMatch | None:
    subject_norm = (subject or "").strip()

    # Rule 1: subject tag beats domain — explicit intent from the sender.
    if subject_norm:
        for t in tenants:
            tag = str(_config_of(t).get("subject_tag") or "").strip()
            if tag and tag in subject_norm:
                return TenantMatch(tenant=t, reason="subject_tag")

    # Rule 2: allow-listed sender domain.
    domain = _extract_domain(from_addr)
    if domain:
        for t in tenants:
            allow_list = _config_of(t).get("sender_domains") or []
            if not isinstance(allow_list, list):
                continue
            for allow in allow_list:
                if isinstance(allow, str) and _domain_matches(domain, allow):
                    return TenantMatch(tenant=t, reason="sender_domain")

    return None


def auto_approve_threshold(tenant: Tenant, *, default: float = 99.0) -> float:
    """Score at or above which the agent auto-publishes without review.

    Default is 99: room for a single acroform-default label to slip
    through on large forms without gating the whole ingest. Operators
    who want strict "zero misses" auto-publish can raise the tenant's
    inbound_config.auto_approve_threshold to 100.
    """
    raw = _config_of(tenant).get("auto_approve_threshold")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    # Clamp: a nonsensical value like 500 shouldn't silently gate everything.
    return max(0.0, min(100.0, v))


def notify_addresses(tenant: Tenant) -> list[str]:
    raw = _config_of(tenant).get("notify_addresses") or []
    if not isinstance(raw, list):
        return []
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
