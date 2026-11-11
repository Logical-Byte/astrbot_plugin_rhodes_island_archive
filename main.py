"""MCP-backed game-data queries for the Rhodes Island Data Archive."""

from __future__ import annotations

import json
import math
import re
import time
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from PIL import ImageFile

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import ActionRow, Button, Image
from astrbot.core.message.message_event_result import MessageEventResult

from .commands import COMMANDS, SUFFIX_PATTERN
from .providers.mcp import MCPError, MCPGateway
from .providers.ocr import OCRError, recognize

_CARD_TTL = 300.0
_RECRUIT_WAIT = 30.0
_RECRUIT_KEYWORD = re.compile(r"^(公开招募|公招|高资|高姿)")
_DISABLED_PREFIXES = ("终末地",)
_TAG_SPLIT = re.compile(r"[\s,，、/|]+")
_TAG_ALIASES = {
    "高资": "高级资深干员",
    "高姿": "高级资深干员",
    "高级资深": "高级资深干员",
    "资深": "资深干员",
}
_PROFESSIONS = ("近卫", "狙击", "重装", "医疗", "辅助", "术师", "特种", "先锋")
_SELECTION = re.compile(r"^选择\s*(\d{1,2})$")


class _QueryFilter(filter.CustomFilter):
    """Keep ordinary non-wake messages from activating the query handler."""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        """Accept wake-prefixed queries and explicit card follow-ups.

        Args:
            event: Incoming message event.
            cfg: AstrBot configuration.

        Returns:
            Whether the query handler should be activated.
        """
        del cfg
        text = event.get_message_str().strip()
        return (
            event.is_at_or_wake_command
            or bool(_SELECTION.fullmatch(text))
            or text in {"模组", "皮肤", "材料", "召唤物"}
        )


class _ImageFilter(filter.CustomFilter):
    """Limit the screenshot listener to events that actually contain an image."""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        """Identify image messages without intercepting other chat messages.

        Args:
            event: Incoming message event.
            cfg: AstrBot configuration.

        Returns:
            Whether the event contains an image component.
        """
        del cfg
        return any(isinstance(component, Image) for component in event.get_messages())


def _buttons(actions: list[dict[str, Any]]) -> list[ActionRow]:
    """Build QQ-compatible button rows without changing their callback deadline.

    Args:
        actions: Callback descriptions with a label and JSON-compatible data.

    Returns:
        At most five rows with five buttons each.
    """
    issued = time.time()
    buttons = [
        Button(
            id=f"ri_{index}",
            label=str(action["label"]),
            style=Button.Style.SUCCESS,
            action=Button.CallbackAction(data={**action["data"], "ts": issued}),
        )
        for index, action in enumerate(actions[:25], 1)
    ]
    return [ActionRow(buttons=buttons[i : i + 5]) for i in range(0, len(buttons), 5)]


def _matches(query: str, items: list[dict[str, Any]]) -> tuple[list, list]:
    """Separate exact matches from plausible fuzzy candidates.

    Args:
        query: User-supplied name, alias or stage code.
        items: MCP search candidates already filtered by requested type.

    Returns:
        Exact and plausible-but-inexact candidates, respectively.
    """
    needle = query.casefold().strip()
    exact, fuzzy = [], []
    for item in items:
        values = [
            str(item.get(key) or "").casefold().strip()
            for key in ("id", "name", "from_alias", "code")
        ]
        if needle in values:
            exact.append(item)
        elif any(
            value
            and (
                needle in value
                or (
                    len(needle) >= 3
                    and SequenceMatcher(None, needle, value).ratio() >= 0.66
                )
            )
            for value in values[1:]
        ):
            fuzzy.append(item)
    return exact, fuzzy


class Main(star.Star):
    """Route game-data requests to the configured MCP server."""

    def __init__(self, context: star.Context, config: dict | None = None):
        """Initialize the gateway and per-session interaction state.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration, if available.
        """
        super().__init__(context, config)
        config = config or {}
        try:
            timeout = float(config.get("timeout", 30.0))
            if not math.isfinite(timeout) or timeout <= 0:
                timeout = 30.0
        except (TypeError, ValueError):
            timeout = 30.0
        self._mcp = MCPGateway(
            str(config.get("mcp_url") or "").strip(), timeout=timeout
        )
        # Optional origin used only to read image sizes when the public host is
        # unreachable from this machine (hairpin NAT). QQ still gets the original
        # URL, and any path in the configured value (such as ``/mcp``) is ignored.
        probe = urlsplit(str(config.get("image_probe_base") or "").strip())
        self._probe_base = f"{probe.scheme}://{probe.netloc}" if probe.netloc else ""
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._dimensions: dict[str, tuple[int, int]] = {}
        self._vocabulary: list[str] | None = None
        self._last_operator: dict[str, tuple[dict[str, Any], float]] = {}
        self._selections: dict[str, tuple[list[dict[str, Any]], float]] = {}
        self._recent_images: dict[tuple[str, str], Image] = {}
        self._pending_recruit: dict[tuple[str, str], float] = {}

    async def terminate(self) -> None:
        """Close both MCP and image-metadata connections on plugin unload."""
        try:
            await self._mcp.close()
        finally:
            await self._http.aclose()

    def _probe_url(self, url: str) -> str:
        """Rewrite an image URL for local size probing only.

        Args:
            url: Public image URL returned by the MCP service.

        Returns:
            The URL to fetch locally, honouring ``image_probe_base`` when set.
        """
        if not self._probe_base:
            return url
        parsed = urlsplit(url)
        if not parsed.path:
            return url
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{self._probe_base}{parsed.path}{query}"

    async def _message(
        self,
        event: AstrMessageEvent,
        text: str,
        url: str = "",
        actions: list[dict[str, Any]] | None = None,
    ) -> MessageEventResult:
        """Compose one reply, keeping QQ Markdown images and buttons together.

        Args:
            event: Incoming message or button event.
            text: Short caption or error message.
            url: Public image URL returned by the MCP service.
            actions: Optional button descriptions.

        Returns:
            A platform-compatible message chain.

        Raises:
            ValueError: If an image URL or its dimensions are invalid.
            httpx.HTTPError: If image metadata cannot be fetched.
        """
        result = MessageEventResult()
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("Invalid MCP image URL")
            if event.get_platform_name().startswith("qq_official"):
                if url not in self._dimensions:
                    try:
                        parser = ImageFile.Parser()
                        received = 0
                        async with self._http.stream(
                            "GET",
                            self._probe_url(url),
                            headers={"Range": "bytes=0-65535"},
                        ) as response:
                            response.raise_for_status()
                            async for chunk in response.aiter_bytes():
                                received += len(chunk)
                                parser.feed(chunk)
                                if parser.image is not None:
                                    width, height = parser.image.size
                                    if 0 < width <= 65535 and 0 < height <= 65535:
                                        self._dimensions[url] = (width, height)
                                    break
                                if received > 2 * 1024 * 1024:
                                    break
                    except (httpx.HTTPError, OSError):
                        # The public host may be unreachable from this machine;
                        # QQ can still fetch the image, so drop the size hints.
                        self.logger.debug("image size probe failed: %s", url)
                safe_url = quote(url, safe=":/%?=&+;,@!$'*-._~%#")
                caption = text.replace("[", "（").replace("]", "）").replace("\n", " ")
                size = self._dimensions.get(url)
                if size:
                    result.message(
                        f"![{caption or '资料卡'} #{size[0]}px #{size[1]}px]({safe_url})"
                    )
                else:
                    result.message(f"![{caption or '资料卡'}]({safe_url})")
                if text:
                    result.message(f"\n{text}")
                result.use_markdown(True)
            else:
                if text:
                    result.message(text)
                result.url_image(url)
        else:
            result.message(text)
        result.chain.extend(_buttons(actions or []))
        return result

    async def _selection_card(
        self,
        event: AstrMessageEvent,
        candidates: list[dict[str, Any]],
        operation: str,
        response: dict[str, Any] | None = None,
    ) -> MessageEventResult:
        """Display MCP's selection image with buttons tied to its item order.

        Args:
            event: Incoming message event.
            candidates: Plausible MCP results, in the desired display order.
            operation: Requested operation, or ``auto`` for type-based routing.
            response: Optional search-card response that was already fetched.

        Returns:
            One image and its numbered selection buttons.
        """
        by_id: dict[str, dict[str, Any]] = {}
        for item in candidates:
            item_id = str(item.get("id") or "")
            if item_id and item_id not in by_id:
                by_id[item_id] = item
            if len(by_id) == 20:
                break
        if not by_id:
            return await self._message(event, "没有找到匹配的结果。")
        if response is None:
            response = await self._mcp.call("build_search_card", {"ids": list(by_id)})
        data = response.get("data") or {}
        returned = data.get("items") if isinstance(data, dict) else None
        if not isinstance(returned, list):
            raise MCPError("MCP 搜索卡缺少候选列表")
        ordered = []
        for value in returned:
            if isinstance(value, dict) and str(value.get("id") or "") in by_id:
                ordered.append({**by_id[str(value["id"])], **value})
        if not ordered:
            raise MCPError("MCP 搜索卡未返回可用候选")
        self._selections[event.unified_msg_origin] = (
            [
                {"action": "select", "operation": operation, "item": item}
                for item in ordered
            ],
            time.time(),
        )
        actions = [
            {
                "label": str(index),
                "data": {"action": "select", "operation": operation, "item": item},
            }
            for index, item in enumerate(ordered, 1)
        ]
        return await self._message(
            event,
            "请选择编号",
            str(response.get("card_image_url") or ""),
            actions,
        )

    async def _lookup(
        self,
        event: AstrMessageEvent,
        query: str,
        operation: str,
        *,
        silent: bool = False,
    ) -> MessageEventResult | None:
        """Resolve names without treating fuzzy search hits as exact matches.

        Args:
            event: Incoming request.
            query: Name, alias, code or upstream ID.
            operation: Requested detail operation, ``search`` or ``auto``.
            silent: Suppress the no-match message for bare character names.

        Returns:
            A result, a selection card, or None for an unmatched bare query.
        """
        query = query.strip()
        if not query:
            return await self._message(event, "请提供名称或 ID。")
        looks_like_id = bool(re.search(r"[_:@#]", query) or query.isdecimal())
        if looks_like_id:
            # Search uses fuzzy matching even for IDs; this tool resolves exact IDs.
            resolved = await self._mcp.call("build_search_card", {"ids": [query]})
            data = resolved.get("data") or {}
            values = data.get("items") if isinstance(data, dict) else None
            items = [
                item
                for item in values or []
                if isinstance(item, dict) and item.get("id") == query
            ]
        else:
            response = await self._mcp.call("search", {"query": query})
            data = response.get("data") or {}
            values = data.get("items") if isinstance(data, dict) else None
            items = [item for item in values or [] if isinstance(item, dict)]
        items = [
            item
            for item in items
            if COMMANDS.accepts(operation, str(item.get("type") or ""))
        ]
        exact, fuzzy = _matches(query, items)
        if operation == "search":
            candidates = [item for item in items if item in exact or item in fuzzy]
        else:
            candidates = exact or fuzzy
        if not candidates:
            return (
                None
                if silent
                else await self._message(event, f"没有找到「{query}」的相关结果。")
            )
        if operation == "search":
            return await self._selection_card(event, candidates, "auto")
        if len(exact) == 1:
            return await self._execute(event, operation, exact[0])
        return await self._selection_card(event, candidates, operation)

    async def _execute(
        self, event: AstrMessageEvent, operation: str, item: dict[str, Any]
    ) -> MessageEventResult:
        """Call the selected MCP detail tool and compose its response.

        Args:
            event: Incoming message or button event.
            operation: Requested detail operation or ``auto``.
            item: Exact MCP search result or previously returned item.

        Returns:
            The service's card, its normal empty-state message, or structured data.

        Raises:
            MCPError: If the selected item cannot be mapped to a service tool.
        """
        kind = str(item.get("type") or "")
        if operation == "auto":
            operation = COMMANDS.operation_for(kind)
        if not COMMANDS.has(operation):
            raise MCPError("该搜索结果没有对应的详情接口")
        item_id = str(item.get("id") or "")
        title = str(item.get("name") or item.get("名称") or item_id)
        skin_id = item_id if operation == "skins" and kind == "皮肤" else ""
        if operation == "skins" and skin_id:
            item_id = str(item.get("operator_id") or "")
        if operation == "token" and kind == "干员":
            basic = await self._mcp.call(
                "get_operator_basic_data", {"operator_id": item_id}
            )
            data = basic.get("data") or {}
            tokens = data.get("召唤物") if isinstance(data, dict) else None
            if not isinstance(tokens, dict):
                return await self._message(event, f"{title} 没有召唤物资料。")
            token_items = [
                {"id": token.get("id"), "name": token.get("名称"), "type": "召唤物"}
                for token in tokens.get("列表") or []
                if isinstance(token, dict) and token.get("id")
            ]
            if len(token_items) == 1:
                return await self._execute(event, "token", token_items[0])
            if token_items:
                return await self._selection_card(event, token_items, "token")
            return await self._message(event, f"{title} 没有召唤物资料。")
        if not item_id:
            raise MCPError("搜索结果缺少详情所需的 ID")
        tool, argument = COMMANDS.tool(operation)
        response = await self._mcp.call(tool, {argument: item_id})
        data = response.get("data") or {}
        url = str(response.get("card_image_url") or "")
        actions: list[dict[str, Any]] = []
        if operation == "operator" and isinstance(data, dict):
            names = data.get("名称")
            if isinstance(names, dict):
                title = str(names.get("中文名") or title)
            operator = {"id": item_id, "name": title, "type": "干员"}
            self._last_operator[event.unified_msg_origin] = (operator, time.time())
            actions = [
                {
                    "label": label,
                    "data": {"action": "select", "operation": op, "item": operator},
                }
                for op, label in (
                    ("modules", "模组"),
                    ("skins", "皮肤"),
                    ("operator_material", "材料"),
                )
            ]
            tokens = data.get("召唤物")
            if isinstance(tokens, dict) and tokens.get("列表"):
                actions.append(
                    {
                        "label": "召唤物",
                        "data": {
                            "action": "select",
                            "operation": "token",
                            "item": operator,
                        },
                    }
                )
        elif operation == "skins" and isinstance(data, dict):
            skins = [skin for skin in data.get("skins") or [] if isinstance(skin, dict)]
            if skin_id:
                chosen = next(
                    (skin for skin in skins if skin.get("id") == skin_id), None
                )
                if chosen is None:
                    raise MCPError("MCP 未返回所选皮肤")
                title = str(chosen.get("名称") or title)
                url = str(chosen.get("card_url") or chosen.get("立绘URL") or "")
            else:
                selections = []
                for skin in skins[:25]:
                    skin_url = str(skin.get("card_url") or skin.get("立绘URL") or "")
                    if skin_url:
                        selections.append(
                            {
                                "action": "image",
                                "url": skin_url,
                                "title": str(skin.get("名称") or "皮肤"),
                            }
                        )
                self._selections[event.unified_msg_origin] = (selections, time.time())
                actions = [
                    {"label": str(index), "data": selection}
                    for index, selection in enumerate(selections, 1)
                ]
                if len(skins) > 25:
                    title += "（前 25 套，可按皮肤名称查询）"
        if url:
            return await self._message(
                event, f"{title} · {COMMANDS.label(operation)}", url, actions
            )
        message = str(response.get("message") or "")
        if message:
            return await self._message(event, message)
        if data:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            if len(text) > 3000:
                text = text[:3000] + "\n……内容较长，已截取前 3000 字符。"
            return await self._message(event, text)
        return await self._message(
            event, f"{title} 暂无{COMMANDS.label(operation)}资料。"
        )

    async def _vocabulary_from_mcp(self) -> list[str]:
        """Read accepted recruitment labels from the live MCP tool schema.

        Returns:
            The server's tag enum, longest first.

        Raises:
            MCPError: If the service does not expose a usable tag enum.
        """
        if self._vocabulary is None:
            schema = await self._mcp.input_schema("recruit_with_tag")
            tags = (
                schema.get("properties", {})
                .get("tags", {})
                .get("items", {})
                .get("enum")
            )
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) for tag in tags
            ):
                raise MCPError("MCP 公招接口没有提供可用的标签列表")
            self._vocabulary = sorted(tags, key=len, reverse=True)
        return self._vocabulary

    async def _recruit(
        self, event: AstrMessageEvent, tags: list[str]
    ) -> MessageEventResult:
        """Send the complete tag list to MCP and report ignored labels.

        Args:
            event: Incoming message event.
            tags: One to five distinct labels supplied by the user or OCR.

        Returns:
            The unmodified MCP result image and any ignored-tag notice.
        """
        vocabulary = await self._vocabulary_from_mcp()
        invalid = [tag for tag in tags if tag not in vocabulary]
        if invalid or not 1 <= len(tags) <= 5:
            return await self._message(event, "请提供 1 至 5 个有效的公招标签。")
        response = await self._mcp.call("recruit_with_tag", {"tags": tags})
        data = response.get("data") or {}
        accepted = data.get("tags") if isinstance(data, dict) else None
        ignored = []
        if isinstance(accepted, list):
            accepted_set = set(accepted)
            for tag in tags:
                normalized = (
                    tag[:-2]
                    if tag.endswith("干员") and tag[:-2] in _PROFESSIONS
                    else tag
                )
                if normalized not in accepted_set:
                    ignored.append(tag)
        notice = "公开招募"
        if ignored:
            notice += "\nMCP 未识别的标签：" + "、".join(ignored)
        url = str(response.get("card_image_url") or "")
        if url:
            return await self._message(event, notice, url)
        return await self._message(event, str(response.get("message") or notice))

    async def _recruit_image(
        self, event: AstrMessageEvent, image: Image
    ) -> MessageEventResult:
        """Recognize five screenshot labels before calling the MCP planner.

        Args:
            event: Event that supplied the screenshot.
            image: Image component from the screenshot.

        Returns:
            The recruitment result or a prompt to provide another image.
        """
        key = (event.unified_msg_origin, str(event.get_sender_id()))
        try:
            path = await image.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001 - image adapters raise varied errors
            self.logger.warning("Recruit screenshot could not be read: %s", exc)
            self._pending_recruit[key] = time.monotonic() + _RECRUIT_WAIT
            return await self._message(event, "截图读取失败，请在 30 秒内再发一张。")
        try:
            text = re.sub(r"\s+", "", await recognize(path))
            vocabulary = await self._vocabulary_from_mcp()
            visible_tags = {tag: tag for tag in vocabulary}
            visible_tags.update(
                {
                    profession: f"{profession}干员"
                    for profession in _PROFESSIONS
                    if f"{profession}干员" in vocabulary
                }
            )
            visible_order = sorted(visible_tags, key=len, reverse=True)
            found: list[str] = []
            index = 0
            while index < len(text):
                for visible in visible_order:
                    if text.startswith(visible, index):
                        tag = visible_tags[visible]
                        if tag not in found:
                            found.append(tag)
                        index += len(visible)
                        break
                else:
                    index += 1
            if len(found) != 5:
                self._pending_recruit[key] = time.monotonic() + _RECRUIT_WAIT
                return await self._message(
                    event,
                    f"识别到 {len(found)} 个标签，请发送包含完整 5 个标签的截图。",
                )
            self._pending_recruit.pop(key, None)
            return await self._recruit(event, found)
        except OCRError as exc:
            if "未安装" in str(exc):
                self._pending_recruit.pop(key, None)
                return await self._message(event, str(exc))
            self._pending_recruit[key] = time.monotonic() + _RECRUIT_WAIT
            return await self._message(
                event, "截图文字识别失败，请在 30 秒内再发一张。"
            )

    @filter.custom_filter(_ImageFilter)
    @filter.event_message_type(
        filter.EventMessageType.ALL,
        desc="公招等待期间收到的截图：识别五个标签并出公开招募卡片",
    )
    async def on_recruit_image(self, event: AstrMessageEvent):
        """Remember the sender's latest image and process pending screenshots.

        Args:
            event: Any incoming message event.

        Yields:
            A recruitment reply when an image completes a pending request.
        """
        images = [
            component
            for component in event.get_messages()
            if isinstance(component, Image)
        ]
        if not images:
            return
        key = (event.unified_msg_origin, str(event.get_sender_id()))
        self._recent_images[key] = images[-1]
        deadline = self._pending_recruit.get(key)
        if deadline is None:
            return
        if time.monotonic() >= deadline:
            self._pending_recruit.pop(key, None)
            return
        if event.is_at_or_wake_command and _RECRUIT_KEYWORD.match(
            event.get_message_str()
        ):
            return
        try:
            yield await self._recruit_image(event, images[-1])
        except Exception as exc:  # noqa: BLE001 - isolate upstream event failures
            self.logger.warning("Recruit screenshot failed: %s", exc, exc_info=True)
            yield event.plain_result("公招查询暂时失败，请稍后重试。")

    @filter.custom_filter(_QueryFilter)
    @filter.regex(
        r"^[^\n]{1,128}$",
        desc=(
            "查询明日方舟数据：干员、材料、模组、皮肤、召唤物、关卡、敌人、"
            "公开招募、肉鸽、搜索、术语"
        ),
    )
    async def on_query(self, event: AstrMessageEvent):
        """Route a wake-prefixed query or an active numbered selection.

        Args:
            event: Incoming message event.

        Yields:
            An MCP-backed result when the text matches a supported request.
        """
        text = event.get_message_str().strip()
        text = re.sub(r"^干员\s+(材料|模组|皮肤|技能)", r"干员\1", text)
        selection = _SELECTION.fullmatch(text)
        panel_word = text in {"模组", "皮肤", "材料", "召唤物"}
        if not event.is_at_or_wake_command and not selection:
            saved = self._last_operator.get(event.unified_msg_origin)
            if saved is not None and time.time() - saved[1] >= _CARD_TTL:
                self._last_operator.pop(event.unified_msg_origin, None)
                saved = None
            if not panel_word or saved is None:
                return
        operation = "auto"
        try:
            if selection:
                saved = self._selections.get(event.unified_msg_origin)
                number = int(selection.group(1))
                if saved is None or time.time() - saved[1] >= _CARD_TTL:
                    self._selections.pop(event.unified_msg_origin, None)
                    return
                if not 1 <= number <= len(saved[0]):
                    yield event.plain_result("请选择图片中已有的编号。")
                    return
                action = saved[0][number - 1]
                if action.get("action") == "image":
                    yield await self._message(
                        event,
                        str(action.get("title") or "皮肤"),
                        str(action.get("url") or ""),
                    )
                else:
                    yield await self._execute(
                        event, str(action.get("operation") or "auto"), action["item"]
                    )
                return
            if text.startswith(("技能", "干员技能")) or text.endswith("技能"):
                return
            if text.startswith(_DISABLED_PREFIXES):
                return
            query = text
            resolved = COMMANDS.resolve(text)
            if resolved:
                operation, query = resolved
            if operation == "auto":
                suffix = SUFFIX_PATTERN.fullmatch(text)
                if suffix:
                    query = suffix.group(1)
                    operation = COMMANDS.suffix_operation(suffix.group(2))
            if text == "材料" and not event.is_at_or_wake_command:
                operation = "operator_material"
            if operation == "recruit":
                key = (event.unified_msg_origin, str(event.get_sender_id()))
                match = _RECRUIT_KEYWORD.match(text)
                tags = (
                    ["高级资深干员"]
                    if match and match.group(1) in ("高资", "高姿")
                    else []
                )
                for raw in _TAG_SPLIT.split(query):
                    if not raw:
                        continue
                    tag = _TAG_ALIASES.get(raw, raw)
                    if tag in _PROFESSIONS:
                        tag += "干员"
                    if tag not in tags:
                        tags.append(tag)
                if tags:
                    self._pending_recruit.pop(key, None)
                    yield await self._recruit(event, tags)
                    return
                images = [
                    component
                    for component in event.get_messages()
                    if isinstance(component, Image)
                ]
                image = images[-1] if images else self._recent_images.get(key)
                if image is not None:
                    yield await self._recruit_image(event, image)
                else:
                    self._pending_recruit[key] = time.monotonic() + _RECRUIT_WAIT
                    yield event.plain_result(
                        "请发送公招截图，30 秒内有效；也可以直接输入标签。"
                    )
                return
            if operation == "search_card":
                ids = list(
                    dict.fromkeys(value for value in _TAG_SPLIT.split(query) if value)
                )
                if not 1 <= len(ids) <= 20:
                    yield event.plain_result("请提供 1 至 20 个不同的结果 ID。")
                    return
                result = await self._mcp.call("build_search_card", {"ids": ids})
                data = result.get("data") or {}
                items = data.get("items") if isinstance(data, dict) else []
                yield await self._selection_card(event, items or [], "auto", result)
                return
            if operation == "glossary":
                names = [value for value in _TAG_SPLIT.split(query) if value]
                if not names:
                    yield event.plain_result("请提供要查询的术语。")
                    return
                result = await self._mcp.call(
                    "get_glossary",
                    {"glossary_name": names[0] if len(names) == 1 else names},
                )
                yield await self._message(
                    event,
                    json.dumps(result, ensure_ascii=False, indent=2)
                    if result
                    else "MCP 未返回该术语的解释。",
                )
                return
            if (
                operation in ("modules", "skins", "operator_material", "token")
                and not query
            ):
                saved = self._last_operator.get(event.unified_msg_origin)
                if saved is None or time.time() - saved[1] >= _CARD_TTL:
                    self._last_operator.pop(event.unified_msg_origin, None)
                    yield event.plain_result("请在指令后提供干员名称。")
                    return
                yield await self._execute(event, operation, saved[0])
                return
            if operation == "auto" and not text:
                return
            # Single-token bare names stay silent; anything else answers explicitly.
            result = await self._lookup(
                event,
                query,
                operation,
                silent=operation == "auto" and not re.search(r"\s", text),
            )
            if result is not None:
                yield result
        except MCPError as exc:
            self.logger.warning("MCP query failed: %r", exc.__cause__ or exc)
            if operation != "auto":
                yield event.plain_result(str(exc))
        except Exception as exc:  # noqa: BLE001 - isolate upstream event failures
            self.logger.warning("Game-data query failed: %s", exc, exc_info=True)
            yield event.plain_result("查询暂时失败，请稍后重试。")

    @filter.button_action(
        desc="干员卡与选择卡上的按钮：切换模组、皮肤、材料、召唤物或候选项",
    )
    async def on_button(self, event: AstrMessageEvent):
        """Reply to this plugin's non-expired card buttons.

        Args:
            event: Portable button interaction.

        Yields:
            The selected MCP-backed result.
        """
        interaction = event.get_button_interaction()
        if interaction is None or not interaction.action_id.startswith("ri_"):
            return
        data = interaction.data
        if not isinstance(data, dict):
            return
        try:
            issued = float(data.get("ts") or 0)
        except (TypeError, ValueError):
            return
        age = time.time() - issued
        if not math.isfinite(issued) or issued <= 0 or age >= _CARD_TTL or age < -5:
            return
        try:
            if data.get("action") == "image":
                yield await self._message(
                    event, str(data.get("title") or "皮肤"), str(data.get("url") or "")
                )
            elif data.get("action") == "select" and isinstance(data.get("item"), dict):
                yield await self._execute(
                    event, str(data.get("operation") or "auto"), data["item"]
                )
        except MCPError as exc:
            self.logger.warning("MCP button query failed: %r", exc.__cause__ or exc)
            yield event.plain_result(str(exc))
        except Exception as exc:  # noqa: BLE001 - isolate upstream event failures
            self.logger.warning("Card button failed: %s", exc, exc_info=True)
            yield event.plain_result("查询暂时失败，请稍后重试。")
