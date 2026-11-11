"""Lazy MCP connection for the Rhodes Island archive."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from astrbot.core.agent.mcp_client import MCPClient


class MCPError(RuntimeError):
    """Report a connection, tool, response, or cleanup failure."""


_FAILED = "数据服务暂时没有响应，请稍后再试。"


class MCPGateway:
    """Reuse an AstrBot MCP client until the gateway is closed.

    Args:
        url: Streamable HTTP endpoint of the MCP server.
        timeout: Connection and tool-call timeout in seconds.
    """

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self._url = url
        self._timeout = timeout
        self._client: MCPClient | None = None
        self._connection: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active_calls = 0
        self._closed = False

    async def _acquire_client(self) -> MCPClient:
        """Acquire an initialized client while protecting it from cleanup.

        Returns:
            The shared, connected AstrBot MCP client.

        Raises:
            MCPError: If the gateway is closed or the connection fails.
        """
        async with self._lock:
            if self._closed:
                raise MCPError("MCP gateway is closed")
            try:
                if self._client is None:
                    self._client = MCPClient()
                client = self._client
                if self._connection is None:
                    self._connection = asyncio.create_task(
                        client.connect_to_server(
                            {
                                "url": self._url,
                                "transport": "streamable_http",
                                "timeout": self._timeout,
                            },
                            "rhodes_island_archive",
                        )
                    )
                    # A cancelled waiter must not leave a failed task unobserved.
                    self._connection.add_done_callback(
                        lambda task: None if task.cancelled() else task.exception()
                    )
                connection = self._connection
            except Exception as exc:
                raise MCPError(_FAILED) from exc
            self._active_calls += 1
            self._idle.clear()
        try:
            # A cancelled waiter must not cancel a shared connection.
            await asyncio.shield(connection)
        except asyncio.CancelledError:
            self._release_client()
            raise
        except Exception as exc:
            if self._connection is connection:
                self._connection = None
            self._release_client()
            raise MCPError(_FAILED) from exc
        return client

    def _release_client(self) -> None:
        """Release a call's claim so cleanup can run when the client is idle."""
        # There is no await, so counter updates are atomic on the owning loop.
        self._active_calls -= 1
        if self._active_calls == 0:
            self._idle.set()

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and normalize its result to a dictionary.

        Args:
            tool: Exact server-side tool name.
            arguments: Arguments passed unchanged to the MCP tool.

        Returns:
            The server's structured result or decoded JSON object.

        Raises:
            MCPError: If the gateway is closed or connection, call, or decoding fails.
        """
        client = await self._acquire_client()
        try:
            try:
                result = await client.call_tool_with_reconnect(
                    tool, arguments, timedelta(seconds=self._timeout)
                )
            except Exception as exc:
                raise MCPError(_FAILED) from exc
            return self._parse_result(tool, result)
        finally:
            self._release_client()

    async def input_schema(self, tool: str) -> dict[str, Any]:
        """Read a tool's advertised input schema through the same MCP session.

        Args:
            tool: Exact server-side tool name.

        Returns:
            The input schema published by the MCP server.

        Raises:
            MCPError: If the server cannot be reached or does not list the tool.
        """
        client = await self._acquire_client()
        try:
            try:
                result = await asyncio.wait_for(
                    client.list_tools_and_save(), timeout=self._timeout
                )
            except Exception as exc:
                raise MCPError(_FAILED) from exc
            for item in result.tools:
                if item.name == tool and isinstance(item.inputSchema, dict):
                    return item.inputSchema
            raise MCPError("数据服务没有提供这个接口的定义。")
        finally:
            self._release_client()

    @staticmethod
    def _parse_result(tool: str, result: Any) -> dict[str, Any]:
        """Decode an MCP CallToolResult using the archive server's conventions.

        Structured content takes precedence. For text-only results, the first
        text block containing a JSON object is used. FastMCP wraps the glossary
        tool's JSON string in a structured ``result`` field; that field is
        decoded rather than returned as the glossary itself. Message-only
        responses from other archive tools represent unsuccessful queries.

        Args:
            tool: Exact server-side tool name.
            result: MCP CallToolResult returned by AstrBot's MCP client.

        Returns:
            The decoded server payload.

        Raises:
            MCPError: If the tool reports an error or has no JSON object result.
        """
        blocks = getattr(result, "content", None) or []
        if getattr(result, "isError", False):
            texts = [
                block.text
                for block in blocks
                if getattr(block, "type", None) == "text"
                and isinstance(getattr(block, "text", None), str)
            ]
            detail = "\n".join(texts) or "server reported an unspecified error"
            raise MCPError(f"MCP tool {tool!r} returned an error: {detail}")

        payload = getattr(result, "structuredContent", None)
        if payload is not None:
            if not isinstance(payload, dict):
                raise MCPError(f"MCP tool {tool!r} returned invalid structured content")
            if tool == "get_glossary" and set(payload) == {"result"}:
                payload = payload["result"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise MCPError(
                            f"MCP tool {tool!r} returned invalid glossary JSON"
                        ) from exc
        else:
            for block in blocks:
                text = getattr(block, "text", None)
                if getattr(block, "type", None) != "text" or not isinstance(text, str):
                    continue
                try:
                    candidate = json.loads(text)
                    # Also accept a JSON-encoded JSON string for the glossary.
                    if tool == "get_glossary" and isinstance(candidate, str):
                        candidate = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break

        if not isinstance(payload, dict):
            raise MCPError(_FAILED)
        if tool != "get_glossary" and set(payload) == {"message"}:
            raise MCPError(str(payload["message"]))
        return payload

    async def _finish_close(self) -> None:
        """Wait for calls and release the AstrBot client.

        Raises:
            MCPError: If the AstrBot client fails to clean up.
        """
        await self._idle.wait()
        connection = self._connection
        if connection is not None and not connection.done():
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
        if self._client is not None:
            try:
                await self._client.cleanup()
            except Exception as exc:
                raise MCPError(
                    f"MCP cleanup failed ({type(exc).__name__}): {exc}"
                ) from exc

    async def close(self) -> None:
        """Permanently close the gateway after its active calls finish.

        Concurrent and repeated closes share one cleanup operation. Cancelling a
        close waiter does not cancel the cleanup task.

        Raises:
            MCPError: If the AstrBot client fails to clean up.
        """
        async with self._lock:
            self._closed = True
            if self._closing is None:
                self._closing = asyncio.create_task(self._finish_close())
                # Cleanup continues even if its original waiter is cancelled.
                self._closing.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
            closing = self._closing
        await asyncio.shield(closing)
