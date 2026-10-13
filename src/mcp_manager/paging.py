"""Signed paging and byte accounting for the actual MCP JSON-RPC envelope."""
import base64
import hashlib
import hmac
import json

from .runtime import GatewayError

MAX_MCP_BYTES = 1048576
ENVELOPE_RESERVE = 4096


def encode_cursor(offset, version, signing_key):
    key = signing_key.encode() if isinstance(signing_key, str) else signing_key
    raw = json.dumps({"version": version, "offset": offset}, separators=(",", ":")).encode()
    data = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return data + "." + hmac.new(key, data.encode(), hashlib.sha256).hexdigest()


def decode_cursor(cursor, version, signing_key):
    if not cursor:
        return 0
    key = signing_key.encode() if isinstance(signing_key, str) else signing_key
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError
        data, signature = cursor.split(".")
        expected = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
        saved = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if saved["version"] != version:
            raise GatewayError("catalog_changed", "Catalog, permissions or query changed; restart without cursor")
        if type(saved["offset"]) is not int or saved["offset"] < 0:
            raise ValueError
        return saved["offset"]
    except GatewayError:
        raise
    except (ValueError, KeyError, TypeError):
        raise GatewayError("invalid_cursor", "Invalid discovery cursor; restart without cursor") from None


def envelope_bytes(result, *, max_bytes=MAX_MCP_BYTES, request_id="x" * 128):
    value = result.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(result, "model_dump") else result
    envelope = {"jsonrpc": "2.0", "id": request_id, "result": value}
    size = 0
    for piece in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(envelope):
        size += len(piece.encode("utf-8"))
        if size > max_bytes:
            break
    return size


class McpPageBuilder:
    """Incremental assembly: one candidate definition at a time, never a whole catalog."""
    def __init__(self, *, total, offset, limit, version, signing_key, envelope, max_bytes=MAX_MCP_BYTES):
        if type(limit) is not int or limit < 1 or type(total) is not int or total < 0:
            raise ValueError("Invalid page size or total")
        if type(offset) is not int or offset < 0 or (offset >= total and offset != 0):
            raise GatewayError("invalid_cursor", "Cursor offset is outside the result")
        self.total, self.offset, self.limit = total, offset, limit
        self.version, self.signing_key, self.envelope = version, signing_key, envelope
        self.max_bytes = max_bytes
        self.items = []
        self.page = self._page([])
        self.result = envelope(self.page)
        if envelope_bytes(self.result, max_bytes=max_bytes) > max_bytes - ENVELOPE_RESERVE:
            raise GatewayError("catalog_envelope_too_large", "MCP response metadata exceeds its byte budget")

    def _page(self, items):
        following = encode_cursor(self.offset + len(items), self.version, self.signing_key) if (
            self.offset + len(items) < self.total
        ) else None
        return {"items": items, "total": self.total, "returned": len(items),
                "next_cursor": following, "catalog_version": self.version}

    def finish(self):
        if self.total > self.offset and not self.items:
            raise GatewayError("catalog_changed", "Catalog page disappeared during selection; restart without cursor")
        return self.result

    def add(self, item):
        if len(self.items) >= self.limit:
            return False
        candidate = [*self.items, item]
        page = self._page(candidate)
        result = self.envelope(page)
        if envelope_bytes(result, max_bytes=self.max_bytes) > self.max_bytes - ENVELOPE_RESERVE:
            if not self.items:
                raise GatewayError("catalog_entry_too_large", "One complete catalog contract exceeds the MCP envelope budget")
            return False
        self.items, self.page, self.result = candidate, page, result
        return True


def bounded_mcp_page(items, *, envelope, cursor, limit, version, signing_key, max_bytes=MAX_MCP_BYTES):
    offset = decode_cursor(cursor, version, signing_key)
    if cursor and offset >= len(items):
        raise GatewayError("invalid_cursor", "Cursor offset is outside the result")
    builder = McpPageBuilder(total=len(items), offset=offset, limit=limit, version=version,
                             signing_key=signing_key, envelope=envelope, max_bytes=max_bytes)
    for item in items[offset:offset + limit]:
        if not builder.add(item):
            break
    builder.finish()
    return builder.page
