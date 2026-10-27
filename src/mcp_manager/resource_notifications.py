"""Resource list invalidation across modern subscriptions and legacy sessions."""
import asyncio
import logging
from weakref import WeakSet

from mcp.server.lowlevel import Server
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    PromptsListChanged,
    ResourcesListChanged,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

logger = logging.getLogger(__name__)


class ResourceServer(Server):
    def get_capabilities(self, *args, **kwargs):
        capabilities = super().get_capabilities(*args, **kwargs)
        if capabilities.resources is not None:
            capabilities.resources.list_changed = True
        if capabilities.prompts is not None:
            capabilities.prompts.list_changed = True
        return capabilities


class ResourceNotifications:
    def __init__(self):
        self.bus = InMemorySubscriptionBus()
        self.listen = ListenHandler(self.bus)
        self.connections = WeakSet()

    async def track_legacy(self, ctx, call_next):
        result = await call_next(ctx)
        if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            # SDK 2.2 creates a request proxy; retain the connection, not that
            # proxy's short-lived response channel. Teardown removes it.
            connection = ctx.session._connection
            if connection.initialized.is_set() and connection not in self.connections:
                self.connections.add(connection)
                connection.exit_stack.callback(self.connections.discard, connection)
        return result

    async def _notify_legacy(self, connection, event):
        try:
            async with asyncio.timeout(2):
                if isinstance(event, ResourcesListChanged):
                    await connection.send_resource_list_changed()
                else:
                    await connection.send_prompt_list_changed()
        except (TimeoutError, OSError):
            logger.warning("Resource list notification could not reach a disconnected client")

    async def publish(self, event):
        await self.bus.publish(event)
        await asyncio.gather(*(self._notify_legacy(connection, event) for connection in tuple(self.connections)))

    async def changed(self):
        await self.publish(ResourcesListChanged())
        await self.publish(PromptsListChanged())
