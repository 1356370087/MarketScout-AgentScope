"""MCP stdio fixture server on the mcp 1.30 SDK (FastMCP)."""

import asyncio

from pydantic import Field
from typing_extensions import Annotated

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-stdio-server")


@mcp.tool(description="Echo the text back.")
def echo(text: str) -> str:
    return f"echo: {text}"


@mcp.tool(description="Greet a person by name.")
def greet(name: str) -> str:
    return f"hello {name}"


@mcp.tool(description="Constrained input for schema parity checks.")
def constrained(
    code: Annotated[str, Field(pattern=r"^[A-Z]{3}$", max_length=3)],
) -> str:
    return f"code: {code}"


@mcp.tool(description="Sleep briefly; used for cancellation checks.")
async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return f"slept {seconds}"


@mcp.tool(description="Always returns an error result.")
def fail() -> str:
    raise RuntimeError("boom")


if __name__ == "__main__":
    mcp.run("stdio")
