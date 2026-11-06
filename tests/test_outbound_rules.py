"""Manual target lists are literal and predictable; environment exclusions stay compatible."""
import socket

import pytest

from mcp_manager import outbound


@pytest.mark.parametrize("entry,url,hit", [
    ("example.com", "https://example.com", True),
    ("EXAMPLE.COM.", "https://example.com", True),
    ("example.com", "https://a.example.com", False),
    ("*.example.com", "https://a.example.com", True),
    ("*.example.com", "https://a.b.example.com", True),
    ("*.example.com", "https://example.com", False),
    ("*.example.com", "https://badexample.com", False),
    (".example.com", "https://a.example.com", True),
    ("192.0.2.1", "http://192.0.2.1", True),
    ("192.0.2.1", "http://192.0.2.2", False),
    ("::1", "http://[0:0:0:0:0:0:0:1]", True),
    ("192.0.2.0/24", "http://192.0.2.255", True),
    ("192.0.2.0/24", "http://192.0.3.1", False),
    ("2001:db8::/32", "http://[2001:db8:1::2]", True),
    ("2001:db8::/32", "http://[2001:db9::1]", False),
    ("https://*.example.com:8443", "https://a.example.com:8443", True),
    ("https://*.example.com:8443", "http://a.example.com:8443", False),
    ("https://*.example.com:8443", "https://a.example.com:443", False),
])
def test_target_matching_and_black_white_routing(entry, url, hit):
    value = outbound.normalize_config({
        "enabled": True, "mode": "manual", "host": "proxy.example", "list_entries": [entry],
    })
    assert outbound.matches(url, [entry]) is hit
    assert bool(outbound.client_options(value, url).get("proxy")) is not hit
    value["list_mode"] = "whitelist"
    assert bool(outbound.client_options(value, url).get("proxy")) is hit


def test_cidr_never_resolves_hostname(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Target matching must not resolve DNS")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert outbound.matches("https://example.com", ["0.0.0.0/0", "::/0"]) is False


@pytest.mark.parametrize("entry", ["192.0.2.0/33", "2001:db8::/129", "example.com/24", "foo*bar.example", "*."])
def test_invalid_target_rule_fails_format_validation(entry):
    with pytest.raises(ValueError):
        outbound.normalize_config({"list_entries": [entry]})


def test_env_no_proxy_keeps_hostname_and_subdomain_semantics(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "example.com")
    value = outbound.normalize_config({"enabled": True, "mode": "env"})
    assert "proxy" not in outbound.client_options(value, "https://example.com")
    assert "proxy" not in outbound.client_options(value, "https://a.example.com")
