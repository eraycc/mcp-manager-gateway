"""Downstream responses are read incrementally under hard byte budgets."""
import pytest

from mcp_manager.runtime import GatewayError
from mcp_manager.transports import read_response_with_budget


class StreamResponse:
    def __init__(self, chunks, *, declared=None):
        self.headers = {}
        if declared is not None:
            self.headers["content-length"] = str(declared)
        self._chunks = chunks
        self.iterated = 0

    async def aiter_bytes(self):
        for chunk in self._chunks:
            self.iterated += 1
            yield chunk


@pytest.mark.asyncio
async def test_response_budget_rejects_declared_and_streamed_oversize():
    declared = StreamResponse([], declared=9)
    with pytest.raises(GatewayError) as error:
        await read_response_with_budget(declared, max_bytes=8)
    assert error.value.code == "outcome_unknown"
    assert declared.iterated == 0

    streamed = StreamResponse([b"1234", b"5678", b"9"])
    with pytest.raises(GatewayError) as error:
        await read_response_with_budget(streamed, max_bytes=8)
    assert error.value.code == "outcome_unknown"
    assert streamed.iterated == 3

    accepted = StreamResponse([b"1234", b"5678"])
    assert await read_response_with_budget(accepted, max_bytes=8) == b"12345678"


@pytest.mark.asyncio
async def test_media_budget_accounts_for_base64_expansion():
    raw_limit = 8 * 1024 * 1024 * 3 // 4
    accepted = StreamResponse([b"x" * raw_limit])
    raw = await read_response_with_budget(accepted, max_bytes=raw_limit)
    assert len(raw) == raw_limit

    oversized = StreamResponse([b"x" * (raw_limit + 1)])
    with pytest.raises(GatewayError) as error:
        await read_response_with_budget(oversized, max_bytes=raw_limit)
    assert error.value.code == "outcome_unknown"
