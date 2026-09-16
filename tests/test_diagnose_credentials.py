"""Saved-service diagnosis restores scoped credentials without leaking them."""
from contextlib import asynccontextmanager

import httpx

from mcp_manager.app import create_app
from mcp_manager.config import Settings


class DiagnosisConnection:
    def __init__(self, case):
        self.case = case

    async def discover(self):
        token = self.case["token"]
        if self.case["status"]:
            request = httpx.Request(
                "GET", "https://mcp.test", headers={"Authorization": f"Bearer {token}"}
            )
            response = httpx.Response(self.case["status"], request=request)
            raise httpx.HTTPStatusError(
                f"Authorization Bearer {token} failed",
                request=request,
                response=response,
            )
        return {"tools": [], "resources": [], "prompts": [], "templates": []}


async def test_saved_user_bearer_diagnosis_restores_actor_redaction(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="diagnosis"))
    async with app.router.lifespan_context(app):
        seen = {"token": None, "status": None}

        @asynccontextmanager
        async def connect(spec):
            seen["token"] = spec.config["auth"]["token"]
            yield DiagnosisConnection(seen)

        app.state.runtime.connector = connect
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as web:
            await web.post(
                "/api/v1/auth/register",
                json={"username": "admin", "password": "password12345"},
            )
            await web.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "password12345"},
            )
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            created = await web.post("/api/v1/mcps", json={
                "name": "Diagnosis",
                "transport": "streamable-http",
                "mode": "disabled",
                "isolation": "user",
                "config": {
                    "url": "https://mcp.test/mcp",
                    "auth": {"type": "bearer", "token": "actor-secret"},
                },
            })
            row = created.json()
            response = await web.post("/api/v1/mcps/diagnose", json={
                "server_id": row["id"],
                "revision": row["revision"],
                "transport": row["transport"],
                "isolation": "user",
                "config": {
                    "url": "https://mcp.test/mcp",
                    "auth": {"type": "bearer", "token": "[REDACTED]"},
                },
            })
            assert response.status_code == 200, response.text
            assert seen["token"] == "actor-secret"

            stale = await web.post("/api/v1/mcps/diagnose", json={
                "server_id": row["id"],
                "revision": row["revision"] - 1,
                "transport": row["transport"],
                "isolation": "user",
                "config": row["config"],
            })
            assert stale.status_code == 409


async def test_diagnosis_error_preserves_safe_status_and_removes_secret(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="diagnosis-error"))
    async with app.router.lifespan_context(app):
        case = {"token": None, "status": 401}

        @asynccontextmanager
        async def connect(spec):
            case["token"] = spec.config["auth"]["token"]
            yield DiagnosisConnection(case)

        app.state.runtime.connector = connect
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as web:
            await web.post(
                "/api/v1/auth/register",
                json={"username": "admin", "password": "password12345"},
            )
            await web.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "password12345"},
            )
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            row = (await web.post("/api/v1/mcps", json={
                "name": "Diagnosis",
                "transport": "streamable-http",
                "mode": "disabled",
                "isolation": "user",
                "config": {
                    "url": "https://mcp.test/mcp",
                    "auth": {"type": "bearer", "token": "actor-secret"},
                },
            })).json()
            response = await web.post("/api/v1/mcps/diagnose", json={
                "server_id": row["id"],
                "revision": row["revision"],
                "transport": row["transport"],
                "isolation": "user",
                "config": {
                    "url": "https://mcp.test/mcp",
                    "auth": {"type": "bearer", "token": "[REDACTED]"},
                },
            })
            assert response.status_code == 502
            assert response.json()["downstream_status"] == 401
            assert response.json()["error_type"] == "HTTPStatusError"
            assert "actor-secret" not in response.text
            assert "Authorization" not in response.text
