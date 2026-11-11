"""Offline regression tests for the MCP-backed archive plugin."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import botpy.message
import httpx
import pytest
import pytest_asyncio

from astrbot.api.message_components import ActionRow, ButtonInteraction, Image, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform import button_interaction
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.button_interaction import decode_button_callback
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from data.plugins.astrbot_plugin_rhodes_island_archive import main

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aHosAAAAASUVORK5CYII="
)
CARD_URL = "https://images.example.invalid/archive/card.png"
VOCABULARY = [
    "高级资深干员",
    "资深干员",
    "近卫干员",
    "狙击干员",
    "重装干员",
    "医疗干员",
    "辅助干员",
    "术师干员",
    "特种干员",
    "先锋干员",
    "近战位",
    "远程位",
    "输出",
    "防护",
    "治疗",
    "支援",
]


class StubGateway:
    """Record calls and emulate the MCP result and schema boundaries."""

    def __init__(self) -> None:
        self.calls = []
        self.schema_calls = []
        self.search_items = []
        self.card_items = None
        self.tool_results = {}
        self.closed = False

    async def call(self, tool: str, arguments: dict) -> dict:
        """Return a configurable response without contacting a server.

        Args:
            tool: Requested MCP tool name.
            arguments: Arguments sent to the tool.

        Returns:
            An MCP-shaped result.
        """
        assert tool != "get_operator_skill", "The retired skill tool was called"
        self.calls.append((tool, arguments))
        if tool == "search":
            return {"data": {"items": self.search_items}}
        if tool == "build_search_card":
            by_id = {item["id"]: item for item in self.search_items}
            items = (
                self.card_items
                if self.card_items is not None
                else [
                    by_id[item_id] for item_id in arguments["ids"] if item_id in by_id
                ]
            )
            return {"data": {"items": items}, "card_image_url": CARD_URL}
        return self.tool_results.get(tool, {"data": {}, "card_image_url": CARD_URL})

    async def input_schema(self, tool: str) -> dict:
        """Expose an asynchronous recruitment schema just like the gateway.

        Args:
            tool: Requested MCP tool name.

        Returns:
            An enum-backed tags input schema.
        """
        self.schema_calls.append(tool)
        assert tool == "recruit_with_tag"
        return {"properties": {"tags": {"items": {"enum": VOCABULARY}}}}

    async def close(self) -> None:
        self.closed = True


class StubEvent:
    """Minimal event with explicit sender, session, wake, and image state."""

    def __init__(
        self,
        text: str = "",
        *,
        sender: str = "user-a",
        origin: str = "qq_official:GroupMessage:group-a",
        wake: bool = True,
        images: list[Image] | None = None,
        interaction: ButtonInteraction | None = None,
        platform: str = "qq_official",
    ) -> None:
        self.text = text
        self.sender = sender
        self.unified_msg_origin = origin
        self.is_at_or_wake_command = wake
        self.images = images or []
        self.interaction = interaction
        self.platform = platform

    def get_message_str(self) -> str:
        return self.text

    def get_messages(self) -> list[Image]:
        return self.images

    def get_sender_id(self) -> str:
        return self.sender

    def get_platform_name(self) -> str:
        return self.platform

    def get_button_interaction(self) -> ButtonInteraction | None:
        return self.interaction

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(text)


@pytest_asyncio.fixture
async def subject(monkeypatch):
    """Create a real plugin with offline MCP, image HTTP, and deterministic clocks.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Yields:
        Plugin instance, gateway, mutable clock, and recorded image requests.
    """
    gateway = StubGateway()
    clock = [1000.0]
    requests = []
    monkeypatch.setattr(main, "MCPGateway", lambda *args, **kwargs: gateway)
    monkeypatch.setattr(
        main, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )
    plugin = main.Main(context=None, config={})
    await plugin._http.aclose()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "images.example.invalid"
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    plugin._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        yield plugin, gateway, clock, requests
    finally:
        await plugin.terminate()
        assert gateway.closed


async def collect(generator) -> list[MessageEventResult]:
    """Consume a plugin event handler and return its replies.

    Args:
        generator: Async generator returned by a listener.

    Returns:
        List of emitted message results.
    """
    return [reply async for reply in generator]


def text_of(result: MessageEventResult) -> str:
    """Read only the text components of a message result.

    Args:
        result: Result produced by the plugin.

    Returns:
        Concatenated plain text.
    """
    return "".join(part.text for part in result.chain if isinstance(part, Plain))


def buttons_of(result: MessageEventResult):
    """Flatten all buttons in the result, preserving their visual order.

    Args:
        result: Result produced by the plugin.

    Returns:
        The ordered button list.
    """
    return [
        button
        for row in result.chain
        if isinstance(row, ActionRow)
        for button in row.buttons
    ]


DETAIL_CASES = [
    ("干员", "干员", "get_operator_basic_data", "operator_id"),
    ("干员材料", "干员", "get_operator_material", "operator_id"),
    ("模组", "干员", "get_operator_modules", "operator_id"),
    ("皮肤", "干员", "get_operator_skins", "operator_id"),
    ("召唤物", "召唤物", "get_token_detail", "token_id"),
    ("材料", "材料", "get_material", "material_id"),
    ("关卡", "关卡", "get_stage_data", "stage_id"),
    ("敌人", "敌人", "get_enemy_data", "enemy_id"),
    ("盟约", "卫戍协议盟约", "get_autochess_bond_detail", "bond_id"),
    ("策略", "卫戍协议策略", "get_autochess_strategy_detail", "strategy_id"),
    ("肉鸽", "集成战略藏品", "get_integrated_strategy_item_detail", "item_id"),
    ("藏品", "集成战略藏品", "get_integrated_strategy_item_detail", "item_id"),
    ("集成战略", "集成战略剧目", "get_integrated_strategy_item_detail", "item_id"),
    # Live search reports bare type names for these sub-kinds.
    ("集成战略", "通宝", "get_integrated_strategy_item_detail", "item_id"),
    ("集成战略", "骰子", "get_integrated_strategy_item_detail", "item_id"),
    ("集成战略", "构想", "get_integrated_strategy_item_detail", "item_id"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("prefix", "kind", "tool", "argument"), DETAIL_CASES)
async def test_all_detail_commands_forward_exact_mcp_arguments(
    subject, prefix, kind, tool, argument
):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "fixture_id", "name": "测试名称", "type": kind}]
    replies = await collect(plugin.on_query(StubEvent(f"{prefix} 测试名称")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "测试名称"}),
        (tool, {argument: "fixture_id"}),
    ]
    assert CARD_URL in text_of(replies[0])


@pytest.mark.asyncio
async def test_exact_alias_outranks_fuzzy_and_filters_result_type(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "fuzzy_id", "name": "阿米娅近卫", "type": "干员"},
        {"id": "wrong_id", "name": "小兔", "type": "敌人"},
        {"id": "exact_id", "name": "阿米娅", "from_alias": "小兔", "type": "干员"},
    ]
    replies = await collect(plugin.on_query(StubEvent("干员 小兔")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "小兔"}),
        ("get_operator_basic_data", {"operator_id": "exact_id"}),
    ]


@pytest.mark.asyncio
async def test_stage_code_exact_match_is_resolved(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "stage_1_7", "name": "暴君", "code": "1-7", "type": "关卡"}
    ]
    replies = await collect(plugin.on_query(StubEvent("关卡 1-7")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "1-7"}),
        ("get_stage_data", {"stage_id": "stage_1_7"}),
    ]


@pytest.mark.asyncio
async def test_exact_id_is_resolved_through_search_card(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "char_002_amiya", "name": "阿米娅", "type": "干员"}]
    replies = await collect(plugin.on_query(StubEvent("干员 char_002_amiya")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("build_search_card", {"ids": ["char_002_amiya"]}),
        ("get_operator_basic_data", {"operator_id": "char_002_amiya"}),
    ]


@pytest.mark.asyncio
async def test_unique_fuzzy_match_requires_confirmation(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    replies = await collect(plugin.on_query(StubEvent("干员 阿米")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "阿米"}),
        ("build_search_card", {"ids": ["char_amy"]}),
    ]
    assert [button.label for button in buttons_of(replies[0])] == ["1"]
    assert buttons_of(replies[0])[0].action.data["item"]["id"] == "char_amy"


@pytest.mark.asyncio
async def test_search_card_uses_returned_item_order_for_buttons_and_number_selection(
    subject,
):
    plugin, gateway, _, _ = subject
    first = {"id": "char_a", "name": "甲", "type": "干员"}
    second = {"id": "enemy_b", "name": "乙", "type": "敌人"}
    gateway.card_items = [second, first]
    replies = await collect(plugin.on_query(StubEvent("搜索卡 char_a enemy_b char_a")))
    assert len(replies) == 1
    assert gateway.calls == [("build_search_card", {"ids": ["char_a", "enemy_b"]})]
    buttons = buttons_of(replies[0])
    assert [button.label for button in buttons] == ["1", "2"]
    assert [button.action.data["item"]["id"] for button in buttons] == [
        "enemy_b",
        "char_a",
    ]
    selected = await collect(plugin.on_query(StubEvent("选择1")))
    assert len(selected) == 1
    assert gateway.calls[-1] == ("get_enemy_data", {"enemy_id": "enemy_b"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index", "tool", "argument", "item_id"),
    [
        (0, "get_enemy_data", "enemy_id", "enemy_b"),
        (1, "get_operator_basic_data", "operator_id", "char_a"),
    ],
)
async def test_search_card_button_routes_its_displayed_item(
    subject, index, tool, argument, item_id
):
    plugin, gateway, _, _ = subject
    gateway.card_items = [
        {"id": "enemy_b", "name": "乙", "type": "敌人"},
        {"id": "char_a", "name": "甲", "type": "干员"},
    ]
    result = (await collect(plugin.on_query(StubEvent("搜索卡 char_a enemy_b"))))[0]
    button = buttons_of(result)[index]
    interaction = ButtonInteraction(
        action_id=button.id, data=button.action.data, interaction_id="click"
    )
    assert len(await collect(plugin.on_button(StubEvent(interaction=interaction)))) == 1
    assert gateway.calls[-1] == (tool, {argument: item_id})


@pytest.mark.asyncio
async def test_search_command_always_shows_selection_card(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    replies = await collect(plugin.on_query(StubEvent("搜索 阿米娅")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "阿米娅"}),
        ("build_search_card", {"ids": ["char_amy"]}),
    ]
    assert [button.label for button in buttons_of(replies[0])] == ["1"]


@pytest.mark.asyncio
async def test_qq_group_image_and_buttons_survive_real_adapter_as_one_message(
    subject, monkeypatch
):
    plugin, gateway, _, requests = subject
    parent_send = AsyncMock()
    monkeypatch.setattr(AstrMessageEvent, "send", parent_send)
    monkeypatch.setattr(
        button_interaction,
        "_button_callback_registry",
        button_interaction._ButtonCallbackRegistry(),
    )
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    result = (await collect(plugin.on_query(StubEvent("干员 阿米娅"))))[0]
    assert result.use_markdown_ is True
    assert any(isinstance(part, Plain) for part in result.chain)
    assert any(isinstance(part, ActionRow) for part in result.chain)
    assert not any(isinstance(part, Image) for part in result.chain)
    assert len(requests) == 1
    assert requests[0].headers["range"] == "bytes=0-65535"

    parsed = await QQOfficialMessageEvent._parse_to_qqofficial(result)
    markdown, image_base64, image_path, *_ = parsed
    assert f"]({CARD_URL})" in markdown
    assert "#1px #1px" in markdown
    assert image_base64 is None and image_path is None
    keyboard = QQOfficialMessageEvent._parse_keyboard(result)
    native_buttons = keyboard["content"]["rows"][0]["buttons"]
    assert [button["render_data"]["label"] for button in native_buttons] == [
        "模组",
        "皮肤",
        "材料",
    ]
    action_id, data = decode_button_callback(native_buttons[0]["action"]["data"])
    assert action_id == "ri_1"
    assert data["operation"] == "modules"
    assert data["item"]["id"] == "char_amy"

    raw = botpy.message.GroupMessage(
        api=None,
        event_id="event-1",
        data={
            "id": "msg-1",
            "author": {"member_openid": "member-1"},
            "group_openid": "group-1",
            "content": "干员 阿米娅",
            "timestamp": "0",
        },
    )
    abm = AstrBotMessage()
    abm.message_id = "msg-1"
    abm.session_id = abm.group_id = "group-1"
    abm.self_id = "bot-1"
    abm.sender = MessageMember(user_id="member-1")
    abm.type = MessageType.GROUP_MESSAGE
    abm.message_str = "干员 阿米娅"
    abm.message = []
    abm.raw_message = raw
    bot = SimpleNamespace(api=SimpleNamespace(post_group_message=AsyncMock()))
    qq = QQOfficialMessageEvent(
        message_str=abm.message_str,
        message_obj=abm,
        platform_meta=PlatformMetadata(
            name="qq_official", description="test", id="test"
        ),
        session_id="group-1",
        bot=bot,
    )
    await qq.send(result)
    parent_send.assert_awaited_once()
    assert parent_send.await_args.args[0].chain == result.chain
    bot.api.post_group_message.assert_awaited_once()
    payload = bot.api.post_group_message.await_args.kwargs
    assert payload["msg_type"] == 2
    assert payload["markdown"]["content"] == markdown
    assert payload["keyboard"] == keyboard
    assert "media" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "expected_count"), [(0.0, 1), (299.999, 1), (300.0, 0), (301.0, 0)]
)
async def test_button_silently_expires_after_five_minutes(subject, age, expected_count):
    plugin, gateway, clock, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    result = (await collect(plugin.on_query(StubEvent("干员 阿米娅"))))[0]
    button = buttons_of(result)[0]
    clock[0] += age
    interaction = ButtonInteraction(
        action_id=button.id, data=button.action.data, interaction_id="click"
    )
    before = len(gateway.calls)
    replies = await collect(plugin.on_button(StubEvent(interaction=interaction)))
    assert len(replies) == expected_count
    assert len(gateway.calls) - before == expected_count
    if expected_count:
        assert gateway.calls[-1] == (
            "get_operator_modules",
            {"operator_id": "char_amy"},
        )


@pytest.mark.asyncio
async def test_operator_title_uses_chinese_name_object(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "搜索名", "type": "干员"}]
    gateway.tool_results["get_operator_basic_data"] = {
        "data": {"名称": {"中文名": "阿米娅", "英文名": "Amiya"}},
        "card_image_url": CARD_URL,
    }
    result = (await collect(plugin.on_query(StubEvent("干员 搜索名"))))[0]
    assert "阿米娅 · 干员" in text_of(result)
    assert "{'中文名'" not in text_of(result)
    assert all(
        button.action.data["item"]["name"] == "阿米娅" for button in buttons_of(result)
    )


@pytest.mark.asyncio
async def test_summon_owner_resolves_to_its_token_id(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "char_owner", "name": "召唤者", "type": "干员"}]
    gateway.tool_results["get_operator_basic_data"] = {
        "data": {"召唤物": {"列表": [{"id": "token_1", "名称": "装置"}]}}
    }
    replies = await collect(plugin.on_query(StubEvent("召唤物 召唤者")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "召唤者"}),
        ("get_operator_basic_data", {"operator_id": "char_owner"}),
        ("get_token_detail", {"token_id": "token_1"}),
    ]


@pytest.mark.asyncio
async def test_skin_candidate_uses_operator_id_not_skin_id(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "skin_a", "operator_id": "char_owner", "name": "新皮肤", "type": "皮肤"}
    ]
    gateway.tool_results["get_operator_skins"] = {
        "data": {"skins": [{"id": "skin_a", "名称": "新皮肤", "card_url": CARD_URL}]}
    }
    replies = await collect(plugin.on_query(StubEvent("皮肤 新皮肤")))
    assert len(replies) == 1
    assert gateway.calls[-1] == ("get_operator_skins", {"operator_id": "char_owner"})
    assert CARD_URL in text_of(replies[0])


@pytest.mark.asyncio
async def test_glossary_and_text_recruit_are_forwarded_as_tools(subject):
    plugin, gateway, _, _ = subject
    gateway.tool_results["get_glossary"] = {"攻击力": "说明"}
    gateway.tool_results["recruit_with_tag"] = {
        "data": {"tags": ["近卫", "输出"]},
        "card_image_url": CARD_URL,
    }
    assert len(await collect(plugin.on_query(StubEvent("术语 攻击力")))) == 1
    assert len(await collect(plugin.on_query(StubEvent("公招 近卫 输出")))) == 1
    assert gateway.calls == [
        ("get_glossary", {"glossary_name": "攻击力"}),
        ("recruit_with_tag", {"tags": ["近卫干员", "输出"]}),
    ]
    assert gateway.schema_calls == ["recruit_with_tag"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ocr_text", "count"),
    [
        ("未检测到任何内容", 0),
        ("输出", 1),
        ("近卫 近战位 输出 防护", 4),
        ("近卫 近战位 输出 防护 支援 治疗", 6),
    ],
)
async def test_screenshot_requires_exactly_five_tags(
    subject, monkeypatch, ocr_text, count
):
    plugin, gateway, _, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    recognize = AsyncMock(return_value=ocr_text)
    monkeypatch.setattr(main, "recognize", recognize)
    event = StubEvent(
        "公招", images=[Image.fromURL("https://images.example.invalid/screenshot.png")]
    )
    replies = await collect(plugin.on_query(event))
    assert len(replies) == 1
    assert str(count) in text_of(replies[0])
    assert "5" in text_of(replies[0])
    assert all(tool != "recruit_with_tag" for tool, _ in gateway.calls)
    recognize.assert_awaited_once_with("/offline/screenshot.png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profession", ["近卫", "狙击", "重装", "医疗", "辅助", "术师", "特种", "先锋"]
)
async def test_screenshot_maps_short_profession_and_passes_all_five_tags(
    subject, monkeypatch, profession
):
    plugin, gateway, _, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    monkeypatch.setattr(
        main, "recognize", AsyncMock(return_value=f"{profession} 近战位 输出 防护 支援")
    )
    gateway.tool_results["recruit_with_tag"] = {
        "data": {"tags": [profession, "近战位", "输出", "防护", "支援"]},
        "card_image_url": CARD_URL,
    }
    event = StubEvent(
        "公招", images=[Image.fromURL("https://images.example.invalid/screenshot.png")]
    )
    replies = await collect(plugin.on_query(event))
    assert len(replies) == 1
    assert gateway.calls == [
        (
            "recruit_with_tag",
            {"tags": [f"{profession}干员", "近战位", "输出", "防护", "支援"]},
        )
    ]
    assert gateway.schema_calls == ["recruit_with_tag"]
    assert "未识别" not in text_of(replies[0])
    assert CARD_URL in text_of(replies[0])


@pytest.mark.asyncio
async def test_ocr_error_keeps_waiting_without_calling_recruit(subject, monkeypatch):
    plugin, gateway, _, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    monkeypatch.setattr(
        main, "recognize", AsyncMock(side_effect=main.OCRError("recognition failed"))
    )
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    replies = await collect(plugin.on_query(StubEvent("公招", images=[image])))
    assert len(replies) == 1
    assert "30" in text_of(replies[0])
    assert not gateway.calls
    assert (
        plugin._pending_recruit[("qq_official:GroupMessage:group-a", "user-a")]
        == 1030.0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["阿米娅", "干员 阿米娅", "公招", "搜索 阿米娅", "普通消息"]
)
async def test_unwoken_messages_are_silent_and_do_not_query(subject, text):
    plugin, gateway, _, _ = subject
    assert await collect(plugin.on_query(StubEvent(text, wake=False))) == []
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "tool"),
    [
        ("模组", "get_operator_modules"),
        ("皮肤", "get_operator_skins"),
        ("材料", "get_operator_material"),
        ("召唤物", "get_token_detail"),
    ],
)
async def test_unwoken_card_fallback_words_expire_with_the_card(subject, text, tool):
    plugin, gateway, clock, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    gateway.tool_results["get_operator_basic_data"] = {
        "data": {"召唤物": {"列表": [{"id": "token_amy", "名称": "装置"}]}},
        "card_image_url": CARD_URL,
    }
    assert len(await collect(plugin.on_query(StubEvent("干员 阿米娅")))) == 1
    gateway.calls.clear()
    assert len(await collect(plugin.on_query(StubEvent(text, wake=False)))) == 1
    assert gateway.calls[-1][0] == tool
    gateway.calls.clear()
    clock[0] += 300
    assert await collect(plugin.on_query(StubEvent(text, wake=False))) == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_unwoken_number_selection_expires_with_search_card(subject):
    plugin, gateway, clock, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    assert len(await collect(plugin.on_query(StubEvent("搜索 阿米娅")))) == 1
    gateway.calls.clear()
    assert len(await collect(plugin.on_query(StubEvent("选择1", wake=False)))) == 1
    assert gateway.calls[-1][0] == "get_operator_basic_data"
    gateway.calls.clear()
    clock[0] += 300
    assert await collect(plugin.on_query(StubEvent("选择1", wake=False))) == []
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["技能", "技能 阿米娅", "干员技能 阿米娅", "阿米娅技能", "干员 技能 阿米娅"]
)
async def test_skill_commands_never_trigger_or_call_retired_tool(subject, text):
    plugin, gateway, _, _ = subject
    assert await collect(plugin.on_query(StubEvent(text))) == []
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["终末地 干员 陈千语", "终末地 陈千语"])
async def test_endfield_commands_are_disabled(subject, text):
    plugin, gateway, _, _ = subject
    assert await collect(plugin.on_query(StubEvent(text))) == []
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("elapsed", "expected_count"), [(29.999, 1), (30.0, 0), (31.0, 0)]
)
async def test_pending_recruit_screenshot_expires_at_30_seconds(
    subject, monkeypatch, elapsed, expected_count
):
    plugin, gateway, clock, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    monkeypatch.setattr(
        main, "recognize", AsyncMock(return_value="近卫 近战位 输出 防护 支援")
    )
    assert len(await collect(plugin.on_query(StubEvent("公招")))) == 1
    clock[0] += elapsed
    image_event = StubEvent(
        wake=False,
        images=[Image.fromURL("https://images.example.invalid/screenshot.png")],
    )
    replies = await collect(plugin.on_recruit_image(image_event))
    assert len(replies) == expected_count
    assert len(gateway.calls) == expected_count
    if expected_count:
        assert gateway.calls[0][0] == "recruit_with_tag"


@pytest.mark.asyncio
async def test_screenshots_are_isolated_per_sender_in_same_group(subject, monkeypatch):
    plugin, gateway, _, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    recognize = AsyncMock(return_value="近卫 近战位 输出 防护 支援")
    monkeypatch.setattr(main, "recognize", recognize)
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    assert len(await collect(plugin.on_query(StubEvent("公招", sender="user-a")))) == 1
    assert (
        await collect(
            plugin.on_recruit_image(
                StubEvent(sender="user-b", wake=False, images=[image])
            )
        )
        == []
    )
    assert gateway.calls == []
    recognize.assert_not_awaited()
    replies = await collect(
        plugin.on_recruit_image(StubEvent(sender="user-a", wake=False, images=[image]))
    )
    assert len(replies) == 1
    assert gateway.calls == [
        ("recruit_with_tag", {"tags": ["近卫干员", "近战位", "输出", "防护", "支援"]})
    ]


@pytest.mark.asyncio
async def test_prior_other_user_screenshot_is_not_consumed_for_recruit(
    subject, monkeypatch
):
    plugin, gateway, _, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    recognize = AsyncMock(return_value="近卫 近战位 输出 防护 支援")
    monkeypatch.setattr(main, "recognize", recognize)
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    assert (
        await collect(
            plugin.on_recruit_image(
                StubEvent(sender="user-b", wake=False, images=[image])
            )
        )
        == []
    )
    replies = await collect(plugin.on_query(StubEvent("公招", sender="user-a")))
    assert len(replies) == 1
    assert "30" in text_of(replies[0])
    assert gateway.calls == []
    recognize.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_prior_screenshot_can_start_a_recruit_query(subject, monkeypatch):
    plugin, gateway, clock, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    recognize = AsyncMock(return_value="近卫 近战位 输出 防护 支援")
    monkeypatch.setattr(main, "recognize", recognize)
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    assert (
        await collect(plugin.on_recruit_image(StubEvent(wake=False, images=[image])))
        == []
    )
    clock[0] += 5
    replies = await collect(plugin.on_query(StubEvent("公招")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("recruit_with_tag", {"tags": ["近卫干员", "近战位", "输出", "防护", "支援"]})
    ]
    recognize.assert_awaited_once()


@pytest.mark.asyncio
async def test_latest_prior_screenshot_has_no_implicit_age_limit(subject, monkeypatch):
    plugin, gateway, clock, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/offline/screenshot.png"),
    )
    recognize = AsyncMock(return_value="近卫 近战位 输出 防护 支援")
    monkeypatch.setattr(main, "recognize", recognize)
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    assert (
        await collect(plugin.on_recruit_image(StubEvent(wake=False, images=[image])))
        == []
    )
    clock[0] += 24 * 60 * 60
    replies = await collect(plugin.on_query(StubEvent("公招")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("recruit_with_tag", {"tags": ["近卫干员", "近战位", "输出", "防护", "支援"]})
    ]
    recognize.assert_awaited_once()


@pytest.mark.parametrize("text", ["普通聊天", "阿米娅", "公招", "", "hello"])
def test_unwoken_chat_does_not_activate_plugin_filters(text):
    event = StubEvent(text, wake=False)
    assert not main._QueryFilter().filter(event, None)
    assert not main._ImageFilter().filter(event, None)


def test_plugin_filters_accept_wake_followups_and_image():
    assert main._QueryFilter().filter(StubEvent("干员 阿米娅"), None)
    assert main._QueryFilter().filter(StubEvent("模组", wake=False), None)
    assert main._QueryFilter().filter(StubEvent("选择1", wake=False), None)
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    assert main._ImageFilter().filter(StubEvent(wake=False, images=[image]), None)


@pytest.mark.asyncio
async def test_image_download_failure_opens_recruit_wait_window(subject, monkeypatch):
    plugin, gateway, _, _ = subject
    monkeypatch.setattr(
        main.Image,
        "convert_to_file_path",
        AsyncMock(side_effect=OSError("fetch failed")),
    )
    image = Image.fromURL("https://images.example.invalid/screenshot.png")
    replies = await collect(plugin.on_query(StubEvent("公招", images=[image])))
    assert len(replies) == 1
    assert "30" in text_of(replies[0])
    assert (
        plugin._pending_recruit[("qq_official:GroupMessage:group-a", "user-a")] == 1030
    )
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_explicit_search_includes_plausible_fuzzy_and_exact_items(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "char_amy", "name": "阿米娅", "type": "干员"},
        {"id": "token_amy", "name": "阿米娅的装置", "type": "召唤物"},
        {"id": "other", "name": "完全不相关", "type": "干员"},
    ]
    replies = await collect(plugin.on_query(StubEvent("搜索 阿米娅")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "阿米娅"}),
        ("build_search_card", {"ids": ["char_amy", "token_amy"]}),
    ]
    assert [button.label for button in buttons_of(replies[0])] == ["1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["肉鸽", "集成战略", "藏品"])
async def test_rougelike_prefixes_use_the_shared_item_tool(subject, prefix):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "rogue_1_relic_q36", "name": "王庭盟约", "type": "集成战略藏品"},
    ]
    replies = await collect(plugin.on_query(StubEvent(f"{prefix} 王庭盟约")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "王庭盟约"}),
        ("get_integrated_strategy_item_detail", {"item_id": "rogue_1_relic_q36"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["藏品 王庭盟约", "肉鸽 王庭盟约"])
async def test_collectible_prefixes_use_the_shared_item_tool(subject, text):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "rogue_1_relic_q36", "name": "王庭盟约", "type": "集成战略藏品"},
    ]
    await collect(plugin.on_query(StubEvent(text)))
    assert gateway.calls[-1] == (
        "get_integrated_strategy_item_detail",
        {"item_id": "rogue_1_relic_q36"},
    )


@pytest.mark.asyncio
async def test_bare_strategy_item_name_routes_through_its_bare_type(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [
        {"id": "rogue_5_copper_B_01_a", "name": "大炎通宝", "type": "通宝"},
        {"id": "rogue_9_other", "name": "大炎通宝碎片", "type": "材料"},
    ]
    replies = await collect(plugin.on_query(StubEvent("大炎通宝")))
    assert len(replies) == 1
    assert gateway.calls == [
        ("search", {"query": "大炎通宝"}),
        ("get_integrated_strategy_item_detail", {"item_id": "rogue_5_copper_B_01_a"}),
    ]


@pytest.mark.asyncio
async def test_unrecognized_multiword_query_answers_instead_of_staying_silent(subject):
    plugin, gateway, _, _ = subject
    replies = await collect(plugin.on_query(StubEvent("旧版藏品 王庭盟约")))
    assert len(replies) == 1
    assert "没有找到" in text_of(replies[0])


@pytest.mark.asyncio
async def test_single_token_bare_name_stays_silent_when_unmatched(subject):
    plugin, gateway, _, _ = subject
    replies = await collect(plugin.on_query(StubEvent("不存在的干员")))
    assert replies == []


@pytest.mark.asyncio
async def test_probe_base_rewrites_only_the_local_size_probe(subject):
    plugin, gateway, _, _ = subject
    plugin._probe_base = "http://127.0.0.1:40317"
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    await plugin._http.aclose()
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    plugin._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    result = (await collect(plugin.on_query(StubEvent("干员 阿米娅"))))[0]
    markdown = await _markdown(result)
    assert seen and seen[0].startswith("http://127.0.0.1:40317/")
    assert "images.example.invalid" in markdown


@pytest.mark.asyncio
async def test_unreachable_image_host_keeps_the_card_without_size_hints(subject):
    plugin, gateway, _, _ = subject
    gateway.search_items = [{"id": "char_amy", "name": "阿米娅", "type": "干员"}]
    await plugin._http.aclose()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    plugin._http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    result = (await collect(plugin.on_query(StubEvent("干员 阿米娅"))))[0]
    markdown = await _markdown(result)
    assert f"]({CARD_URL})" in markdown
    assert "px #" not in markdown


async def _markdown(result):
    parsed = await QQOfficialMessageEvent._parse_to_qqofficial(result)
    return parsed[0]


@pytest.mark.asyncio
async def test_probe_base_ignores_a_path_suffix(monkeypatch):
    """A probe base pasted with /mcp keeps only scheme, host, and port."""
    monkeypatch.setattr(main, "MCPGateway", lambda *args, **kwargs: object())
    plugin = main.Main(
        context=None,
        config={"mcp_url": "http://example.invalid/mcp", "image_probe_base": "http://192.168.1.33:40317/mcp"},
    )
    assert plugin._probe_base == "http://192.168.1.33:40317"
    assert plugin._probe_url("http://public.example/cards/a.png") == "http://192.168.1.33:40317/cards/a.png"
    await plugin._http.aclose()
