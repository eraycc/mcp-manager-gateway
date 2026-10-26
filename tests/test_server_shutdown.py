"""Connection reset must not strand the Windows server during Ctrl+C."""
import asyncio
import errno
import sys
from types import SimpleNamespace

import pytest
import uvicorn

from mcp_manager import cli
from mcp_manager.server import windows_connection_cleanup


class TransportState:
    def __init__(self, **values):
        self.__dict__.update(values)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor regression")
@pytest.mark.parametrize("failure", [None, ConnectionResetError(10054, "peer reset")])
async def test_reset_connection_does_not_block_server_close(failure):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    listener = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    closed = []
    notified = []
    transport = TransportState(
        _called_connection_lost=False,
        _protocol=SimpleNamespace(connection_lost=lambda exc: notified.append(exc)),
        _server=listener,
    )

    def shutdown(how):
        if failure:
            raise failure

    transport._sock = SimpleNamespace(
        shutdown=shutdown, fileno=lambda: 1, close=lambda: closed.append(True),
    )
    if sys.version_info >= (3, 13):
        listener._attach(transport)
    else:
        listener._attach()
    try:
        with windows_connection_cleanup():
            try:
                _ProactorBasePipeTransport._call_connection_lost(transport, None)
            except ConnectionResetError:
                pass  # The assertion below detects the original incomplete cleanup.
            listener.close()
            await asyncio.wait_for(listener.wait_closed(), 0.3)
            _ProactorBasePipeTransport._call_connection_lost(transport, None)
        assert closed == [True]
        assert notified == [None]
        assert transport._sock is None
        assert transport._server is None
    finally:
        listener.close()
        if transport._server is not None:
            if sys.version_info >= (3, 13):
                listener._detach(transport)
            else:
                listener._detach()
        await listener.wait_closed()


@pytest.mark.parametrize("command", [[], ["serve"]])
def test_cli_gives_connections_a_finite_shutdown_deadline(tmp_path, monkeypatch, command):
    observed = {}

    def run(app, **kwargs):
        observed.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(sys, "argv", ["mmg", "--home", str(tmp_path / "home"), *command])
    cli.main()
    assert 0 < observed.get("timeout_graceful_shutdown", float("inf")) <= 30


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor regression")
@pytest.mark.parametrize("source", ["protocol", "shutdown"])
def test_unexpected_errors_remain_visible_and_cleanup_is_restored(source):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    original = _ProactorBasePipeTransport._call_connection_lost
    failure = ConnectionResetError("protocol failure") if source == "protocol" else OSError(errno.EACCES, "denied")
    closed = []

    def fail():
        raise failure

    transport = TransportState(
        _called_connection_lost=False,
        _protocol=SimpleNamespace(connection_lost=lambda exc: fail() if source == "protocol" else None),
        _sock=SimpleNamespace(
            shutdown=lambda how: fail() if source == "shutdown" else None,
            fileno=lambda: 1, close=lambda: closed.append(True),
        ),
        _server=None,
    )
    with pytest.raises(type(failure)) as caught, windows_connection_cleanup():
        _ProactorBasePipeTransport._call_connection_lost(transport, None)
    assert caught.value is failure
    assert closed == [True]
    assert transport._sock is None
    assert transport._called_connection_lost
    assert _ProactorBasePipeTransport._call_connection_lost is original
