"""Uvicorn entry point and scoped Windows asyncio teardown compatibility."""
import sys
from contextlib import contextmanager

import uvicorn


def _call_connection_lost(self, exc):
    """Finish Proactor cleanup even when a peer resets during shutdown.

    CPython 3.12-3.14 can raise from socket.shutdown() before detaching
    the transport, leaving asyncio.Server.wait_closed() pending forever.
    Keep protocol errors visible and suppress resets only at shutdown().
    """
    if self._called_connection_lost:
        return
    try:
        self._protocol.connection_lost(exc)
    finally:
        try:
            if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
                import socket

                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except ConnectionResetError:
                    pass  # The peer is already gone; still close and detach.
        finally:
            try:
                self._sock.close()
            finally:
                self._sock = None
                server = self._server
                if server is not None:
                    # Python 3.13 started tracking transports instead of a count.
                    if sys.version_info >= (3, 13):
                        server._detach(self)
                    else:
                        server._detach()
                    self._server = None
                self._called_connection_lost = True


@contextmanager
def windows_connection_cleanup():
    """Apply the compatibility fix only while MMG owns the server process."""
    if sys.platform != "win32":
        yield
        return

    from asyncio.proactor_events import _ProactorBasePipeTransport

    original = _ProactorBasePipeTransport._call_connection_lost
    _ProactorBasePipeTransport._call_connection_lost = _call_connection_lost
    try:
        yield
    finally:
        _ProactorBasePipeTransport._call_connection_lost = original


def serve(app, *, host, port):
    with windows_connection_cleanup():
        uvicorn.run(app, host=host, port=port, workers=1, timeout_graceful_shutdown=10)
