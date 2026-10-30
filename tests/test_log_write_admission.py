"""Permanent retention does not bypass shared free-space and spool admission."""
import pytest

from mcp_manager.logs import LogStore
from mcp_manager.runtime import GatewayError


async def test_calls_payloads_and_audits_refuse_before_creating_sources_when_disk_floor_is_exceeded(tmp_path):
    logs = LogStore(tmp_path)
    try:
        logs.admission.set_policy({"min_free_bytes": 2**50})
        for event in ({"status": "running"}, {"status": "success", "result": "x" * 100000}):
            with pytest.raises(GatewayError) as failure:
                await logs.append(event)
            assert failure.value.code == "storage_low_space"
        with pytest.raises(GatewayError):
            await logs.audit("settings.update", None, {}, require=True)
        assert not list(logs.calls_root.glob("????-??-??.jsonl"))
        assert not list(logs.audits.root.glob("????-??-??.jsonl"))
        assert not list(logs.payload_root.glob("*/*/*.json"))
        assert not list(logs.calls_root.glob("*.tmp"))
        assert logs.admission.usage() == {"slots": 0, "bytes": 0}
    finally:
        await logs.close()
