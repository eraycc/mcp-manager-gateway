"""Opt-in acceptance with a fresh Codex process and a temporary gateway."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from test_gateway import running_gateway  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("MCP_LIVE_CODEX") != "1", reason="Live Codex acceptance not requested")


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("mode", ["native", "discovery"])
async def test_codex_finds_and_calls_gateway(running_gateway, tmp_path, transport, mode):  # noqa: F811
    _app, web, url, _old_token, row = running_gateway
    original = _app.middleware_stack
    wire_events = []
    async def observe(scope, receive, send):
        async def capture(message):
            if scope.get("path") == "/mcp" and message["type"] == "http.response.start":
                wire_events.append((scope["method"], message["status"]))
            await send(message)
        await original(scope, receive, capture)
    _app.middleware_stack = observe
    token = (await web.post("/api/v1/tokens", json={
        "name": "codex-smoke", "discovery_mode": mode, "scope_mode": "all"})).json()["token"]
    marker = "CODEX_MCP_" + transport.upper() + "_" + mode.upper()
    project = str(Path(__file__).resolve().parents[1])
    args = ["C:/node/nodejs/node.exe", "C:/node/nodejs/node_modules/@openai/codex/bin/codex.js",
            "exec", "--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check",
            "-C", str(tmp_path), "-s", "read-only", "-m", "gpt-6-astra",
            "-c", 'model_reasoning_effort="low"']
    settings = ({"url": url.replace("127.0.0.1", "localhost") + "/mcp", "bearer_token_env_var": "MCP_MANAGER_TOKEN"} if transport == "http" else {
        "command": "uv", "args": ["run", "--frozen", "--project", project, "python", "-m",
                                 "mcp_manager.cli", "stdio", "--url", url],
        "env_vars": ["MCP_MANAGER_TOKEN"]})
    settings.update(required=True, startup_timeout_sec=30, experimental_environment="local")
    # Only fixture echo execution is pre-approved for this explicitly requested acceptance run.
    settings["tools.echo__echo.approval_mode"] = "approve"
    settings["tools.gateway_call.approval_mode"] = "approve"
    for key, value in settings.items():
        args.extend(["-c", "mcp_servers.acceptance." + key + "=" + json.dumps(value)])
    if mode == "discovery":
        prompt = (
            "This is an MCP gateway acceptance test. Use only the acceptance MCP tools. "
            "Call gateway_search_mcps with query='*', verify echo is listed and its tools_list contains echo, "
            "then call gateway_search_tools with mcp='echo' and tool='echo'. Read the returned original "
            "inputSchema. Call gateway_call using the exact returned gateway_name "
            "and arguments containing value='" + marker + "'. Return the echoed text. "
            "Do not use shell, read files, or fabricate a successful call.")
    else:
        prompt = (
            "This is an MCP gateway acceptance test. Use the acceptance MCP echo__echo tool, "
            "whose schema is already listed, with value='" + marker + "'. Return the echoed text. "
            "Do not use shell, read files, or fabricate a successful call.")
    env = dict(os.environ, MCP_MANAGER_TOKEN=token)
    # A fresh CLI must not inherit this parent agent session identity.
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        env.pop(key, None)
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, [env.get(key, ""), "127.0.0.1", "localhost", "::1"]))
    process = await asyncio.create_subprocess_exec(
        *args, prompt, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    output = stdout.decode("utf-8", errors="replace")
    diagnostic = stderr.decode("utf-8", errors="replace")[-1500:]
    assert process.returncode == 0, (diagnostic, wire_events)
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert any(event.get("type") == "turn.completed" for event in events), output[-2000:]
    logs = (await web.get("/api/v1/logs", params={"mcp_id": row["id"]})).json()
    successful = [item for item in logs["items"] if item["tool_name"] == "echo" and item["status"] == "success"]
    assert successful, (output[-2500:] + "\nSTDERR:\n" + diagnostic).replace(token, "[REDACTED]")
    detail = (await web.get("/api/v1/logs/" + successful[0]["id"])).json()
    assert detail["arguments"] == {"value": marker}, detail
