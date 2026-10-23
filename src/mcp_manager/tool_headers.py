"""Per-call schema handoff to HTTP transports; SDK owns protocol header encoding."""
import json
from contextvars import ContextVar

from mcp.shared.inbound import find_invalid_x_mcp_header, mcp_param_headers, x_mcp_header_map

from .runtime import GatewayError

# A mutable holder lets the dispatch authorizer replace a schema after startup.
tool_context = ContextVar("mmg_tool_context", default=None)


def validate_header_schema(schema):
    reason = find_invalid_x_mcp_header(schema)
    if reason is not None:
        raise GatewayError("invalid_arguments", "Invalid x-mcp-header annotation: " + reason)


async def mirror_tool_headers(request):
    context = tool_context.get()
    if not context or request.method != "POST":
        return
    version = request.headers.get("MCP-Protocol-Version", "")
    if version < "2026-07-28":
        return
    data = json.loads(request.content)
    if data.get("method") != "tools/call":
        return
    params = data.get("params") or {}
    if params.get("name") != context["name"]:
        return
    schema = context["tool"].get("inputSchema", {})
    validate_header_schema(schema)
    headers = mcp_param_headers(x_mcp_header_map(schema), params.get("arguments") or {})
    # Mutate only this request. Shared connection defaults must never carry call arguments.
    request.headers.update(headers)
