"""Offline tests for the archive's AstrBot MCP gateway."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

_spec = importlib.util.spec_from_file_location(
    "archive_mcp_test_subject",
    Path(__file__).resolve().parents[1] / "providers" / "mcp.py",
)
assert _spec is not None and _spec.loader is not None
mcp_gateway = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_gateway)
MCPError = mcp_gateway.MCPError
MCPGateway = mcp_gateway.MCPGateway


def result(
    text: str | None = None,
    *,
    structured: dict | None = None,
    is_error: bool = False,
) -> CallToolResult:
    """Build a real MCP result for the offline client double.

    Args:
        text: Optional text content block.
        structured: Optional structured content object.
        is_error: MCP protocol-level error flag.

    Returns:
        A validated MCP CallToolResult.
    """
    content = [] if text is None else [TextContent(type="text", text=text)]
    return CallToolResult(
        content=content, structuredContent=structured, isError=is_error
    )


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, Mock]:
    """Patch only the client boundary; never connect to a network.

    Args:
        monkeypatch: Pytest patch fixture.

    Returns:
        The client double and the patched constructor.
    """
    client = SimpleNamespace(
        connect_to_server=AsyncMock(),
        call_tool_with_reconnect=AsyncMock(
            return_value=result('{"data": {"ok": true}}')
        ),
        cleanup=AsyncMock(),
    )
    constructor = Mock(return_value=client)
    monkeypatch.setattr(mcp_gateway, "MCPClient", constructor)
    return client, constructor


@pytest.mark.asyncio
async def test_lazy_connect_reuses_client_and_forwards_arguments(fake_client) -> None:
    client, constructor = fake_client
    gateway = MCPGateway("https://example.invalid/mcp", timeout=12.5)
    constructor.assert_not_called()

    arguments = {"query": "阿米娅", "filters": ["operator"]}
    assert await gateway.call("search", arguments) == {"data": {"ok": True}}
    assert await gateway.call("search", {"query": "凯尔希"}) == {"data": {"ok": True}}

    constructor.assert_called_once_with()
    client.connect_to_server.assert_awaited_once_with(
        {
            "url": "https://example.invalid/mcp",
            "transport": "streamable_http",
            "timeout": 12.5,
        },
        "rhodes_island_archive",
    )
    assert client.call_tool_with_reconnect.await_args_list == [
        call("search", arguments, timedelta(seconds=12.5)),
        call("search", {"query": "凯尔希"}, timedelta(seconds=12.5)),
    ]
    assert client.call_tool_with_reconnect.await_args_list[0].args[1] is arguments
    await gateway.close()
    client.cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_concurrent_first_calls_share_one_connection(fake_client) -> None:
    client, constructor = fake_client
    entered = asyncio.Event()
    release = asyncio.Event()

    async def connect(*args) -> None:
        entered.set()
        await release.wait()

    client.connect_to_server.side_effect = connect
    gateway = MCPGateway("https://example.invalid/mcp")
    tasks = [asyncio.create_task(gateway.call("search", {"n": n})) for n in range(20)]
    await asyncio.wait_for(entered.wait(), 1)
    assert client.connect_to_server.await_count == 1
    client.call_tool_with_reconnect.assert_not_awaited()
    release.set()
    assert await asyncio.gather(*tasks) == [{"data": {"ok": True}}] * 20
    assert client.call_tool_with_reconnect.await_count == 20
    constructor.assert_called_once_with()
    await gateway.close()


@pytest.mark.asyncio
async def test_concurrent_connection_failure_is_shared_and_later_call_retries(
    fake_client,
) -> None:
    client, _ = fake_client
    entered = asyncio.Event()
    release = asyncio.Event()

    async def connect(*args) -> None:
        entered.set()
        await release.wait()
        raise OSError("connection refused")

    client.connect_to_server.side_effect = connect
    gateway = MCPGateway("https://example.invalid/mcp")
    tasks = [asyncio.create_task(gateway.call("search", {})) for _ in range(5)]
    await asyncio.wait_for(entered.wait(), 1)
    release.set()
    errors = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(errors) == 5
    assert all(isinstance(error, MCPError) for error in errors)
    assert all("数据服务暂时没有响应" in str(error) for error in errors)
    assert all(isinstance(error.__cause__, OSError) for error in errors)
    client.connect_to_server.assert_awaited_once()
    client.call_tool_with_reconnect.assert_not_awaited()

    client.connect_to_server.side_effect = None
    assert await gateway.call("search", {}) == {"data": {"ok": True}}
    assert client.connect_to_server.await_count == 2
    await gateway.close()


@pytest.mark.asyncio
async def test_cancelling_one_waiter_preserves_the_shared_connection(
    fake_client,
) -> None:
    client, _ = fake_client
    entered = asyncio.Event()
    release = asyncio.Event()

    async def connect(*args) -> None:
        entered.set()
        await release.wait()

    client.connect_to_server.side_effect = connect
    gateway = MCPGateway("https://example.invalid/mcp")
    first = asyncio.create_task(gateway.call("search", {"n": 1}))
    second = asyncio.create_task(gateway.call("search", {"n": 2}))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {"data": {"ok": True}}
    client.connect_to_server.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
async def test_disconnect_preserves_cause_and_uses_astrbot_reconnect(
    fake_client,
) -> None:
    client, _ = fake_client
    client.call_tool_with_reconnect.side_effect = ConnectionError("transport closed")
    gateway = MCPGateway("https://example.invalid/mcp")
    with pytest.raises(MCPError, match="数据服务暂时没有响应") as caught:
        await gateway.call("search", {"query": "x"})
    assert isinstance(caught.value.__cause__, ConnectionError)
    client.call_tool_with_reconnect.assert_awaited_once_with(
        "search", {"query": "x"}, timedelta(seconds=30)
    )
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "response", "expected"),
    [
        (
            "search",
            result('{"data": {"source": "text"}}'),
            {"data": {"source": "text"}},
        ),
        (
            "search",
            result(
                '{"data": {"source": "text"}}',
                structured={"data": {"source": "structured"}},
            ),
            {"data": {"source": "structured"}},
        ),
        ("search", result(structured={}), {}),
        (
            "search",
            result('{"data": {}, "message": "info"}'),
            {"data": {}, "message": "info"},
        ),
        (
            "get_operator_basic_data",
            result('{"message": "choose", "candidates": ["A", "B"]}'),
            {"message": "choose", "candidates": ["A", "B"]},
        ),
        (
            "get_glossary",
            result('{"攻击力": "描述"}', structured={"result": '{"攻击力": "描述"}'}),
            {"攻击力": "描述"},
        ),
        ("get_glossary", result('{"攻击力": "描述"}'), {"攻击力": "描述"}),
        (
            "get_glossary",
            result(json.dumps('{"term": "description"}')),
            {"term": "description"},
        ),
        (
            "get_glossary",
            result('{"message": "a glossary entry"}'),
            {"message": "a glossary entry"},
        ),
    ],
)
async def test_successful_result_decoding(
    fake_client, tool, response, expected
) -> None:
    client, _ = fake_client
    client.call_tool_with_reconnect.return_value = response
    gateway = MCPGateway("https://example.invalid/mcp")
    assert await gateway.call(tool, {}) == expected
    await gateway.close()


@pytest.mark.asyncio
async def test_text_decoding_skips_non_json_content_blocks(fake_client) -> None:
    client, _ = fake_client
    client.call_tool_with_reconnect.return_value = CallToolResult(
        content=[
            TextContent(type="text", text="summary"),
            TextContent(type="text", text='["not", "an", "object"]'),
            TextContent(type="text", text='{"data": {"ok": true}}'),
        ]
    )
    gateway = MCPGateway("https://example.invalid/mcp")
    assert await gateway.call("search", {}) == {"data": {"ok": True}}
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "response", "error"),
    [
        (
            "search",
            result(
                "server exploded",
                structured={"data": "looks successful"},
                is_error=True,
            ),
            "server exploded",
        ),
        ("search", result('{"message": "未初始化数据上下文"}'), "未初始化数据上下文"),
        ("search", result(structured={"message": "not found"}), "not found"),
        ("search", result("not JSON"), "数据服务暂时没有响应"),
        ("search", result("[1, 2]"), "数据服务暂时没有响应"),
        ("search", result("null"), "数据服务暂时没有响应"),
        ("search", result(), "数据服务暂时没有响应"),
        (
            "get_glossary",
            result('{"valid": "text"}', structured={"result": "not JSON"}),
            "invalid glossary JSON",
        ),
        ("get_glossary", result(structured={"result": "[]"}), "数据服务暂时没有响应"),
    ],
)
async def test_error_results_raise_mcp_error(
    fake_client, tool, response, error
) -> None:
    client, _ = fake_client
    client.call_tool_with_reconnect.return_value = response
    gateway = MCPGateway("https://example.invalid/mcp")
    with pytest.raises(MCPError, match=error):
        await gateway.call(tool, {})
    await gateway.close()


@pytest.mark.asyncio
async def test_close_before_first_call_is_lazy_and_permanent(fake_client) -> None:
    _, constructor = fake_client
    gateway = MCPGateway("https://example.invalid/mcp")
    await asyncio.gather(gateway.close(), gateway.close())
    constructor.assert_not_called()
    with pytest.raises(MCPError, match="closed"):
        await gateway.call("search", {})


@pytest.mark.asyncio
async def test_close_waits_for_active_call_and_is_idempotent(fake_client) -> None:
    client, _ = fake_client
    entered = asyncio.Event()
    release = asyncio.Event()

    async def call_tool(*args):
        entered.set()
        await release.wait()
        return result('{"data": {"ok": true}}')

    client.call_tool_with_reconnect.side_effect = call_tool
    gateway = MCPGateway("https://example.invalid/mcp")
    pending_call = asyncio.create_task(gateway.call("search", {}))
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(gateway.close())
    await asyncio.sleep(0)
    assert not closing.done()
    client.cleanup.assert_not_awaited()
    with pytest.raises(MCPError, match="closed"):
        await gateway.call("search", {})
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    release.set()
    assert await pending_call == {"data": {"ok": True}}
    await asyncio.gather(gateway.close(), gateway.close())
    client.cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_cancels_unobserved_pending_connection(fake_client) -> None:
    client, _ = fake_client
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def connect(*args) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    client.connect_to_server.side_effect = connect
    gateway = MCPGateway("https://example.invalid/mcp")
    task = asyncio.create_task(gateway.call("search", {}))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(gateway.close(), 1)
    assert stopped.is_set()
    client.cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_reports_cleanup_failure(fake_client) -> None:
    client, _ = fake_client
    client.cleanup.side_effect = RuntimeError("cleanup exploded")
    gateway = MCPGateway("https://example.invalid/mcp")
    await gateway.call("search", {})
    with pytest.raises(MCPError, match="cleanup exploded") as caught:
        await gateway.close()
    assert isinstance(caught.value.__cause__, RuntimeError)
    with pytest.raises(MCPError, match="cleanup exploded"):
        await gateway.close()
    client.cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_real_mcp_protocol_over_in_memory_transport(monkeypatch) -> None:
    server = FastMCP("archive-test")

    @server.tool()
    def search(query: str) -> dict:
        return {"data": {"query": query}}

    @server.tool()
    def get_glossary(glossary_name: str | list[str]) -> str:
        assert glossary_name == ["攻击力"]
        return json.dumps({"攻击力": "描述"}, ensure_ascii=False)

    @server.tool()
    def failing_tool() -> dict:
        raise ValueError("real protocol error")

    async def connect_in_memory(client, config, name) -> None:
        assert config["transport"] == "streamable_http"
        assert client.exit_stack is not None
        client.session = await client.exit_stack.enter_async_context(
            create_connected_server_and_client_session(server)
        )

    # Keep the real AstrBot client, including its owning task, RPC call and
    # cleanup; replace only the network transport with MCP memory streams.
    monkeypatch.setattr(mcp_gateway.MCPClient, "_do_connect", connect_in_memory)
    gateway = MCPGateway("https://example.invalid/mcp")
    try:
        assert await gateway.call("search", {"query": "阿米娅"}) == {
            "data": {"query": "阿米娅"}
        }
        schema = await gateway.input_schema("search")
        assert schema["properties"]["query"]["type"] == "string"
        with pytest.raises(MCPError, match="没有提供这个接口的定义"):
            await gateway.input_schema("missing")
        assert await gateway.call("get_glossary", {"glossary_name": ["攻击力"]}) == {
            "攻击力": "描述"
        }
        with pytest.raises(MCPError, match="real protocol error"):
            await gateway.call("failing_tool", {})
    finally:
        await gateway.close()

