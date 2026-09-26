import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import METHOD_NOT_FOUND

from mcp_manager.runtime import GatewayError
from mcp_manager.transports import SdkConnection


class PartialClient:
    protocol_version = "2025-06-18"

    def __init__(self):
        self.server_capabilities = {"tools": {}, "resources": {}}
        self.server_info = {"name": "partial"}

    async def list_tools(self, **kwargs):
        return {"tools": [{"name": "fetch", "inputSchema": {"type": "object"}}]}

    async def list_resources(self, **kwargs):
        raise MCPError(METHOD_NOT_FOUND, "Method not found")

    async def list_resource_templates(self, **kwargs):
        return {"resourceTemplates": []}


async def test_discovery_keeps_successful_capabilities_and_reports_failed_ones():
    result = await SdkConnection(PartialClient()).discover()

    assert [tool["name"] for tool in result["tools"]] == ["fetch"]
    assert result["resources"] == []
    assert result["templates"] == []
    assert result["capability_errors"] == [
        {"capability": "resources", "error": "Method not found"}
    ]


async def test_resources_only_server_passes_without_tools():
    class ResourcesOnlyClient:
        protocol_version = "2025-06-18"

        def __init__(self):
            self.server_capabilities = {"resources": {}}
            self.server_info = {"name": "resources-only"}

        async def list_resources(self, **kwargs):
            return {"resources": [{"uri": "fixture://item", "name": "item"}]}

        async def list_resource_templates(self, **kwargs):
            return {"resourceTemplates": []}

    result = await SdkConnection(ResourcesOnlyClient()).discover()

    assert [resource["uri"] for resource in result["resources"]] == ["fixture://item"]
    assert result["tools"] == []
    assert result["capability_errors"] == []


@pytest.mark.parametrize("capabilities", [{"tools": {}}, {}])
async def test_discovery_fails_when_no_declared_capability_succeeds(capabilities):
    class FailedClient:
        protocol_version = "2025-06-18"

        def __init__(self):
            self.server_capabilities = capabilities
            self.server_info = {"name": "failed"}

        async def list_tools(self, **kwargs):
            raise MCPError(METHOD_NOT_FOUND, "Method not found")

    with pytest.raises(GatewayError, match="capabilit"):
        await SdkConnection(FailedClient()).discover()
