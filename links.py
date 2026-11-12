"""Keyword shortcuts that answer with a link or a freshly fetched image."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LinkRule:
    """One keyword answer, either a plain link or an image to render."""

    label: str
    pattern: str
    url: str
    image: bool = False


DEFAULT_RULES: tuple[dict[str, Any], ...] = (
    {"label": "一图流", "pattern": "一图流", "url": "https://ark.yituliu.cn/"},
    {
        "label": "商店性价比",
        "pattern": "(?:一图流)?商店性价比",
        "url": "https://ark.yituliu.cn/material/store",
    },
    {
        "label": "礼包性价比",
        "pattern": "(?:一图流)?礼包性价比",
        "url": "https://ark.yituliu.cn/material/pack",
    },
    {
        "label": "物品价值",
        "pattern": "(?:一图流)?(?:物品)?价值",
        "url": "https://ark.yituliu.cn/material/value",
    },
    {
        "label": "攒抽计算器",
        "pattern": "(?:一图流)?攒抽(?:计算器)?",
        "url": "https://ark.yituliu.cn/tools/gachaCalc",
    },
    {"label": "特米米", "pattern": "特米米", "url": "https://tomimi.cyou/zh"},
    {"label": "企鹅物流", "pattern": "企鹅物流", "url": "https://penguin-stats.io/"},
    {
        "label": "终末地一图流",
        "pattern": "终末地一图流",
        "url": "https://ef.yituliu.cn/",
    },
    {
        "label": "蓝图站",
        "pattern": "(?:终末地)?蓝图站",
        "url": "https://talospioneers.com/zh-CN",
    },
    {
        "label": "终末地 wiki",
        "pattern": "终末地wiki",
        "url": "https://warfarin.wiki/cn",
    },
    {
        "label": "热能池规划",
        "pattern": "热能池(?:规划)?",
        "url": "https://dige.aunly.cn/?lang=zh",
    },
    {
        "label": "材料",
        "pattern": "材料|活动|复刻",
        "url": "http://bd4wxr.top:5040/api/files?name=材料",
        "image": True,
    },
    {
        "label": "进店",
        "pattern": "进店",
        "url": "http://bd4wxr.top:5040/api/files?name=进店",
        "image": True,
    },
)

DEFAULT_CONFIG = json.dumps(list(DEFAULT_RULES), ensure_ascii=False, indent=2)


def _as_bool(value: Any) -> bool:
    """Read an image flag that may arrive as a boolean or a string.

    Args:
        value: Raw value from the configuration table.

    Returns:
        Whether the rule should answer with an image.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "image"}


def parse_rules(raw: Any) -> tuple[LinkRule, ...]:
    """Parse the configurable rule table, falling back to the built-in rules.

    Args:
        raw: JSON string or list of rule dictionaries from the plugin config.

    Returns:
        Valid rules in declaration order; the built-in table when nothing parses.
    """
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return tuple(LinkRule(**item) for item in DEFAULT_RULES)
        try:
            data = json.loads(text)
        except ValueError:
            return tuple(LinkRule(**item) for item in DEFAULT_RULES)
    if not isinstance(data, list):
        return tuple(LinkRule(**item) for item in DEFAULT_RULES)
    rules: list[LinkRule] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        url = str(item.get("url") or "").strip()
        if not pattern or not url:
            continue
        try:
            re.compile(pattern)
        except re.error:
            continue
        rules.append(
            LinkRule(
                label=str(item.get("label") or pattern),
                pattern=pattern,
                url=url,
                image=_as_bool(item.get("image")),
            )
        )
    return tuple(rules) or tuple(LinkRule(**item) for item in DEFAULT_RULES)


class LinkTable:
    """Ordered matcher for keyword shortcuts."""

    def __init__(self, rules: tuple[LinkRule, ...]) -> None:
        """Compile every pattern once.

        Args:
            rules: Rules in priority order.
        """
        self._rules = tuple((rule, re.compile(rule.pattern)) for rule in rules)

    def match(self, text: str) -> LinkRule | None:
        """Return the first rule whose pattern covers the whole message.

        Args:
            text: Message text without the wake prefix.

        Returns:
            The matching rule, or ``None`` when the text is a different request.
        """
        candidate = text.strip()
        if not candidate:
            return None
        for rule, pattern in self._rules:
            if pattern.fullmatch(candidate):
                return rule
        return None


_ACTIVE = LinkTable(parse_rules(None))


def set_rules(raw: Any) -> None:
    """Replace the rules used by the query filter and the handler.

    Args:
        raw: JSON string or list of rule dictionaries from the plugin config.
    """
    global _ACTIVE
    _ACTIVE = LinkTable(parse_rules(raw))


def match(text: str) -> LinkRule | None:
    """Match text against the active rule table.

    Args:
        text: Message text without the wake prefix.

    Returns:
        The matching rule, if any.
    """
    return _ACTIVE.match(text)
