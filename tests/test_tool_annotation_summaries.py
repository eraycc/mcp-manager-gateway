"""Real service and approval summary APIs retain bounded upstream tool hints."""
from contextlib import asynccontextmanager

import pytest
from test_bug1_catalog import Connection
from test_proposal_atomicity import propose


@pytest.mark.parametrize("entry", ["service", "proposal"])
async def test_tool_annotations_survive_summary_and_exact_definition(db_app, entry, monkeypatch):
    fields = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    annotations = [
        {"readOnlyHint": False, "idempotentHint": False, "title": "Get repository details"},
        dict.fromkeys(fields, True) | {"title": "长标题" * 400, "extension": {"opaque": "preserved"}},
        None,
        dict(zip(fields, ("true", 1, [], {}), strict=True)) | {"title": 42},
        "invalid",
    ]
    tools = [
        {"name": f"tool_{index}", "inputSchema": {"type": "object"}, "annotations": value}
        for index, value in enumerate(annotations)
    ]
    tools.append({"name": "tool_missing", "inputSchema": {"type": "object"}})

    class AnnotatedConnection(Connection):
        async def discover(self):
            return {"tools": tools, "resources": [], "prompts": [], "templates": []}

    @asynccontextmanager
    async def connect(spec):
        yield AnnotatedConnection()

    db_app.app.state.runtime.connector = connect
    if entry == "service":
        result = await db_app.web.post("/api/v1/mcps", json={
            "name": "Annotated service", "config": {"command": "synthetic"},
        })
        assert result.status_code == 200, result.text
        root = "/api/v1/mcps/" + result.json()["id"] + "/capabilities"
    else:
        identity = await propose(db_app)
        result = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test?summary=true")
        assert result.status_code == 200, result.text
        root = f"/api/v1/mcp-proposals/{identity}/test-capabilities"

    result = await db_app.web.get(root, params={"kind": "tools", "limit": 50})
    assert result.status_code == 200, result.text
    items = result.json()["items"]
    assert items[0]["annotations"] == annotations[0]
    assert items[1]["annotations"] == dict.fromkeys(fields, True) | {
        "title": annotations[1]["title"][:256],
    }
    assert all("annotations" not in item for item in items[2:])
    assert all("inputSchema" not in item for item in items)
    for tool in tools:
        definition = await db_app.web.get(root + "/tools/" + tool["name"])
        assert definition.status_code == 200, definition.text
        assert definition.json() == tool

    async def forbidden_read(*args, **kwargs):
        raise AssertionError("Summary lists must not read full definition artifacts")

    monkeypatch.setattr(db_app.app.state.catalog.index.artifacts, "read_json", forbidden_read)
    result = await db_app.web.get(root, params={"kind": "tools", "limit": 50})
    assert result.status_code == 200, result.text
    assert result.json()["items"] == items
