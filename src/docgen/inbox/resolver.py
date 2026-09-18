"""Resolve which tenant an inbound email belongs to.

Two match rules, in priority order:

  1. Subject tag. Any tenant with `inbound_config.subject_tag` set to a
     substring of the message subject wins. This is the explicit path —
     stakeholders who copy a marker like "[tenant:acme]" into the subject
     line get deterministic routing even if their sender domain is shared
     with another tenant. Match is case-insensitive so a tag configured as
     `alderwood` still routes when a stakeholder types `Alderwood - foo`.

  2. Sender domain. Otherwise, the RFC 5321 domain of the From address
     (lower-cased) must appear in the tenant's `inbound_config.sender_domains`
     allow-list. Matches on exact domain OR any strictly-below subdomain
     (e.g. "acme.com" matches "billing.acme.com") so tenants don't have
     to enumerate every subdomain.

     Because ``From:`` is trivially spoofable, sender-domain routing also
     requires the receiving MX to have stamped an Authentication-Results
     header showing at least one of DMARC/DKIM/SPF ``pass`` for the
     visible ``From:`` domain. Messages without any Authentication-Results
     header (test fixtures, dev harnesses) still route — the enforcement
     is gated on the *presence* of a verdict, not its absence.

Returns None on no match. The caller logs the miss (still persisted as an
InboxEvent for auditability) and drops the message.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Any

from docgen.inbox.imap_poller import AuthResults
from docgen.store.models import Tenant

log = structlog.get_logger(__name__)


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
    auth_results: AuthResults | None = None,
) -> TenantMatch | None:
    subject_norm = (subject or "").strip()
    subject_lower = subject_norm.lower()

    # Rule 1: subject tag beats domain — explicit intent from the sender.
    # Case-insensitive so a tag configured as "alderwood" still matches when
    # a stakeholder naturally types "Alderwood - templates 9".
    if subject_lower:
        for t in tenants:
            tag = str(_config_of(t).get("subject_tag") or "").strip().lower()
            if tag and tag in subject_lower:
                return TenantMatch(tenant=t, reason="subject_tag")

    # Rule 2: allow-listed sender domain.
    domain = _extract_domain(from_addr)
    if domain:
        if not _sender_domain_is_authenticated(domain, auth_results):
            log.warning(
                "inbox_sender_domain_auth_failed",
                from_domain=domain,
                spf=getattr(auth_results, "spf", None),
                dkim=getattr(auth_results, "dkim", None),
                dmarc=getattr(auth_results, "dmarc", None),
            )
            return None
        for t in tenants:
            allow_list = _config_of(t).get("sender_domains") or []
            if not isinstance(allow_list, list):
                continue
            for allow in allow_list:
                if isinstance(allow, str) and _domain_matches(domain, allow):
                    return TenantMatch(tenant=t, reason="sender_domain")

    return None


def _sender_domain_is_authenticated(
    from_domain: str, auth: AuthResults | None
) -> bool:
    """Gate for the sender-domain routing branch of :func:`resolve_tenant`.

    Passes when:

      * No Authentication-Results header was stamped (test fixtures, dev
        harness, offline flows). Sender-domain routing then behaves as it
        did before this check — the caller accepts the trade-off by not
        putting an authenticating MX in front of the mailbox.
      * At least one of DMARC / DKIM / SPF returned ``pass`` AND the
        passing signature's domain aligns with the ``From:`` domain
        (org-level or subdomain match). Alignment closes the gap where a
        legitimate DKIM signature on ``bounces.mailer.example`` is
        replayed to pass ``authoritative.example.com`` routing.

    Explicit fail values (``fail`` / ``permerror``) block routing even
    if another method reports ``pass`` — a mixed verdict on a From that
    should have consistent alignment is a stronger tampering signal than
    it is a benign misconfiguration.
    """
    if auth is None:
        return True
    for verdict in (auth.spf, auth.dkim, auth.dmarc):
        if verdict in {"fail", "permerror"}:
            return False
    from_domain = from_domain.lower()
    header_from = (auth.header_from_domain or "").lower()
    dkim_domain = (auth.dkim_domain or "").lower()

    def _aligned(candidate: str) -> bool:
        if not candidate:
            return False
        return (
            candidate == from_domain
            or from_domain.endswith("." + candidate)
            or candidate.endswith("." + from_domain)
        )

    if auth.dmarc == "pass" and _aligned(header_from or from_domain):
        return True
    if auth.dkim == "pass" and _aligned(dkim_domain or header_from or from_domain):
        return True
    if auth.spf == "pass" and _aligned(header_from or from_domain):
        return True
    return False


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
