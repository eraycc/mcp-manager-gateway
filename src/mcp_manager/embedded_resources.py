"""Bounded, process-local snapshots of tool-embedded resources, isolated by session."""
import copy
import json
import secrets
import time
from collections import OrderedDict

from mcp import types


class EmbeddedResources:
    ttl = 1800
    max_item_bytes = 4 * 1024 * 1024
    max_session_bytes = 16 * 1024 * 1024
    max_total_bytes = 64 * 1024 * 1024
    max_session_items = 128
    max_total_items = 4096

    def __init__(self):
        self.items = OrderedDict()
        self.bytes = 0

    def _remove(self, uri):
        item = self.items.pop(uri)
        self.bytes -= item["bytes"]

    def prune(self):
        now = time.monotonic()
        for uri, item in list(self.items.items()):
            if item["expires"] <= now:
                self._remove(uri)

    def release(self, namespace):
        for uri, item in list(self.items.items()):
            if item["namespace"] == namespace:
                self._remove(uri)

    def capture(self, namespace, scope, result):
        self.prune()
        retained, skipped = [], 0
        for block in result.content:
            if not isinstance(block, types.EmbeddedResource):
                continue
            content = block.resource.model_dump(mode="json", by_alias=True, exclude_none=True)
            size = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
            if size > self.max_item_bytes:
                skipped += 1
                continue
            same = [uri for uri, item in self.items.items() if item["namespace"] == namespace]
            while same and (len(same) >= self.max_session_items or
                            sum(self.items[uri]["bytes"] for uri in same) + size > self.max_session_bytes):
                self._remove(same.pop(0))
            while self.items and (self.bytes + size > self.max_total_bytes or len(self.items) >= self.max_total_items):
                self._remove(next(iter(self.items)))
            uri = "mcp-manager://" + scope.server_id + "/_embedded/" + secrets.token_urlsafe(24)
            self.items[uri] = {
                "namespace": namespace, "scope": scope, "content": content,
                "expires": time.monotonic() + self.ttl, "bytes": size,
            }
            self.bytes += size
            retained.append(uri)
        return retained, skipped

    def visible(self, namespace, scopes):
        self.prune()
        if namespace is None:
            return []
        return [(uri, item) for uri, item in self.items.items()
                if item["namespace"] == namespace and scopes.get(item["scope"].server_id) == item["scope"]]

    def read(self, namespace, scope, uri):
        self.prune()
        item = self.items.get(uri)
        if item is None or namespace is None or item["namespace"] != namespace or item["scope"] != scope:
            return None
        return {"contents": [dict(copy.deepcopy(item["content"]), uri=uri)]}

    def summaries(self, namespace, scopes):
        return [(item["scope"].server_id, {
            "uri": uri, "name": item["content"]["uri"].rsplit("/", 1)[-1] or "embedded resource",
            "mimeType": item["content"].get("mimeType", "application/octet-stream"),
            "source_uri": item["content"]["uri"], "temporary": True,
        }) for uri, item in self.visible(namespace, scopes)]
