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
