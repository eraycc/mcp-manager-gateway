"""Real CONNECT/TLS regression for OAuth discovery and refresh, without external traffic."""
import asyncio
import contextlib
import ipaddress
import json
import ssl
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from mcp_manager import oauth_http, outbound


@pytest.fixture(params=["http", "https"])
async def oauth_proxy(request, tmp_path, monkeypatch):
    now = datetime.now(UTC)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test OAuth CA")])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(root_key, hashes.SHA256()))
    ca = tmp_path / "ca.pem"
    ca.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    sni, connects, requests, resolved = [], [], [], []

    def tls_context(name, names):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(root_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName(names), critical=False)
                .sign(root_key, hashes.SHA256()))
        cert_path, key_path = tmp_path / (name + ".pem"), tmp_path / (name + ".key")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        context.set_servername_callback(lambda sock, host, ctx: sni.append((name, host)))
        return context

    target_tls = tls_context("target", [x509.DNSName("mcp.example.test"), x509.DNSName("auth.example.test")])
    proxy_tls = tls_context("proxy", [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    resource, issuer = "https://mcp.example.test/mcp", "https://auth.example.test"

    async def endpoint(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            method, path, _ = lines[0].split()
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            body = await reader.readexactly(int(headers.get("Content-Length", "0")))
            requests.append((method, path, headers.get("Host"), parse_qs(body.decode())))
            status, extra = "200 OK", ""
            if path == "/mcp":
                status, value = "401 Unauthorized", {}
                extra = 'WWW-Authenticate: Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource"\r\n'
            elif path.startswith("/.well-known/oauth-protected-resource"):
                value = {"resource": resource, "authorization_servers": [issuer]}
            elif path == "/token":
                value = {"access_token": "renewed", "token_type": "Bearer", "expires_in": 3600}
            else:
                value = {"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
                         "token_endpoint": issuer + "/token", "code_challenge_methods_supported": ["S256"]}
            data = json.dumps(value).encode()
            writer.write((f"HTTP/1.1 {status}\r\n{extra}Content-Type: application/json\r\n"
                          f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    tasks = set()
    async def tunnel(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        remote_writer = None
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            connects.append(raw.split(b"\r\n")[0].decode())
            remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", target.sockets[0].getsockname()[1])
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            async def pump(source, destination):
                try:
                    while data := await source.read(65536):
                        destination.write(data)
                        await destination.drain()
                finally:
                    destination.close()
            await asyncio.gather(pump(reader, remote_writer), pump(remote_reader, writer))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            if remote_writer:
                remote_writer.close()
            tasks.discard(task)

    async def resolve(host, port, timeout):
        resolved.append(host)
        return ["8.8.8.8"]
    monkeypatch.setattr(oauth_http, "resolve_addresses", resolve)
    target = await asyncio.start_server(endpoint, "127.0.0.1", 0, ssl=target_tls)
    proxy = await asyncio.start_server(tunnel, "127.0.0.1", 0, ssl=proxy_tls if request.param == "https" else None)
    config = outbound.normalize_config({
        "enabled": True, "mode": "manual", "scheme": request.param,
        "host": "127.0.0.1", "port": proxy.sockets[0].getsockname()[1],
        "ssl_verify": "custom_ca", "custom_ca_path": str(ca),
        "list_mode": "whitelist", "list_entries": ["*.example.test"],
    })
    async def load():
        return config
    token = outbound._current_loader.set(load)
    try:
        yield config, connects, requests, sni, resolved
    finally:
        outbound._current_loader.reset(token)
        proxy.close()
        target.close()
        await proxy.wait_closed()
        await target.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)


@pytest.mark.parametrize("mode", ["manual", "env"])
async def test_proxy_discovers_authorization_and_refreshes_saved_token(db_app, oauth_proxy, monkeypatch, mode):
    config, connects, requests, sni, resolved = oauth_proxy
    if mode == "env":
        monkeypatch.setenv("HTTPS_PROXY", outbound.proxy_url(config))
        monkeypatch.setenv("NO_PROXY", "")
        config["mode"] = "env"
    # API middleware loads the actual settings; the direct refresh uses the same loader.
    from mcp_manager.database import SystemSetting
    state, user = db_app.app.state, db_app.actor["id"]
    async with db_app.db.locked() as session:
        session.add(SystemSetting(key="outbound_proxy", value=state.catalog.seal(config)))
    row = await state.catalog.create({
        "name": "Proxy OAuth", "transport": "streamable-http", "isolation": "user",
        "config": {"url": "https://mcp.example.test/mcp", "auth": {
            "type": "oauth", "auth_mode": "pre_registration", "client_id": "public"}},
    }, user_id=user)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 200, response.text
    authorization = urlsplit(response.json()["authorization_url"])
    assert authorization.netloc == "auth.example.test" and authorization.path == "/authorize"
    pending = state.oauth.pending[parse_qs(authorization.query)["state"][0]]
    async with db_app.db.locked() as session:
        await state.credentials.save_oauth_token(session, row, user, {
            "access_token": "old", "refresh_token": "refresh", "expires_at": 0,
        }, client=pending["client_binding"])
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")
    assert response.status_code == 200, response.text
    assert response.json()["authorized"] is True and response.json()["auth_status"] == "valid"
    payload = await state.credentials.load(row.id, user)
    assert payload["oauth_token"]["access_token"] == "renewed" and not payload.get("oauth_state")
    assert connects and set(connects) == {"CONNECT 8.8.8.8:443 HTTP/1.1"}
    assert {host for kind, host in sni if kind == "target"} == {"mcp.example.test", "auth.example.test"}
    assert {"mcp.example.test", "auth.example.test"} <= set(resolved)
    posts = [item for item in requests if item[0] == "POST"]
    assert len(posts) == 1 and posts[0][2] == "auth.example.test"
    assert posts[0][3]["grant_type"] == ["refresh_token"]


async def test_proxy_still_rejects_wrong_target_certificate(oauth_proxy):
    import httpx

    _, connects, requests, sni, _ = oauth_proxy
    async with httpx.AsyncClient(transport=oauth_http.OAuthHTTPTransport()) as client:
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            await client.get("https://wrong.example.test/mcp", extensions={"oauth_public_only": True})
    assert connects == ["CONNECT 8.8.8.8:443 HTTP/1.1"]
    assert ("target", "wrong.example.test") in sni
    assert not requests
