"""The gateway preserves upstream tool schema and annotation semantics."""
from test_progressive_discovery import SCHEMA, discovery_env, payload  # noqa: F401


async def test_discovery_returns_upstream_tool_fields_without_normalization(discovery_env):  # noqa: F811
    *_, invoke, _web = discovery_env
    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "send_message"}))
    tool = body["tools"][0]

    assert tool["title"] == "Notification sender"
    assert tool["description"] == "Send a notification"
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {"recipient": {
            "type": "string",
            "description": "destination email address",
        }},
        "required": ["recipient"],
    }
    assert tool["outputSchema"] == {
        "type": "object",
        "properties": {"sent": {"type": "boolean"}},
    }
    assert tool["annotations"] == {"readOnlyHint": False}
    assert "additionalProperties" not in tool["inputSchema"]
    assert "effective_annotations" not in tool
    assert "annotations_inferred" not in tool
    assert "argument_policy" not in tool


async def test_upstream_schema_alone_controls_extra_arguments(discovery_env):  # noqa: F811
    _app, _rows, calls, _ctx, invoke, _web = discovery_env

    open_result = await invoke("gateway_call", {
        "name": "files__send_message",
        "arguments": {"recipient": "ops@example.test", "upstream_extension": "kept"},
    })
    assert not open_result.is_error
    assert calls[-1] == (
        "files",
        "send_message",
        {"recipient": "ops@example.test", "upstream_extension": "kept"},
    )

    before = list(calls)
    strict_result = await invoke("gateway_call", {
        "name": "files__read_file",
        "arguments": {"path": "example.txt", "unexpected": True},
    })
    assert strict_result.is_error
    assert strict_result.structured_content["error"]["code"] == "invalid_arguments"
    assert calls == before

    wrapper_result = await invoke("gateway_call", {
        "name": "files__send_message",
        "recipient": "ops@example.test",
    })
    assert wrapper_result.is_error
    assert wrapper_result.structured_content["error"]["code"] == "invalid_request"
    assert calls == before


async def test_strict_schema_is_returned_exactly(discovery_env):  # noqa: F811
    *_, invoke, _web = discovery_env
    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    assert body["tools"][0]["inputSchema"] == SCHEMA
