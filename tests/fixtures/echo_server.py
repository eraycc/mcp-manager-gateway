from mcp.server import MCPServer

server = MCPServer("echo-fixture")

@server.tool()
def echo(value: str) -> str:
    return value

@server.tool()
def structured(value: int) -> dict:
    return {"value": value}

if __name__ == "__main__":
    server.run(transport="stdio")
