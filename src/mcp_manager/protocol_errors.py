"""Separate MCP request rejections from errors that require connection recovery."""
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR, REQUEST_TIMEOUT


def is_request_error(exc: Exception) -> bool:
    """Recognize a peer rejection that does not invalidate its connection.

    The SDK also synthesizes MCPError for disconnects and timeouts, and uses
    INVALID_REQUEST/INTERNAL_ERROR/PARSE_ERROR for HTTP session loss, failed
    responses and malformed transport messages. Keep those ambiguous failures
    on the existing recovery path. Unknown non-MCP exceptions stay there too.
    """
    return isinstance(exc, MCPError) and exc.code not in {
        CONNECTION_CLOSED, REQUEST_TIMEOUT, INVALID_REQUEST, INTERNAL_ERROR, PARSE_ERROR,
    }


def is_connection_failure(exc: BaseException) -> bool:
    """Require transport evidence; business and response validation errors are not liveness failures."""
    import anyio
    import httpx
    import httpx2

    if isinstance(exc, BaseExceptionGroup):
        return any(is_connection_failure(item) for item in exc.exceptions)
    if isinstance(exc, (ConnectionError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError,
                        httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.CloseError,
                        httpx.RemoteProtocolError, httpx2.ConnectError, httpx2.ReadError, httpx2.WriteError,
                        httpx2.CloseError, httpx2.RemoteProtocolError)):
        return True
    if isinstance(exc, MCPError):
        return exc.code == CONNECTION_CLOSED
    cause = exc.__cause__
    return cause is not None and cause is not exc and is_connection_failure(cause)
