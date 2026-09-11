from contextlib import asynccontextmanager

import pytest

from mcp_manager.runtime import Runtime, ServerSpec
from mcp_manager.transports import PLUGINS, connect, register_transport, validate_config


async def test_plugin_uses_shared_lifecycle():
    class Plugin:
        starts = 0
        closes = 0
        @staticmethod
        def validate(config):
            if config.get("version") != 1:
                raise ValueError("version required")
        @classmethod
        @asynccontextmanager
        async def connect(cls, spec):
            cls.starts += 1
            try:
                yield cls()
            finally:
                cls.closes += 1
        async def call(self, name, args):
            return {"content": [{"type": "text", "text": "plugin"}]}
    register_transport("example-plugin", Plugin)
    runtime = Runtime(connect)
    try:
        validate_config("example-plugin", {"version": 1})
        with pytest.raises(ValueError):
            validate_config("example-plugin", {})
        lease = runtime.create_lease("u", "t")
        await runtime.call(ServerSpec("p", "example-plugin", {"version": 1}), lease.id, "tool", {})
        await runtime.release(lease.id)
        assert Plugin.starts == Plugin.closes == 1
    finally:
        await runtime.close()
        PLUGINS.pop("example-plugin")
