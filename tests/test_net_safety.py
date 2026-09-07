"""Unit tests for the outbound-URL SSRF validator."""
from __future__ import annotations

import pytest

from docgen.net_safety import UrlNotAllowed, validate_public_url


def _fake_resolver(ip: str):
    """Return a resolver stub that always resolves to `ip`."""
    def _r(_host, _service):
        return [(2, 1, 6, "", (ip, 0))]
    return _r


class TestSchemeAndUserinfo:
    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(UrlNotAllowed, match="scheme"):
            validate_public_url("file:///etc/passwd")

    def test_rejects_gopher_scheme(self) -> None:
        with pytest.raises(UrlNotAllowed, match="scheme"):
            validate_public_url("gopher://example.com/")

    def test_rejects_userinfo(self) -> None:
        with pytest.raises(UrlNotAllowed, match="userinfo"):
            validate_public_url(
                "https://user:pass@example.com/hook", resolver=_fake_resolver("93.184.216.34")
            )

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(UrlNotAllowed, match="host"):
            validate_public_url("https:///path")


class TestHostnameBlocklist:
    def test_rejects_localhost(self) -> None:
        with pytest.raises(UrlNotAllowed, match="loopback"):
            validate_public_url("https://localhost/hook")

    def test_rejects_subdomain_of_localhost(self) -> None:
        with pytest.raises(UrlNotAllowed, match="loopback"):
            validate_public_url("https://my.localhost/hook")

    def test_rejects_ip6_loopback_alias(self) -> None:
        with pytest.raises(UrlNotAllowed, match="loopback"):
            validate_public_url("https://ip6-loopback/hook")


class TestIpLiteral:
    def test_rejects_loopback_v4(self) -> None:
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://127.0.0.1/hook")

    def test_rejects_private_10(self) -> None:
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://10.0.0.5/hook")

    def test_rejects_private_192_168(self) -> None:
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://192.168.1.1/hook")

    def test_rejects_private_172_16(self) -> None:
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://172.16.0.1/hook")

    def test_rejects_link_local_169_254(self) -> None:
        # AWS/GCP metadata service.
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://169.254.169.254/latest/meta-data/")

    def test_rejects_ipv6_loopback(self) -> None:
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://[::1]/hook")

    def test_rejects_ipv6_unique_local(self) -> None:
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url("https://[fc00::1]/hook")


class TestDnsResolution:
    def test_accepts_when_resolver_returns_public_ip(self) -> None:
        validate_public_url(
            "https://example.com/hook", resolver=_fake_resolver("93.184.216.34")
        )

    def test_rejects_when_resolver_returns_private_ip(self) -> None:
        # Classic DNS-rebinding shape: friendly hostname pointing at RFC1918.
        with pytest.raises(UrlNotAllowed, match="non-public"):
            validate_public_url(
                "https://attacker.example/hook", resolver=_fake_resolver("10.0.0.7")
            )

    def test_rejects_when_dns_resolution_fails(self) -> None:
        import socket

        def _boom(_host, _service):
            raise socket.gaierror("nodename nor servname provided")

        with pytest.raises(UrlNotAllowed, match="unresolvable"):
            validate_public_url("https://does-not-exist.invalid/hook", resolver=_boom)
