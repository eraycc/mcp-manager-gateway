"""Console login follows its actual deployment address, not a fixed PUBLIC_URL."""
import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings


@pytest.mark.parametrize("url", ["http://localhost:8765", "http://192.168.2.111:8765",
                                "https://manager.example", "http://[::1]:8765"])
async def test_console_register_login_and_write_on_deployment_origin(tmp_path, url):
    app = create_app(Settings(data_dir=tmp_path, secret_key="origin-test",
                              public_url="http://127.0.0.1:8765"))
    del app.state.protocol
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=url,
                                     headers={"Origin": url}) as web:
            credentials = {"username": "admin", "password": "password12345"}
            assert (await web.post("/api/v1/auth/register", json=credentials)).status_code == 200
            assert (await web.post("/api/v1/auth/login", json=credentials)).status_code == 200
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            assert (await web.patch("/api/v1/me", json={"email": "test@example.com"})).status_code == 200
            assert (await web.patch("/api/v1/me", json={"email": ""}, headers={
                "Origin": "https://unrelated.example"})).status_code == 403
            assert (await web.patch("/api/v1/me", json={"email": ""}, headers={
                "X-CSRF-Token": ""})).status_code == 403
            assert (await web.post("/api/v1/auth/logout")).status_code == 200
