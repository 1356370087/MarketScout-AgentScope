"""MCP streamable-http/sse fixture server on the MCP 2.x SDK (MCPServer)."""

import sys

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("test-http-server")


@mcp.tool(description="Echo the text back.")
def echo(text: str) -> str:
    return f"echo: {text}"


@mcp.tool(description="Greet a person by name.")
def greet(name: str) -> str:
    return f"hello {name}"


@mcp.tool(description="Sleep briefly; used for cancellation checks.")
async def slow(seconds: float) -> str:
    import asyncio

    await asyncio.sleep(seconds)
    return f"slept {seconds}"


if __name__ == "__main__":
    mcp.run(sys.argv[2] if len(sys.argv) > 2 else "streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
