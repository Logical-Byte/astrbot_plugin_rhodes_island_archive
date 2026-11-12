"""Command and MCP tool routing table for the Rhodes Island archive plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass

STRATEGY_TYPES = frozenset(
    {
        "剧目",
        "骰子",
        "密文板",
        "构想",
        "通宝",
        "零件",
        "集成战略剧目",
        "集成战略骰子",
        "集成战略密文板",
        "集成战略构想",
        "集成战略通宝",
        "集成战略零件",
    }
)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One user-facing command bound to one MCP detail tool.

    Attributes:
        operation: Internal operation name used by stateful callbacks.
        label: Text shown next to a rendered card title.
        tool: MCP tool name invoked for this operation.
        argument: Argument name that MCP expects for the resolved entity id.
        prefixes: Command prefixes, longest first-match wins.
        accepts: Search result types this operation may query.
        auto: Search result types that route here without an explicit command.
        suffixes: Trailing keywords such as ``材料`` in ``迷迭香材料``. Skill
            and mastery queries point at the same materials, so ``技能`` and
            ``专精`` share the operator-material operation.
    """

    operation: str
    label: str
    tool: str
    argument: str
    prefixes: tuple[str, ...] = ()
    accepts: frozenset[str] = frozenset()
    auto: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = ()


SPECS: tuple[CommandSpec, ...] = (
    # Front-door operations: they only resolve candidates and own no detail tool.
    CommandSpec("search", "搜索", "", "", prefixes=("搜索",)),
    CommandSpec("search_card", "搜索卡", "", "", prefixes=("搜索卡",)),
    CommandSpec("glossary", "术语", "", "", prefixes=("术语",)),
    CommandSpec(
        "recruit",
        "公招",
        "",
        "",
        prefixes=("公开招募", "公招", "高资", "高姿"),
    ),
    CommandSpec(
        "operator",
        "干员",
        "get_operator_basic_data",
        "operator_id",
        prefixes=("干员",),
        accepts=frozenset({"干员"}),
        auto=frozenset({"干员"}),
    ),
    CommandSpec(
        "operator_material",
        "干员材料",
        "get_operator_material",
        "operator_id",
        prefixes=("干员材料", "干员技能", "干员专精", "技能", "专精"),
        accepts=frozenset({"干员"}),
        suffixes=("材料", "技能", "专精"),
    ),
    CommandSpec(
        "modules",
        "模组",
        "get_operator_modules",
        "operator_id",
        prefixes=("干员模组", "模组"),
        accepts=frozenset({"干员"}),
        suffixes=("模组",),
    ),
    CommandSpec(
        "skins",
        "皮肤",
        "get_operator_skins",
        "operator_id",
        prefixes=("干员皮肤", "皮肤", "立绘"),
        accepts=frozenset({"干员", "皮肤"}),
        auto=frozenset({"皮肤"}),
        suffixes=("皮肤",),
    ),
    CommandSpec(
        "token",
        "召唤物",
        "get_token_detail",
        "token_id",
        prefixes=("召唤物",),
        accepts=frozenset({"干员", "召唤物"}),
        auto=frozenset({"召唤物"}),
        suffixes=("召唤物",),
    ),
    CommandSpec(
        "material",
        "材料",
        "get_material",
        "material_id",
        prefixes=("材料",),
        accepts=frozenset({"材料"}),
        auto=frozenset({"材料"}),
    ),
    CommandSpec(
        "stage",
        "关卡",
        "get_stage_data",
        "stage_id",
        prefixes=("关卡",),
        accepts=frozenset({"关卡"}),
        auto=frozenset({"关卡"}),
    ),
    CommandSpec(
        "enemy",
        "敌人",
        "get_enemy_data",
        "enemy_id",
        prefixes=("敌人",),
        accepts=frozenset({"敌人"}),
        auto=frozenset({"敌人"}),
    ),
    CommandSpec(
        "bond",
        "盟约",
        "get_autochess_bond_detail",
        "bond_id",
        prefixes=("卫戍协议盟约", "自走棋盟约", "盟约"),
        accepts=frozenset({"卫戍协议盟约"}),
        auto=frozenset({"卫戍协议盟约"}),
    ),
    CommandSpec(
        "autochess_strategy",
        "策略",
        "get_autochess_strategy_detail",
        "strategy_id",
        prefixes=("卫戍协议策略", "自走棋策略", "策略"),
        accepts=frozenset({"卫戍协议策略"}),
        auto=frozenset({"卫戍协议策略"}),
    ),
    CommandSpec(
        "strategy_item",
        "肉鸽",
        "get_integrated_strategy_item_detail",
        "item_id",
        prefixes=("集成战略", "肉鸽", "藏品"),
        accepts=STRATEGY_TYPES | {"集成战略藏品"},
        auto=STRATEGY_TYPES | {"集成战略藏品"},
    ),
)


class CommandTable:
    """Lookup helper over the declarative command specifications."""

    def __init__(self, specs: tuple[CommandSpec, ...] = SPECS) -> None:
        """Build lookup indexes from command specifications.

        Args:
            specs: Command specifications, in declaration order.
        """
        self._specs = {spec.operation: spec for spec in specs}
        self._prefixes = sorted(
            ((prefix, spec.operation) for spec in specs for prefix in spec.prefixes),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._by_type: dict[str, str] = {}
        for spec in specs:
            for kind in spec.auto:
                self._by_type.setdefault(kind, spec.operation)
        self._known_types: frozenset[str] = frozenset(
            kind for spec in specs for kind in spec.accepts
        )
        self._suffixes = {
            suffix: spec.operation for spec in specs for suffix in spec.suffixes
        }

    def resolve(self, text: str) -> tuple[str, str] | None:
        """Split a message into an operation and its remaining query.

        Args:
            text: Message text already stripped of the wake prefix.

        Returns:
            ``(operation, query)`` for the longest matching prefix, else None.
        """
        for prefix, operation in self._prefixes:
            if text.startswith(prefix):
                return operation, text[len(prefix) :].strip()
        return None

    @property
    def suffixes(self) -> tuple[str, ...]:
        """Return every configured trailing keyword.

        Returns:
            Suffix keywords, longest first.
        """
        return tuple(sorted(self._suffixes, key=len, reverse=True))

    def has(self, operation: str) -> bool:
        """Report whether an operation maps to an MCP detail tool.

        Args:
            operation: Operation name.

        Returns:
            True when the operation owns a tool.
        """
        spec = self._specs.get(operation)
        return bool(spec and spec.tool)

    def tool(self, operation: str) -> tuple[str, str]:
        """Return the MCP tool and its entity-id argument name.

        Args:
            operation: Operation name.

        Returns:
            ``(tool, argument)`` pair.
        """
        spec = self._specs[operation]
        return spec.tool, spec.argument

    def suffix_operation(self, suffix: str) -> str:
        """Resolve a trailing keyword such as ``材料`` in ``迷迭香材料``.

        Args:
            suffix: Trailing keyword captured from the message.

        Returns:
            Operation name bound to that suffix.
        """
        return self._suffixes[suffix]

    def label(self, operation: str) -> str:
        """Return the user-facing label for an operation.

        Args:
            operation: Operation name.

        Returns:
            Short Chinese label.
        """
        return self._specs[operation].label

    def accepts(self, operation: str, kind: str) -> bool:
        """Report whether a search result type is valid for an operation.

        Args:
            operation: Requested operation.
            kind: Search result ``type`` value.

        Returns:
            True when the type belongs to a routable operation.
        """
        spec = self._specs.get(operation)
        if spec is not None and spec.accepts:
            return kind in spec.accepts
        return kind in self._known_types

    def operation_for(self, kind: str) -> str:
        """Resolve the operation a search result type routes to by default.

        Args:
            kind: Search result ``type`` value.

        Returns:
            Operation name, or an empty string when the type is unsupported.
        """
        return self._by_type.get(kind, "")


COMMANDS = CommandTable()
SUFFIX_PATTERN = re.compile(
    r"^([^\s/]{1,32})("
    + "|".join(sorted((s for s in COMMANDS.suffixes), key=len, reverse=True))
    + r")$"
)
