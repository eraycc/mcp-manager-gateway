"""Outbound proxy routing, validation, and hot changes."""
import json

import httpx
import httpx2
import pytest

from mcp_manager import outbound
from mcp_manager.database import SystemSetting


def manual(**values):
    return outbound.normalize_config(dict(enabled=True, mode="manual", host="proxy.example", port=8080, **values))


def test_defaults_ignore_environment_and_encode_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://env.invalid:9000")
    assert outbound.client_options(outbound.DEFAULTS, "https://api.example")["trust_env"] is False
    config = manual(auth_type="basic", username="a@b", password="p:/?#")
    assert outbound.proxy_url(config) == "http://a%40b:p%3A%2F%3F%23@proxy.example:8080"
    assert outbound.proxy_url(manual(auth_type="basic", username="user", password="")) == "http://user@proxy.example:8080"


@pytest.mark.parametrize("entry,host,hit", [
    ("example.com", "example.com", True), ("example.com", "a.example.com", False),
    (".example.com", "example.com", False), (".example.com", "a.example.com", True),
    ("example.com", "badexample.com", False), ("127.0.0.1", "127.0.0.1", True),
    ("https://example.com:8443", "example.com:8443", True),
])
def test_blacklist_whitelist(entry, host, hit):
    url = "https://" + host
    black = manual(list_entries=[entry])
    white = manual(list_entries=[entry], list_mode="whitelist")
    assert bool(outbound.client_options(black, url).get("proxy")) is not hit
    assert bool(outbound.client_options(white, url).get("proxy")) is hit


@pytest.mark.parametrize("values", [
    {"host": "http://bad"}, {"port": 0}, {"port": True}, {"scheme": "ftp"},
    {"auth_type": "basic", "username": ""}, {"list_entries": [""]},
    {"ssl_verify": "custom_ca", "custom_ca_path": ""}, {"list_entries": ["host/path"]},
])
def test_invalid_config(values):
    with pytest.raises(ValueError):
        outbound.normalize_config({"enabled": True, "mode": "manual", "host": "proxy.example", "port": 8080} | values)


@pytest.mark.parametrize("module", [httpx, httpx2])
async def test_existing_client_uses_new_policy_without_interrupting_stream(module):
    config = manual()
    calls, closed = [], []
    class Stream(module.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"
        async def aclose(self):
            closed.append(True)
    async def send(request):
        calls.append(request.extensions["proxy_test"])
        return module.Response(200, stream=Stream())
    def build(value, url, lib, **kwargs):
        options = outbound.client_options(value, url)
        async def handle(request):
            request.extensions["proxy_test"] = options.get("proxy")
            return await send(request)
        return lib.AsyncClient(transport=lib.MockTransport(handle))
    transport = outbound.OutboundTransport(lambda: async_config(), module=module, builder=build)
    async def async_config():
        return config
    async with module.AsyncClient(transport=transport) as client:
        first = await client.send(client.build_request("GET", "https://target.example"), stream=True)
        config = dict(outbound.DEFAULTS)
        assert not closed
        second = await client.get("https://target.example")
        assert second.content == b"ok"
        assert calls == ["http://proxy.example:8080", None]
        await first.aclose()
    assert len(closed) == 2


async def test_settings_format_only_save_and_advisory_probe(db_app, monkeypatch):
    web, state = db_app.web, db_app.app.state
    before = (await web.get("/api/v1/settings/outbound-proxy")).json()
    assert before["enabled"] is False
    probes = []
    async def fail(config):
        probes.append(config)
        raise ValueError("无法连接到代理服务器")
    monkeypatch.setattr(outbound, "validate_connection", fail)
    payload = manual(auth_type="basic", username="tester", password="test-only-secret")
    response = await web.patch("/api/v1/settings/outbound-proxy", json=payload | {"host": "http://invalid"})
    assert response.status_code == 422
    assert (await web.get("/api/v1/settings/outbound-proxy")).json() == before
    response = await web.post("/api/v1/settings/outbound-proxy/test", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert (await web.get("/api/v1/settings/outbound-proxy")).json() == before
    response = await web.patch("/api/v1/settings/outbound-proxy", json=payload)
    assert response.status_code == 200, response.text
    assert len(probes) == 1  # Network availability never gates saving.
    assert response.json()["password"] == "[REDACTED]"
    assert response.json()["version"] == before["version"] + 1
    conflict = await web.patch("/api/v1/settings/outbound-proxy", json=payload)
    assert conflict.status_code == 409
    audits = (await web.get("/api/v1/audit-logs", params={"action": "settings.outbound_proxy.update"})).json()
    assert len(audits["items"]) == 1
    event = (await web.get("/api/v1/audit-logs/" + audits["items"][0]["event_id"])).json()
    details = event["details"]
    assert details["mode"] == "manual" and details["version"] == 1
    assert "password" in details["changed_fields"]
    assert "test-only-secret" not in json.dumps(audits)
    async with state.db.session() as session:
        row = await session.get(SystemSetting, "outbound_proxy")
        assert "test-only-secret" not in json.dumps(row.value)
    response = await web.patch("/api/v1/settings/outbound-proxy", json={"password": "[REDACTED]"})
    assert response.status_code == 200
    assert (await outbound.load_config(state))["password"] == "test-only-secret"


async def test_connection_validation_errors_do_not_disclose_credentials(monkeypatch):
    config = manual(auth_type="basic", username="tester", password="test-only-secret")
    def client(*args, **kwargs):
        async def handle(request):
            return httpx.Response(407)
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(outbound, "build_client", client)
    with pytest.raises(ValueError, match="认证失败") as error:
        await outbound.validate_connection(config)
    assert "test-only-secret" not in str(error.value)


def test_env_mode_uses_only_process_variables_and_honors_no_proxy(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    config = outbound.normalize_config({"enabled": True, "mode": "env"})
    assert "proxy" not in outbound.client_options(config, "https://service.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://env.example:8080")
    options = outbound.client_options(config, "https://service.example")
    assert options == {"proxy": "http://env.example:8080", "trust_env": True}
    assert not {"transport", "mounts"} & options.keys()
    monkeypatch.setenv("NO_PROXY", "service.example")
    assert outbound.client_options(config, "https://service.example") == {"trust_env": False}


async def test_clients_reuse_pool_until_policy_changes():
    config = manual()
    created, closed = [], []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"
    class Client(httpx.AsyncClient):
        async def aclose(self):
            closed.append(len(closed))
            await super().aclose()
    def build(value, url, lib, **kwargs):
        created.append(value["mode"])
        return Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())))
    async def load():
        return config
    async with httpx.AsyncClient(transport=outbound.OutboundTransport(load, builder=build)) as client:
        first = await client.send(client.build_request("GET", "http://target.example"), stream=True)
        await client.get("http://target.example")
        assert created == ["manual"]
        assert not closed
        config = dict(outbound.DEFAULTS)
        await client.get("http://target.example")
        assert created == ["manual", "disabled"]
        assert not closed
        await first.aclose()
        assert len(closed) == 1
    assert len(closed) == 2


@pytest.mark.parametrize("module", [httpx, httpx2])
@pytest.mark.parametrize("scheme", ["http", "socks5", "socks5h"])
@pytest.mark.parametrize("secret", ["pass", ""])
async def test_real_proxy_authentication(module, scheme, secret):
    import asyncio

    seen = []
    async def handle(reader, writer):
        try:
            if scheme.startswith("socks"):
                assert await reader.readexactly(1) == b"\x05"
                count = (await reader.readexactly(1))[0]
                methods = await reader.readexactly(count)
                assert 2 in methods
                writer.write(b"\x05\x02")
                await writer.drain()
                assert await reader.readexactly(1) == b"\x01"
                size = (await reader.readexactly(1))[0]
                username = await reader.readexactly(size)
                size = (await reader.readexactly(1))[0]
                password = await reader.readexactly(size)
                seen.append((username, password))
                writer.write(b"\x01\x00")
                await writer.drain()
                header = await reader.readexactly(4)
                assert header == b"\x05\x01\x00\x03"  # Proxy resolves target hostname.
                size = (await reader.readexactly(1))[0]
                assert await reader.readexactly(size) == b"target.invalid"
                await reader.readexactly(2)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                await writer.drain()
            request = await reader.readuntil(b"\r\n\r\n")
            if scheme == "http":
                seen.append(request)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        config = manual(scheme=scheme, auth_type="basic", username="user", password=secret)
        config.update(host="127.0.0.1", port=server.sockets[0].getsockname()[1])
        async def load():
            return config
        async with outbound.get_outbound_client(module=module, loader=load, timeout=2) as client:
            response = await client.get("http://target.invalid/tool")
            assert response.content == b"ok"
        if scheme == "http":
            assert b"GET http://target.invalid/tool HTTP/1.1" in seen[0]
            import base64
            assert b"Proxy-Authorization: Basic " + base64.b64encode(("user:" + secret).encode()) in seen[0]
        else:
            assert seen == [(b"user", secret.encode())]
    finally:
        server.close()
        await server.wait_closed()


async def test_diagnostics_and_real_rest_requests_share_live_policy(db_app):
    import asyncio

    requests = []
    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        body = b'{"ok":true}'
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        web, state = db_app.web, db_app.app.state
        config = {"tools": [{"name": "ping", "inputSchema": {"type": "object"},
                             "request": {"url": "http://target.invalid/tool"}}]}
        saved = await web.patch("/api/v1/settings/outbound-proxy", json={
            "enabled": True, "mode": "manual", "host": "127.0.0.1",
            "port": server.sockets[0].getsockname()[1],
        })
        assert saved.status_code == 200
        diagnosis = await web.post("/api/v1/mcps/diagnose", json={"transport": "rest", "config": config})
        assert diagnosis.status_code == 200, diagnosis.text
        assert diagnosis.json()["tools"][0]["name"] == "ping"
        row = await state.catalog.create({
            "name": "Proxy fixture", "slug": "proxy-rest", "transport": "rest",
            "mode": "lazy", "isolation": "service", "config": config,
        }, user_id=db_app.actor["id"])
        lease = state.runtime.create_lease(db_app.actor["id"], "proxy-test")
        try:
            spec = await state.catalog.spec(row, db_app.actor["id"])
            await state.runtime.call(spec, lease.id, "ping", {})
            assert len(requests) >= 2
            proxied_count = len(requests)
            disabled = await web.patch("/api/v1/settings/outbound-proxy", json={"enabled": False})
            assert disabled.status_code == 200
            from mcp_manager.runtime import GatewayError
            with pytest.raises(GatewayError):
                await state.runtime.call(spec, lease.id, "ping", {})
            assert len(requests) == proxied_count
        finally:
            await state.runtime.release(lease.id)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("module", [httpx, httpx2])
@pytest.mark.parametrize("verification", ["default", "insecure", "custom_ca"])
async def test_tls_policy_also_applies_to_https_proxy(module, verification, tmp_path):
    import asyncio
    import ipaddress
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-proxy-test")])
    stamp = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(stamp - timedelta(days=1)).not_valid_after(stamp + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    ca_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca_path, key_path)
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        config = manual(scheme="https", ssl_verify=verification, custom_ca_path=str(ca_path))
        config.update(host="127.0.0.1", port=server.sockets[0].getsockname()[1])
        async def load():
            return config
        async with outbound.get_outbound_client(module=module, loader=load, timeout=2) as client:
            if verification == "default":
                with pytest.raises(module.ConnectError):
                    await client.get("http://target.invalid/tool")
            else:
                response = await client.get("http://target.invalid/tool")
                assert response.content == b"ok"


    finally:
        server.close()
        await server.wait_closed()


async def test_advisory_probe_redacts_unexpected_errors_and_rechecks_admin(db_app, monkeypatch):
    from mcp_manager.database import User

    async def unexpected(config):
        raise ValueError("http://user:private-proxy-password@host.invalid")
    monkeypatch.setattr(outbound, "validate_connection", unexpected)
    response = await db_app.web.post("/api/v1/settings/outbound-proxy/test", json={})
    assert response.status_code == 200 and response.json()["ok"] is False
    assert "private-proxy-password" not in response.text

    async def revoke(config):
        async with db_app.db.locked() as session:
            (await session.get(User, db_app.actor["id"])).role = "user"
    monkeypatch.setattr(outbound, "validate_connection", revoke)
    response = await db_app.web.post("/api/v1/settings/outbound-proxy/test", json={})
    assert response.status_code == 403
    assert (await db_app.web.get("/api/v1/settings/outbound-proxy")).status_code == 403
    assert (await db_app.web.patch("/api/v1/settings/outbound-proxy", json={})).status_code == 403


async def test_advisory_timeout_is_reported_as_timeout(monkeypatch):
    def client(*args, **kwargs):
        async def timeout(request):
            raise TimeoutError
        return httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    monkeypatch.setattr(outbound, "build_client", client)
    with pytest.raises(outbound.ProxyDiagnosticError, match="超时"):
        await outbound.validate_connection(manual())
