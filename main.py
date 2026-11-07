"""Entry point of the Rhodes Island Data Archive plugin."""

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import ActionRow, Button, Image
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult

from .models import GameDataEntry
from .providers.arknights import ArknightsProvider, match_recruit_tags
from .providers.base import GameDataProvider
from .providers.card import (
    render_card,
    render_panel,
    render_recruit,
    render_token_card,
)
from .providers.endfield import EndfieldProvider
from .providers.net import DEFAULT_USER_AGENT, NetClient, UpstreamError
from .providers.ocr import OCRError, recognize
from .providers.render import LocalRenderer, browsers_dir

#: Directory shipped with the plugin, holding the template font and texture.
ASSETS_DIR = Path(__file__).parent / "assets"


def _number(config: dict, key: str, default: float, cast: type) -> float:
    """Read a numeric option, falling back to its default when invalid.

    Args:
        config: Plugin configuration mapping.
        key: Option name.
        default: Value used when the option is absent or unusable.
        cast: Numeric type applied to the configured value.

    Returns:
        The parsed option value.
    """
    try:
        return cast(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _store_image(body: bytes, target: Path) -> Path:
    """Write a rendered image into the cache exactly as it was produced.

    The image is stored untouched so later features can rely on the original
    pixels.

    Args:
        body: Raw PNG bytes.
        target: Destination path.

    Returns:
        The path of the stored image.

    Raises:
        OSError: If the cache file cannot be written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # A unique temporary name keeps concurrent replies from clobbering.
    tmp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(body)
    os.replace(tmp_path, target)
    return target


def _valid_png(body: bytes) -> bool:
    """Tell a rendered card from an error page stored with a ``.png`` name.

    A render backend replies with whatever bytes it has, so a gateway error can
    be written to a ``.png`` as well. Such a body must never be sent or cached.

    Args:
        body: Bytes returned by the renderer.

    Returns:
        ``True`` when the payload carries a PNG signature and plausibly sized
        content.
    """
    return body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) > 8192


#: The keyword that opens the recruitment planner.
#: Words that open a recruitment query; the last two double as the tag.
_RECRUIT_KEYWORD = re.compile(r"^(?:公开招募|公招|高资|高姿)")

#: Trigger words that also stand for the 高级资深干员 tag.
_RECRUIT_LEAD_TAGS = {"高资": "高级资深干员", "高姿": "高级资深干员"}

#: How long the plugin keeps waiting for a recruitment screenshot.
_RECRUIT_WAIT = 30.0

#: How long a character card keeps answering its sub-query buttons.
_CARD_TTL = 300.0

#: Separators players use between tags.
_TAG_SPLIT = re.compile(r"[\s,，、/|]+")

#: Short forms mapped onto the tags the data uses.
_TAG_ALIASES = {
    "高资": "高级资深干员",
    "高姿": "高级资深干员",
    "高级资深": "高级资深干员",
    "资深": "资深干员",
}


def _recruit_tags(text: str) -> list[str]:
    """Extract recruitment tags from a message.

    Args:
        text: Message text, with or without the leading keyword.

    Returns:
        Known tags in the order they were typed, aliases resolved.
    """
    match = _RECRUIT_KEYWORD.match(text)
    body = _RECRUIT_KEYWORD.sub("", text, count=1)
    tags: list[str] = []
    lead = _RECRUIT_LEAD_TAGS.get(match.group(0)) if match else None
    if lead:
        tags.append(lead)
    for token in _TAG_SPLIT.split(body):
        token = token.strip()
        if not token:
            continue
        tag = _TAG_ALIASES.get(token, token)
        if tag not in tags:
            tags.append(tag)
    return tags


#: Buttons attached under a character card, as ``(operation, label)``.
_OPERATOR_ACTIONS = (
    ("module", "模组"),
    ("skin", "皮肤"),
    ("story", "档案"),
    ("faction", "阵营"),
    ("voice", "语音"),
)

#: Platforms that can render an inline keyboard under an image.
_BUTTON_PLATFORMS = ("qq_official",)


#: Words that open a sub-page of the operator card, mapped to their operation.
_PANEL_WORDS = {
    "模组": "module",
    "皮肤": "skin",
    "立绘": "skin",
    "档案": "story",
    "资料": "story",
    "阵营": "faction",
    "语音": "voice",
}

#: Pattern for the sub-page words; the 干员 prefix is optional.
_PANEL_WORD = re.compile(r"^(?:干员\s*)?(?:" + "|".join(_PANEL_WORDS) + r")$")

#: Material archive query, e.g. ``材料 炽合金``.
_MATERIAL_WORD = re.compile(r"^材料\s*")

#: Operator material query, e.g. ``干员材料 迷迭香``.
_OPERATOR_MATERIAL_WORD = re.compile(r"^干员\s*材料\s*")

#: Suffix form, e.g. ``迷迭香材料``.
_MATERIAL_SUFFIX = re.compile(r"^([^\s/]{1,24})材料$")

#: Skill archive query, e.g. ``技能 思维膨大``.
_SKILL_WORD = re.compile(r"^技能\s*")

#: Operator skill query, e.g. ``干员技能 迷迭香``.
_OPERATOR_SKILL_WORD = re.compile(r"^干员\s*技能\s*")

#: Suffix form, e.g. ``迷迭香技能``.
_SKILL_SUFFIX = re.compile(r"^([^\s/]{1,24})技能$")


def _operator_buttons(entry: GameDataEntry) -> ActionRow:
    """Build the keyboard that rides inside the markdown card.

    Args:
        entry: The character the buttons belong to.

    Returns:
        One row with a button per operator sub-query.
    """
    return ActionRow(
        buttons=[
            Button(
                id=f"ri_{operation}",
                label=label,
                style=Button.Style.SUCCESS,
                action=Button.CallbackAction(
                    data={
                        "op": operation,
                        "game": entry.game,
                        "id": entry.entry_id,
                        "ts": int(time.time()),
                    }
                ),
            )
            for operation, label in _OPERATOR_ACTIONS
        ],
        fallback_text="模组 皮肤 档案 阵营 语音",
    )


def _card_message(entry: GameDataEntry) -> MessageEventResult:
    """Build the message that carries the button row under a character card.

    Args:
        entry: The character the card belongs to.

    Returns:
        A markdown message with one button per operator sub-query.
    """
    result = MessageEventResult().message(f"{entry.title} · 相关资料")
    result.use_markdown(True)
    result.chain.append(_operator_buttons(entry))
    return result


class Main(star.Star):
    """Own the Arknights and Endfield providers for the plugin lifetime.

    A single silent handler answers a bare character name that follows the
    wake word. Anything that does not resolve to exactly one character
    produces no output at all.
    """

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        """Build the shared network client and both providers.

        Args:
            context: The AstrBot plugin context.
            config: Plugin configuration parsed from ``_conf_schema.json``.
        """
        super().__init__(context, config)
        config = config or {}
        self._render_timeout = _number(config, "render_timeout", 30.0, float)
        data_dir: Path | None = None
        try:
            data_dir = star.StarTools.get_data_dir(
                "astrbot_plugin_rhodes_island_archive"
            )
        except (RuntimeError, ValueError, OSError) as exc:
            self.logger.warning("Disk cache unavailable, keeping memory only: %s", exc)
        cache_dir = data_dir / "cache" if data_dir is not None else None
        self.net = NetClient(
            ("torappu.prts.wiki", "data.akedata.wiki"),
            timeout=_number(config, "timeout", 20.0, float),
            cache_ttl=_number(config, "cache_ttl", 600, float),
            max_bytes=int(_number(config, "max_download_mb", 32, float) * 1024 * 1024),
            user_agent=str(config.get("user_agent") or DEFAULT_USER_AGENT),
            cache_dir=cache_dir,
            logger=self.logger,
        )
        self.arknights = ArknightsProvider(self.net)
        self.endfield = EndfieldProvider(self.net)
        self._image_dir = cache_dir / "images" if cache_dir is not None else None
        # QQ turns inline keyboard clicks into their label text, so remember
        # which operator each session last looked up, and when.
        self._last_operator: dict[str, tuple[str, str, float]] = {}
        # Sessions that asked for a recruitment screenshot, with the
        # monotonic deadline of their waiting window.
        self._pending_recruit: dict[str, float] = {}
        self._renderer: LocalRenderer | None = None
        self._renderer_ready = False
        if data_dir is not None:
            self._renderer = LocalRenderer(
                ASSETS_DIR,
                cache_dir / "render",
                timeout=self._render_timeout,
                browsers=browsers_dir(data_dir),
                logger=self.logger,
            )

    async def _local_renderer(self) -> LocalRenderer | None:
        """Start the local browser once, if this installation supports it.

        Returns:
            The running renderer, or ``None`` when Playwright or its Chromium
            build is unavailable and the remote service has to be used.
        """
        if self._renderer is None or self._renderer_ready:
            return self._renderer
        self._renderer_ready = True
        try:
            await self._renderer.start()
        except Exception as exc:  # noqa: BLE001 - any failure means "fallback"
            self.logger.warning(
                "local renderer unavailable, falling back to the AstrBot service: "
                "%s: %s",
                type(exc).__name__,
                exc,
            )
            self._renderer = None
        return self._renderer

    async def _remote_render(self, html: str, entry: GameDataEntry) -> bytes:
        """Render through the AstrBot text-to-image service.

        Args:
            html: Complete HTML document.
            entry: The character being rendered, used for logging.

        Returns:
            The rendered PNG bytes.

        Raises:
            UpstreamError: If the service never returns a usable PNG.
        """
        started = time.monotonic()
        # The render service occasionally answers with a gateway error page or
        # times out; one retry covers those, anything else is reported.
        for attempt in (1, 2):
            try:
                path = await self.context.html_renderer.render_custom_template(
                    html,
                    {},
                    return_url=False,
                    options={"full_page": True, "type": "png"},
                )
                body = Path(path).read_bytes()
            except Exception as exc:
                # The render service fails in opaque ways (timeout, 502, reset),
                # so the payload size is logged with the error.
                self.logger.warning(
                    "card %s/%s: render attempt %d failed after %.2fs, "
                    "html %d bytes: %s: %s",
                    entry.game,
                    entry.entry_id,
                    attempt,
                    time.monotonic() - started,
                    len(html),
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                if attempt == 2:
                    raise
                continue
            if _valid_png(body):
                return body
            self.logger.warning(
                "card %s/%s: render attempt %d returned %d bytes that are not "
                "a PNG: %r",
                entry.game,
                entry.entry_id,
                attempt,
                len(body),
                body[:120],
            )
        raise UpstreamError("Render service returned no usable PNG")

    async def _render(
        self,
        html: str,
        entry: GameDataEntry,
        *,
        size: tuple[int, int] | None = None,
    ) -> bytes:
        """Render the card, preferring the local browser.

        Args:
            html: Complete HTML document.
            entry: The character being rendered, used for logging.
            size: Optional viewport size for pages that are not 1400x900.

        Returns:
            The rendered PNG bytes.
        """
        renderer = await self._local_renderer()
        if renderer is not None:
            started = time.monotonic()
            try:
                body = await renderer.render(
                    html, **(dict(zip(("width", "height"), size)) if size else {})
                )
                if _valid_png(body):
                    self.logger.debug(
                        "card %s/%s: local render in %.2fs",
                        entry.game,
                        entry.entry_id,
                        time.monotonic() - started,
                    )
                    return body
                self.logger.warning(
                    "card %s/%s: local render returned %d bytes that are not a PNG",
                    entry.game,
                    entry.entry_id,
                    len(body),
                )
            except Exception as exc:  # noqa: BLE001 - fall back to the service
                self.logger.warning(
                    "card %s/%s: local render failed after %.2fs: %s: %s",
                    entry.game,
                    entry.entry_id,
                    time.monotonic() - started,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
        return await self._remote_render(html, entry)

    async def _card_image(
        self, provider: GameDataProvider, entry: GameDataEntry
    ) -> str:
        """Render the character card and return the PNG path to send.

        The rendered PNG is cached per character so a repeated lookup skips
        both the table reads and the rendering itself.

        Args:
            provider: Provider that owns the character.
            entry: The matched character.

        Returns:
            Path of the rendered PNG.

        Raises:
            UpstreamError: If no backend produced a usable PNG.
            OSError: If the rendered file cannot be read or written.
        """
        target = None
        if self._image_dir is not None:
            # The version prefix invalidates cards rendered by older layouts.
            target = self._image_dir / f"card_v16_{entry.game}_{entry.entry_id}.png"
            if target.is_file():
                cached = target.read_bytes()
                if _valid_png(cached):
                    self.logger.info(
                        "card %s/%s: cache hit %s (%d bytes)",
                        entry.game,
                        entry.entry_id,
                        target.name,
                        len(cached),
                    )
                    return str(target)
                self.logger.warning(
                    "card %s/%s: dropping invalid cache %s (%d bytes: %r)",
                    entry.game,
                    entry.entry_id,
                    target.name,
                    len(cached),
                    cached[:80],
                )
                target.unlink(missing_ok=True)

        started = time.monotonic()
        self.logger.info("card %s/%s: collecting data", entry.game, entry.entry_id)
        card = await provider.card(entry)
        collected = time.monotonic()
        self.logger.debug(
            "card %s/%s: art inlined=%s skills=%d devices=%d",
            entry.game,
            entry.entry_id,
            bool(card.get("art")),
            len(card.get("skills") or []),
            len(card.get("devices") or []),
        )

        renderer = await self._local_renderer()
        html = render_card(card, font=renderer.font_family if renderer else None)
        rendered_started = time.monotonic()
        body = await self._render(html, entry)
        rendered = time.monotonic()
        self.logger.info(
            "card %s/%s: data %.2fs, html %.0f KB, render %.2fs, png %.0f KB",
            entry.game,
            entry.entry_id,
            collected - started,
            len(html) / 1024,
            rendered - rendered_started,
            len(body) / 1024,
        )
        if target is None:
            # Without a data directory the image still has to exist on disk for
            # the platform layer to upload it, so it goes to the temp directory.
            self.logger.debug(
                "card %s/%s: no cache dir, returning temp file",
                entry.game,
                entry.entry_id,
            )
            target = Path(tempfile.gettempdir()) / f"card_{entry.entry_id}.png"
        return str(_store_image(body, target))

    async def _token_image(
        self, provider: GameDataProvider, entry: GameDataEntry
    ) -> str | None:
        """Render the separate device page of one character.

        Args:
            provider: Provider that owns the character.
            entry: The matched character.

        Returns:
            Path of the rendered PNG, or ``None`` when the character deploys
            no device or the page cannot be produced.
        """
        target = None
        if self._image_dir is not None:
            target = self._image_dir / f"token_v2_{entry.game}_{entry.entry_id}.png"
            if target.is_file():
                cached = target.read_bytes()
                if _valid_png(cached):
                    self.logger.info(
                        "token page %s/%s: cache hit %s (%d bytes)",
                        entry.game,
                        entry.entry_id,
                        target.name,
                        len(cached),
                    )
                    return str(target)
                target.unlink(missing_ok=True)

        started = time.monotonic()
        page = await provider.token_card(entry)
        if not page:
            return None
        renderer = await self._local_renderer()
        html = render_token_card(page, font=renderer.font_family if renderer else None)
        body = await self._render(html, entry, size=(1280, 720))
        self.logger.info(
            "token page %s/%s: %d device(s), html %.0f KB, png %.0f KB in %.2fs",
            entry.game,
            entry.entry_id,
            len(page.get("tokens") or []),
            len(html) / 1024,
            len(body) / 1024,
            time.monotonic() - started,
        )
        if target is None:
            target = Path(tempfile.gettempdir()) / f"token_{entry.entry_id}.png"
        return str(_store_image(body, target))

    async def _first_image(self, event: AstrMessageEvent) -> str | None:
        """Return the local path of the first image in one message.

        Args:
            event: The message event to inspect.

        Returns:
            Absolute file path, or ``None`` when the message carries no image.
        """
        for component in event.get_messages():
            if isinstance(component, Image):
                try:
                    return await component.convert_to_file_path()
                except Exception as exc:  # noqa: BLE001 - download may fail
                    self.logger.warning("image download failed: %s", exc)
                    return None
        return None

    async def _recruit_image(self, page: dict[str, Any]) -> str:
        """Render the recruitment result page, reusing cached images.

        Args:
            page: Result mapping from ``ArknightsProvider.recruit``.

        Returns:
            Path of the rendered PNG.
        """
        key = hashlib.sha1(
            "|".join(sorted(page.get("tags") or [])).encode("utf-8")
        ).hexdigest()[:16]
        target = None
        if self._image_dir is not None:
            target = self._image_dir / f"recruit_{key}.png"
            if target.is_file():
                body = target.read_bytes()
                if _valid_png(body):
                    self.logger.info(
                        "recruit %s: cache hit %s (%d bytes)",
                        key,
                        target.name,
                        len(body),
                    )
                    return str(target)
                target.unlink(missing_ok=True)

        renderer = await self._local_renderer()
        html = render_recruit(page, font=renderer.font_family if renderer else None)
        started = time.monotonic()
        body = await self._render(
            html, GameDataEntry("arknights", "recruit", key, key), size=(1280, 720)
        )
        self.logger.info(
            "recruit %s: %d group(s), html %.0f KB, png %.0f KB in %.2fs",
            key,
            len(page.get("groups") or []),
            len(html) / 1024,
            len(body) / 1024,
            time.monotonic() - started,
        )
        if target is None:
            path = Path(tempfile.gettempdir()) / f"recruit_{key}.png"
            path.write_bytes(body)
            return str(path)
        return str(_store_image(body, target))

    async def _tags_from_image(self, image_path: str) -> list[str]:
        """Read recruitment tags out of a screenshot.

        Args:
            image_path: Local path of the screenshot.

        Returns:
            Recognised tags, empty when the image holds none.

        Raises:
            OCRError: If the local OCR backend is unavailable.
        """
        text = await recognize(image_path)
        tags = match_recruit_tags(text, await self.arknights.recruit_vocabulary())
        self.logger.info("recruit OCR text: %r -> %s", text, tags)
        return tags

    async def _recruit_answer(self, event: AstrMessageEvent, tags: list[str]):
        """Render the page for one tag set.

        Args:
            event: The message event being answered.
            tags: Recruitment tags to solve.

        Yields:
            The rendered page, or a short hint when nothing is available.
        """
        try:
            page = await self.arknights.recruit(tags)
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning(
                "recruit %s failed: %s: %s", tags, type(exc).__name__, exc
            )
            return
        if not page.get("candidates"):
            yield event.plain_result("博士，无法查询到标签所拥有的稀有干员")
            return
        if not page.get("groups"):
            yield event.plain_result("博士，没有找到可以锁定稀有干员的组合")
            return
        try:
            image = await self._recruit_image(page)
        except Exception as exc:  # noqa: BLE001 - rendering must not crash
            self.logger.warning(
                "recruit render failed: %s: %s", type(exc).__name__, exc
            )
            return
        yield event.image_result(image)

    def _wait_for_image(self, event: AstrMessageEvent) -> None:
        """Remember that this session owes the plugin a screenshot.

        Args:
            event: The message event that started the wait.
        """
        self._pending_recruit[event.unified_msg_origin] = (
            time.monotonic() + _RECRUIT_WAIT
        )

    @filter.regex(r"^(?:公开招募|公招|高资|高姿)")
    async def on_recruit(self, event: AstrMessageEvent):
        """Answer a recruitment request with the tag combination page.

        Args:
            event: The message event carrying the tags.

        Yields:
            The rendered page, or a short hint when no tag was recognised.
        """
        if not event.is_at_or_wake_command:
            return
        tags = _recruit_tags(event.get_message_str())
        if tags:
            async for result in self._recruit_answer(event, tags):
                yield result
            return

        image_path = await self._first_image(event)
        if image_path is None:
            self._wait_for_image(event)
            yield event.plain_result(
                f"请发送公招界面截图，{_RECRUIT_WAIT:.0f} 秒内有效；"
                "也可以直接写标签，例如：公招 资深 输出"
            )
            return
        try:
            tags = await self._tags_from_image(image_path)
        except OCRError as exc:
            self.logger.warning("recruit OCR failed: %s", exc)
            if "未安装" in str(exc):
                yield event.plain_result(str(exc))
                return
            self._wait_for_image(event)
            yield event.plain_result(
                "博士，这张图没读出文字，请再发一张；也可以直接写标签"
            )
            return
        if not tags:
            self._wait_for_image(event)
            yield event.plain_result("博士，没有在图内找到标签信息，请再发一张截图")
            return
        async for result in self._recruit_answer(event, tags):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_recruit_image(self, event: AstrMessageEvent):
        """Read a follow-up screenshot while a recruitment wait is open.

        Args:
            event: Any incoming message; only images are considered.

        Yields:
            The rendered page once the screenshot yields tags.
        """
        origin = event.unified_msg_origin
        deadline = self._pending_recruit.get(origin)
        if deadline is None:
            return
        if time.monotonic() > deadline:
            self._pending_recruit.pop(origin, None)
            return
        image_path = await self._first_image(event)
        if image_path is None:
            return
        try:
            tags = await self._tags_from_image(image_path)
        except OCRError as exc:
            self.logger.warning("recruit OCR failed: %s", exc)
            if "未安装" in str(exc):
                self._pending_recruit.pop(origin, None)
                yield event.plain_result(str(exc))
                return
            self._wait_for_image(event)
            yield event.plain_result("博士，这张图没读出文字，请再发一张")
            return
        if not tags:
            # Keep the window open so the user can send another screenshot.
            self._wait_for_image(event)
            yield event.plain_result("博士，没有在图内找到标签信息，请再发一张截图")
            return
        self._pending_recruit.pop(origin, None)
        async for result in self._recruit_answer(event, tags):
            yield result

    @filter.regex(_OPERATOR_MATERIAL_WORD)
    async def on_operator_material(self, event: AstrMessageEvent):
        """Answer 干员材料 with the materials one operator needs.

        Args:
            event: The message event carrying the operator name.

        Yields:
            The rendered material page, or a short hint.
        """
        query = _OPERATOR_MATERIAL_WORD.sub(
            "", event.get_message_str(), count=1
        ).strip()
        try:
            if query:
                entry = await self.arknights.match(query)
                if entry is None:
                    yield event.plain_result(f"没有找到干员「{query}」")
                    return
            else:
                key = getattr(event, "unified_msg_origin", "") or event.get_session_id()
                remembered = self._remembered(key)
                if remembered is None:
                    yield event.plain_result(
                        "请在「干员材料」后面跟上干员名，例如：干员材料 迷迭香"
                    )
                    return
                entry = await self.arknights.get_detail(remembered[1])
            panel = await self.arknights.operator_material_panel(entry)
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning("operator material %s failed: %s", query, exc)
            return
        if not panel:
            yield event.plain_result(f"{entry.title} 没有养成材料数据")
            return
        panel["operation"] = "material"
        try:
            image = await self._panel_image(self.arknights, entry, panel)
        except Exception as exc:  # noqa: BLE001 - rendering must not crash
            self.logger.warning("material page failed: %s", exc)
            return
        yield event.image_result(image)

    @filter.regex(_MATERIAL_SUFFIX)
    async def on_material_suffix(self, event: AstrMessageEvent):
        """Answer ``<干员名>材料`` with that operator's material list.

        Args:
            event: The message event carrying the operator name and suffix.

        Yields:
            The rendered material page, or a hint when the name is unknown.
        """
        match = _MATERIAL_SUFFIX.match(event.get_message_str().strip())
        if match is None:
            return
        name = match.group(1).strip()
        try:
            entry = await self.arknights.match(name)
            if entry is None:
                panel = await self.arknights.material_panel(name)
                if panel is None:
                    yield event.plain_result(f"没有找到干员「{name}」")
                    return
                item = GameDataEntry(
                    game="arknights",
                    category="material",
                    entry_id=str(panel.get("entry_id") or name),
                    title=str(panel.get("title") or name),
                )
                panel["operation"] = "material"
                image = await self._panel_image(self.arknights, item, panel)
                yield event.image_result(image)
                return
            panel = await self.arknights.operator_material_panel(entry)
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning("material suffix %r failed: %s", name, exc)
            return
        if not panel:
            yield event.plain_result(f"{entry.title} 没有养成材料数据")
            return
        panel["operation"] = "material"
        try:
            image = await self._panel_image(self.arknights, entry, panel)
        except Exception as exc:  # noqa: BLE001 - rendering must not crash
            self.logger.warning("material page failed: %s", exc)
            return
        yield event.image_result(image)

    async def _skill_answer(self, event: AstrMessageEvent, query: str):
        """Answer a skill request for an operator name or a skill name.

        Args:
            event: The message event being answered.
            query: Operator name, or a skill name when no operator matches.

        Yields:
            The rendered skill page, or a short hint.
        """
        try:
            entry = await self.arknights.match(query)
            if entry is not None:
                panel = await self.arknights.operator_skill_panel(entry)
                target = entry
            else:
                panel = await self.arknights.skill_panel(query)
                target = GameDataEntry(
                    game="arknights",
                    category="skill",
                    entry_id=str((panel or {}).get("entry_id") or query),
                    title=str((panel or {}).get("title") or query),
                )
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning("skill %r failed: %s", query, exc)
            return
        if not panel:
            yield event.plain_result(f"没有找到技能「{query}」")
            return
        panel["operation"] = "skill"
        try:
            image = await self._panel_image(self.arknights, target, panel)
        except Exception as exc:  # noqa: BLE001 - rendering must not crash
            self.logger.warning("skill page failed: %s", exc)
            return
        yield event.image_result(image)

    @filter.regex(_OPERATOR_SKILL_WORD)
    async def on_operator_skill(self, event: AstrMessageEvent):
        """Answer ``干员技能 <name>`` with that operator's skills.

        Args:
            event: The message event carrying the operator name.

        Yields:
            The rendered skill page.
        """
        query = _OPERATOR_SKILL_WORD.sub("", event.get_message_str(), count=1).strip()
        if not query:
            yield event.plain_result(
                "请在「干员技能」后面跟上干员名，例如：干员技能 迷迭香"
            )
            return
        async for item in self._skill_answer(event, query):
            yield item

    @filter.regex(_SKILL_SUFFIX)
    async def on_skill_suffix(self, event: AstrMessageEvent):
        """Answer ``<name>技能`` for an operator or a skill.

        Args:
            event: The message event carrying the name and suffix.

        Yields:
            The rendered skill page.
        """
        match = _SKILL_SUFFIX.match(event.get_message_str().strip())
        if match is None:
            return
        async for item in self._skill_answer(event, match.group(1).strip()):
            yield item

    @filter.regex(_SKILL_WORD)
    async def on_skill(self, event: AstrMessageEvent):
        """Answer ``技能 <name>`` for an operator or a skill.

        Args:
            event: The message event carrying the name.

        Yields:
            The rendered skill page.
        """
        query = _SKILL_WORD.sub("", event.get_message_str(), count=1).strip()
        if not query:
            yield event.plain_result("请在「技能」后面跟上名称，例如：技能 迷迭香")
            return
        async for item in self._skill_answer(event, query):
            yield item

    @filter.regex(_MATERIAL_WORD)
    async def on_material(self, event: AstrMessageEvent):
        """Answer 材料 with a material archive page.

        Args:
            event: The message event carrying the material name.

        Yields:
            The rendered material page, or a short hint.
        """
        query = _MATERIAL_WORD.sub("", event.get_message_str(), count=1).strip()
        if not query:
            yield event.plain_result("请在「材料」后面跟上名称，例如：材料 炽合金")
            return
        try:
            panel = await self.arknights.material_panel(query)
            if panel is None:
                entry = await self.arknights.match(query)
                if entry is not None:
                    panel = await self.arknights.operator_material_panel(entry)
                    if panel:
                        panel["operation"] = "material"
                        image = await self._panel_image(self.arknights, entry, panel)
                        yield event.image_result(image)
                        return
                yield event.plain_result(f"没有找到材料「{query}」")
                return
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning("material %s failed: %s", query, exc)
            return
        entry = GameDataEntry(
            game="arknights",
            category="material",
            entry_id=str(panel.get("entry_id") or query),
            title=str(panel.get("title") or query),
        )
        panel["operation"] = "material"
        try:
            image = await self._panel_image(self.arknights, entry, panel)
        except Exception as exc:  # noqa: BLE001 - rendering must not crash
            self.logger.warning("material page failed: %s", exc)
            return
        yield event.image_result(image)

    @filter.regex(r"^[^\s/]{1,32}$")
    async def on_single_name(self, event: AstrMessageEvent):
        """Answer a bare character name with the rendered character card.

        The handler stays silent unless the wake word was used and exactly one
        character matched, so unrelated single-word messages never get a
        reply.

        Args:
            event: The message event that triggered the handler.

        Yields:
            The card image, or nothing when the lookup or the rendering fails.
        """
        if not event.is_at_or_wake_command:
            return
        text = event.get_message_str().strip()
        self.logger.debug("single-word lookup: %r", text)
        if _MATERIAL_SUFFIX.match(text) or _SKILL_SUFFIX.match(text):
            # ``迷迭香材料`` / ``迷迭香技能`` have their own handlers.
            return
        for provider in (self.arknights, self.endfield):
            try:
                entry = await provider.match(text)
            except (UpstreamError, LookupError, ValueError) as exc:
                self.logger.warning(
                    "lookup %r failed in %s: %s: %s",
                    text,
                    provider.game,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return
            if entry is None:
                continue
            self.logger.debug("matched %r -> %s/%s", text, entry.game, entry.entry_id)
            try:
                image = await self._card_image(provider, entry)
            except Exception as exc:  # noqa: BLE001 - never crash the event
                # Rendering can fail with transport errors, plugin exceptions or
                # anything a render backend raises; the handler stays silent
                # but the reason is always logged.
                self.logger.warning(
                    "card for %s/%s failed: %s: %s",
                    entry.game,
                    entry.entry_id,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return
            try:
                token_image = await self._token_image(provider, entry)
            except Exception as exc:  # noqa: BLE001 - device page is optional
                self.logger.warning(
                    "token page for %s/%s failed: %s: %s",
                    entry.game,
                    entry.entry_id,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                token_image = None
            key = getattr(event, "unified_msg_origin", "") or event.get_session_id()
            self._last_operator[key] = (entry.game, entry.entry_id, time.time())
            # Pictures first: a markdown reply resets the platform send buffer and
            # would make the following picture fail with "无效 markdown content".
            yield event.image_result(image)
            if token_image:
                yield event.image_result(token_image)
            if (event.get_platform_name() or "").startswith(_BUTTON_PLATFORMS):
                yield _card_message(entry)
            return
        # No operator carries that name; a material may still match, since the
        # wake word already keeps unrelated chatter away.
        try:
            panel = await self.arknights.material_panel(text)
        except (UpstreamError, LookupError, ValueError) as exc:
            self.logger.warning("material lookup %r failed: %s", text, exc)
            return
        if panel:
            item = GameDataEntry(
                game="arknights",
                category="material",
                entry_id=str(panel.get("entry_id") or text),
                title=str(panel.get("title") or text),
            )
            panel["operation"] = "material"
            try:
                image = await self._panel_image(self.arknights, item, panel)
            except Exception as exc:  # noqa: BLE001 - never crash the event
                self.logger.warning("material page %r failed: %s", text, exc)
                return
            yield event.image_result(image)
            return
        try:
            skill_panel = await self.arknights.skill_panel(text)
        except (UpstreamError, LookupError, ValueError) as exc:
            self.logger.warning("skill lookup %r failed: %s", text, exc)
            return
        if skill_panel:
            skill_panel["operation"] = "skill"
            item = GameDataEntry(
                game="arknights",
                category="skill",
                entry_id=str(skill_panel.get("entry_id") or text),
                title=str(skill_panel.get("title") or text),
            )
            try:
                image = await self._panel_image(self.arknights, item, skill_panel)
            except Exception as exc:  # noqa: BLE001 - never crash the event
                self.logger.warning("skill page %r failed: %s", text, exc)
                return
            yield event.image_result(image)
            return
        self.logger.debug("no character matched %r", text)

    def _remembered(self, key: str) -> tuple[str, str] | None:
        """Return the operator a session last looked up, if still fresh.

        Args:
            key: Session key the lookup was remembered under.

        Returns:
            ``(game, entry_id)`` while the card is younger than ``_CARD_TTL``;
            an expired entry is dropped and ``None`` is returned.
        """
        remembered = self._last_operator.get(key)
        if remembered is None:
            return None
        game, entry_id, stamped_at = remembered
        if time.time() - stamped_at > _CARD_TTL:
            self._last_operator.pop(key, None)
            self.logger.debug("card context for %s expired", key)
            return None
        return game, entry_id

    @filter.regex(_PANEL_WORD)
    async def on_panel_word(self, event: AstrMessageEvent):
        """Answer 模组 / 皮肤 / 档案 / 阵营 / 语音 for the last shown operator.

        QQ turns inline keyboard clicks into their label text instead of a
        callback, so the same pages are reachable by typing the word right after
        a character card was shown.

        Args:
            event: The message event that triggered the handler.

        Yields:
            The rendered sub-page, or nothing when no operator was shown yet.
        """
        key = getattr(event, "unified_msg_origin", "") or event.get_session_id()
        remembered = self._remembered(key)
        if remembered is None:
            return
        game, entry_id = remembered
        word = event.get_message_str().strip()
        if word.startswith("干员"):
            word = word[2:].strip()
        operation = _PANEL_WORDS.get(word)
        if not operation:
            return
        provider = self.arknights if game == "arknights" else self.endfield
        collector = f"{operation}_panel"
        try:
            entry = await provider.get_detail(entry_id)
            panel = await getattr(provider, collector)(entry)
            if not panel:
                yield event.plain_result(f"{entry.title} 没有{word}数据")
                return
            panel["operation"] = operation
            image = await self._panel_image(provider, entry, panel)
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning(
                "panel %s/%s/%s failed: %s: %s",
                game,
                operation,
                entry_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return
        yield event.image_result(image)

    async def _panel_image(
        self, provider: GameDataProvider, entry: GameDataEntry, panel: dict[str, Any]
    ) -> str:
        """Render one operator sub-page and cache the PNG.

        Args:
            provider: Provider that owns the character.
            entry: The character the page belongs to.
            panel: Panel data returned by the provider.

        Returns:
            Path of the rendered PNG.

        Raises:
            UpstreamError: If no render backend produced a usable PNG.
            OSError: If the rendered file cannot be read or written.
        """
        operation = str(panel.get("operation") or "panel")
        target = None
        if self._image_dir is not None:
            target = (
                self._image_dir
                / f"panel_v1_{entry.game}_{operation}_{entry.entry_id}.png"
            )
            if target.is_file():
                cached = target.read_bytes()
                if _valid_png(cached):
                    return str(target)
                self.logger.warning("panel %s: dropping invalid cache", target.name)
                target.unlink(missing_ok=True)
        renderer = await self._local_renderer()
        html = render_panel(panel, font=renderer.font_family if renderer else None)
        body = await self._render(html, entry, size=(1280, 720))
        if target is None:
            path = Path(tempfile.gettempdir()) / f"panel_{entry.entry_id}.png"
            path.write_bytes(body)
            return str(path)
        return str(_store_image(body, target))

    @filter.button_action()
    async def on_operator_button(self, event: AstrMessageEvent):
        """Answer one of the buttons attached under a character card.

        Args:
            event: The button click event.

        Yields:
            The rendered sub-page, or nothing when the operation is unknown.
        """
        interaction = event.get_button_interaction()
        data = interaction.data if interaction is not None else None
        if not isinstance(data, dict):
            return
        try:
            issued_at = float(data.get("ts") or 0)
        except (TypeError, ValueError):
            issued_at = 0.0
        if issued_at <= 0 or time.time() - issued_at > _CARD_TTL:
            self.logger.debug("ignoring stale card button: %s", data)
            return
        operation = str(data.get("op") or "")
        game = str(data.get("game") or "")
        entry_id = str(data.get("id") or "")
        collector = {
            "module": "module_panel",
            "skin": "skin_panel",
            "story": "story_panel",
            "faction": "faction_panel",
            "voice": "voice_panel",
        }.get(operation)
        if not collector or not entry_id:
            return
        operation_label = dict(_OPERATOR_ACTIONS).get(operation, operation)
        provider = self.arknights if game == "arknights" else self.endfield
        try:
            entry = await provider.get_detail(entry_id)
            panel = await getattr(provider, collector)(entry)
            if not panel:
                yield event.plain_result(f"{entry.title} 没有{operation}数据")
                return
            panel["operation"] = operation
            image = await self._panel_image(provider, entry, panel)
        except Exception as exc:  # noqa: BLE001 - never crash the event
            self.logger.warning(
                "panel %s/%s/%s failed: %s: %s",
                game,
                operation,
                entry_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return
        # QQ drops the media of an interaction reply; pushing a fresh picture
        # needs active-message rights the bot may not have, so the text summary
        # is the reliable answer and the picture stays one word away.
        try:
            sent = await self.context.send_message(
                event.unified_msg_origin, MessageChain(chain=[Image(file=image)])
            )
        except Exception as exc:  # noqa: BLE001 - fall back to text
            self.logger.info("active panel send failed: %s", exc)
            sent = False
        if not sent:
            yield event.plain_result(
                f"{entry.title} · {operation}\n发送「{operation_label}」查看完整图示"
            )

    async def terminate(self) -> None:
        """Release the browser and the HTTP client on disable or reload."""
        if self._renderer is not None:
            await self._renderer.close()
            self._renderer = None
        await self.net.aclose()
