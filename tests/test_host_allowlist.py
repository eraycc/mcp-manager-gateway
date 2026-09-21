import httpx

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.cors import host_allowed, validate_hosts


def test_host_allowlist_supports_wildcards_hosts_ips_and_ports():
    assert validate_hosts(["*"]) == ["*"]
    assert validate_hosts(["0.0.0.0"]) == ["0.0.0.0"]
    assert host_allowed("192.168.2.111:8765", ["192.168.2.111"])
    assert host_allowed("manager.example:8765", ["manager.example:8765"])
    assert not host_allowed("manager.example:9000", ["manager.example:8765"])
    assert host_allowed("[::1]:8765", ["::1"])
    assert host_allowed("anything.example:1234", ["0.0.0.0"])


async def test_gateway_host_allowlist_rejects_clearly_but_console_keeps_existing_logic(tmp_path):
    app = create_app(Settings(
        data_dir=tmp_path,
        secret_key="host-test",
        public_url="http://127.0.0.1:8765",
    ))
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://192.168.2.111:8765"
        ) as web:
            credentials = {"username": "admin", "password": "password12345"}
            assert (await web.post("/api/v1/auth/register", json=credentials)).status_code == 200
            assert (await web.post("/api/v1/auth/login", json=credentials)).status_code == 200
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            saved = await web.patch("/api/v1/settings", json={"allowed_hosts": ["127.0.0.1"]})
            assert saved.status_code == 200, saved.text
            assert saved.json()["allowed_hosts"] == ["127.0.0.1"]
            rejected = await web.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "ping"
            })
            assert rejected.status_code == 403
            assert rejected.json() == {
                "detail": "Host is not allowed",
                "host": "192.168.2.111:8765",
                "allowed_hosts": ["127.0.0.1"],
            }
            # Management UI/API keeps its current same-origin behavior.
            assert (await web.get("/api/v1/bootstrap")).status_code == 200

            saved = await web.patch("/api/v1/settings", json={"allowed_hosts": ["0.0.0.0"]})
            assert saved.status_code == 200, saved.text
            allowed = await web.post("/mcp", json={
                "jsonrpc": "2.0", "id": 2, "method": "ping"
            })
            assert allowed.status_code == 401
