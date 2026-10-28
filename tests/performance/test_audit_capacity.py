"""Opt-in stress input retained from the ordinary audit-boundary regression."""
from mcp_manager.logs import LogStore


async def test_audit_hundred_thousand_character_input_is_bounded(tmp_path):
    logs = LogStore(tmp_path)
    try:
        identity = await logs.audit("mcp.update", "u", {"mcp_id": "x", "name": "z" * 100_000})
        assert (await logs.audits.detail(identity))["details"]["name"] == "z" * 4096
        assert (await logs.audits.status())["events"] == 1
    finally:
        await logs.close()
