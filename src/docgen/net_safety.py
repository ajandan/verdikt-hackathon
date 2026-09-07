"""URL safety checks for outbound HTTP callbacks (SSRF defense).

Validates that a URL is safe to make an outbound request to:
  * scheme is http or https (no file://, ftp://, gopher://, ...)
  * no userinfo component (no basic-auth smuggled in)
  * hostname is not a loopback alias (localhost, *.localhost, ip6-loopback)
  * every A/AAAA record resolves to a public IP — private, loopback,
    link-local, multicast, reserved, and unspecified addresses are rejected

Callers should invoke `validate_public_url()` at two points:
  1. The HTTP request boundary (schema/route) so bad URLs never reach the DB.
  2. Just before the actual outbound request (defense against DNS rebinding
     between enqueue and delivery, and to catch URLs inserted through other
     paths such as direct DB writes).
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

Resolver = Callable[[str, Any], list[tuple]]


class UrlNotAllowed(ValueError):
    """Raised when a URL fails safety validation. `str(exc)` is a short reason."""


def validate_public_url(url: str, *, resolver: Resolver | None = None) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(f"scheme not allowed: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise UrlNotAllowed("userinfo (user:pass@) not allowed in URL")
    host = parsed.hostname
    if not host:
        raise UrlNotAllowed("missing host")
    host_lower = host.lower()
    if host_lower in _BLOCKED_HOSTNAMES or host_lower.endswith(".localhost"):
        raise UrlNotAllowed(f"loopback hostname not allowed: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        _resolve_and_check(host, resolver=resolver)
        return
    _reject_if_not_public(ip)


def _resolve_and_check(host: str, *, resolver: Resolver | None) -> None:
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, None)
    except OSError as e:
        raise UrlNotAllowed(f"unresolvable host {host!r}: {e}") from e
    if not infos:
        raise UrlNotAllowed(f"unresolvable host {host!r}")
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise UrlNotAllowed(f"invalid resolved IP {ip_str!r}") from e
        _reject_if_not_public(ip)


def _reject_if_not_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UrlNotAllowed(f"non-public IP: {ip}")
