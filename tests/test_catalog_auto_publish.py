"""New services must publish the same directory that the console reads."""
import json
from contextlib import asynccontextmanager

import pytest
from test_proposal_atomicity import propose

from mcp_manager.logs import LogStore

CAPABILITIES = {
    "tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
    "resources": [{"name": "health", "uri": "device://health"}],
    "prompts": [{"name": "status"}],
    "templates": [{"name": "sensor", "uriTemplate": "device://sensor/{key}"}],
}


@pytest.mark.parametrize("rebuilt", [False, True])
async def test_log_preview_ends_at_its_own_record(tmp_path, rebuilt):
    logs = LogStore(tmp_path)
    try:
        await logs.append({"id": "first", "status": "success", "tool_name": "中文"})
        await logs.append({"id": "second", "status": "success", "tool_name": "other"})
        if rebuilt:
            await logs.rebuild()
        preview = await logs.preview("first")
        assert not preview["truncated"]
        assert json.loads(preview["preview"]) == await logs.detail("first")
        assert len(preview["preview"].encode("utf-8")) == preview["size_bytes"]
        assert preview["preview"].rstrip().endswith("}")
    finally:
        await logs.close()


@pytest.mark.parametrize("path", ["create", "import", "copy", "approve", "batch-approve"])
async def test_service_entrypoint_publishes_all_capabilities(db_app, monkeypatch, path):
    @asynccontextmanager
    async def connector(spec):
        class Peer:
            async def discover(self):
                return CAPABILITIES
        yield Peer()

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    config = {"name": "Auto catalog", "slug": "auto_catalog", "transport": "stdio",
              "config": {"command": "synthetic"}, "mode": "lazy"}
    if path == "import":
        response = await db_app.web.post("/api/v1/mcps/import", json={"data": [config]})
        assert response.status_code == 200, response.text
        server_id = response.json()["results"][0]["id"]
    elif path in {"approve", "batch-approve"}:
        identity = await propose(db_app, name=config["name"], config=config["config"])
        if path == "approve":
            response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/approve", json={})
            assert response.status_code == 200, response.text
        else:
            response = await db_app.web.post("/api/v1/mcp-proposals/batch", json={"ids": [identity], "action": "approve"})
            assert response.status_code == 200 and response.json()["results"][0]["ok"], response.text
        proposal = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
        server_id = proposal["approved_mcp_id"]
    else:
        response = await db_app.web.post("/api/v1/mcps", json=config)
        assert response.status_code == 200, response.text
        server_id = response.json()["id"]
        if path == "copy":
            response = await db_app.web.post(f"/api/v1/mcps/{server_id}/copy")
            assert response.status_code == 200, response.text
            server_id = response.json()["id"]

    detail = (await db_app.web.get(f"/api/v1/mcps/{server_id}")).json()
    assert detail["cache_status"] == "ready" and detail["tool_count"] == 1
    listed = (await db_app.web.get("/api/v1/mcps")).json()["items"]
    assert next(row for row in listed if row["id"] == server_id)["tool_count"] == 1
    for kind in CAPABILITIES:
        response = await db_app.web.get(f"/api/v1/mcps/{server_id}/capabilities", params={"kind": kind})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1, (path, kind, response.text)
        assert response.json()["generation"] and not response.json()["refresh_required"]


@pytest.mark.parametrize("isolation,mode", [("user", "lazy"), ("session", "lazy"), ("service", "eager")])
async def test_automatic_catalog_respects_owner_and_instance_lifecycle(db_app, monkeypatch, isolation, mode):
    calls = []
    @asynccontextmanager
    async def connector(spec):
        calls.append(spec.id)
        class Peer:
            async def discover(self):
                return CAPABILITIES
        yield Peer()
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    response = await db_app.web.post("/api/v1/mcps", json={
        "name": "Scoped service", "isolation": isolation, "mode": mode,
        "config": {"command": "synthetic"},
    })
    assert response.status_code == 200, response.text
    row = response.json()
    response = await db_app.web.get(f'/api/v1/mcps/{row["id"]}/capabilities')
    assert response.json()["total"] == 1
    assert calls == [row["id"]]
    assert bool(db_app.app.state.runtime.status()) == (mode == "eager")
