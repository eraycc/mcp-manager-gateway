"""A saved source offset must never read a different event after compaction."""
import json

import pytest

from mcp_manager.logs import LogStore
from mcp_manager.runtime import GatewayError


async def test_download_rejects_a_reference_from_before_source_rewrite(tmp_path):
    logs = LogStore(tmp_path)
    try:
        for identity in ("removed", "keep", "other"):
            await logs.append({"id": identity, "user_id": "alice", "status": "success",
                               "timestamp": "2026-10-30T00:00:00+00:00"})
        reference = await logs.reference("keep", user_id="alice")
        await logs.delete(["removed"])
        with pytest.raises(GatewayError) as failure:
            _ = b"".join([part async for part in logs.download_chunks(reference)])
        assert failure.value.code == "log_reference_changed"
        fresh = await logs.reference("keep", user_id="alice")
        raw = b"".join([part async for part in logs.download_chunks(fresh)])
        assert json.loads(raw)["id"] == "keep"
    finally:
        await logs.close()


async def test_all_historical_events_pin_their_large_payloads(tmp_path):
    logs = LogStore(tmp_path)
    try:
        for day, text in [(29, "first"), (30, "last")]:
            await logs.append({"id": "same-call", "user_id": "alice", "status": "success",
                "timestamp": f"2026-10-{day}T00:00:00+00:00", "result": text * 20000})
        assert logs.conn.execute("SELECT count(*) FROM call_payload_roots").fetchone()[0] == 2
        assert logs.conn.execute("SELECT count(*) FROM payloads").fetchone()[0] == 1
        assert logs.conn.execute("SELECT count(*) FROM call_event_sources WHERE id='same-call'").fetchone()[0] == 2
    finally:
        await logs.close()
