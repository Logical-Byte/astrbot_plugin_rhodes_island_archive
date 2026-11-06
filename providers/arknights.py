"""Arknights (明日方舟) provider backed by the PRTS asset mirror."""

from __future__ import annotations

import base64
import io
import re
from html import escape
from itertools import combinations as _combinations
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..models import GameDataEntry, ResourceRef, SafeHtml
from .base import GameDataProvider, format_number
from .card import encode_art
from .net import UpstreamError

# Character asset directories exposed by the mirror, keyed by kind.
_CHARACTER_DIRS = {
    "portrait": "raw/char_portrait",
    "avatar": "raw/char_avatar",
    "arts": "raw/char_arts",
}

#: Class names of the character table, keyed by their upstream enum.
_PROFESSIONS = {
    "PIONEER": "先锋",
    "WARRIOR": "近卫",
    "TANK": "重装",
    "SNIPER": "狙击",
    "CASTER": "术师",
    "MEDIC": "医疗",
    "SUPPORT": "辅助",
    "SPECIAL": "特种",
}

#: Module bonus keys, translated for the module page.
_MODULE_ATTRS = {
    "max_hp": "生命",
    "atk": "攻击",
    "def": "防御",
    "magic_resistance": "法抗",
    "attack_speed": "攻速",
    "attack_interval": "攻击间隔",
    "block_cnt": "阻挡",
    "cost": "费用",
    "base_attack_time": "攻击间隔",
    "respawn_time": "再部署",
}

#: Archive blocks that are shown on the story page, in reading order.
_ARCHIVE_BLOCKS = (
    "基础档案",
    "客观履历",
    "临床诊断分析",
    "档案资料一",
    "档案资料二",
    "档案资料三",
    "档案资料四",
)

#: Stats printed for one operator, in display order.
_STATS = (
    ("maxHp", "生命"),
    ("atk", "攻击"),
    ("def", "防御"),
    ("magicResistance", "法抗"),
)

#: Attribute rows of the operator card.
_ATTRIBUTES = (
    ("maxHp", "最大生命值"),
    ("atk", "攻击力"),
    ("def", "防御力"),
    ("magicResistance", "魔法抗性"),
    ("attackSpeed", "攻击速度"),
    ("baseAttackTime", "攻击间隔"),
    ("blockCnt", "阻挡数"),
    ("cost", "部署费用"),
    ("respawnTime", "再部署时间"),
)

#: Deployment positions of the character table.
_POSITIONS = {"MELEE": "近战位", "RANGED": "远程位"}

#: Voice languages of ``charword_table.voiceLangDict``.
_VOICE_LANGS = (
    ("CN_MANDARIN", "中文-普通话"),
    ("JP", "日文"),
    ("KR", "韩文"),
    ("EN", "英文"),
)

#: One ``【代号】迷迭香`` pair inside the basic archive block.
_ARCHIVE_FACT = re.compile(r"^【([^】]{1,12})】\s*(.*)$")

#: SP recovery mode of a skill level.
_SP_TYPES = {
    "INCREASE_WITH_TIME": "自动回复",
    "INCREASE_WHEN_ATTACK": "攻击回复",
    "INCREASE_WHEN_TAKEN_DAMAGE": "受击回复",
}

#: Trigger mode of a skill level.
_SKILL_TYPES = {
    "MANUAL": "手动触发",
    "AUTO": "自动触发",
    "PASSIVE": "被动",
}

#: Rich-text tags such as ``<@ba.vup>`` and their closing ``</>``.
_TAG = re.compile(r"<[^<>]*>")

#: Placeholders such as ``{atk:0%}`` filled from the skill blackboard.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _render(text: str, blackboard: dict[str, Any]) -> SafeHtml:
    """Turn an upstream rich-text string into HTML for the card.

    Placeholders are filled from the blackboard and the game's emphasis tags
    become ``<span class="mark">``, which is how the template highlights values.
    Everything outside those spans is escaped.

    Args:
        text: Description carrying ``<@ba.kw>`` tags and ``{key:spec}`` slots.
        blackboard: Values keyed by placeholder name.

    Returns:
        HTML with the highlighted parts wrapped; unknown placeholders are left
        untouched instead of being guessed.
    """

    def substitute(match: re.Match[str]) -> str:
        name, _, spec = match.group(1).partition(":")
        value = blackboard.get(name)
        if value is None:
            return match.group(0)
        if isinstance(value, int | float) and spec:
            if spec.endswith("%"):
                # Upstream stores shares such as 0.55; keep four digits so
                # 0.055 renders as 5.5% and 1.8 as 180% without float noise.
                return f"{value * 100:.4g}%"
            return format(value, spec)
        return format_number(value)

    plain = str(text).replace("\\n", " ").replace("\n", " ")
    plain = _PLACEHOLDER.sub(substitute, plain)

    pieces: list[str] = []
    position = 0
    marked = False
    for match in _TAG.finditer(plain):
        chunk = escape(plain[position : match.start()])
        pieces.append(f'<span class="mark">{chunk}</span>' if marked else chunk)
        tag = match.group(0)
        marked = not tag.startswith("</") and not tag.endswith("/>")
        position = match.end()
    tail = escape(plain[position:])
    pieces.append(f'<span class="mark">{tail}</span>' if marked else tail)
    return SafeHtml("".join(pieces))


def _asset_url(directory: str, name: str) -> str:
    """Build the public URL of one static asset.

    Args:
        directory: Directory under the mirror's ``/assets/`` tree.
        name: File name inside that directory.

    Returns:
        The absolute download URL.
    """
    return f"{ArknightsProvider.root}/assets/{directory}/{quote(name, safe='')}"


def _range_cells(range_record: dict[str, Any] | None) -> list[list[str]]:
    """Turn a range table entry into a grid of cell kinds.

    Args:
        range_record: ``range_table`` entry holding ``grids`` cells.

    Returns:
        Rows of ``"r"`` (attackable), ``"s"`` (the operator's own tile) and
        ``""`` (outside the range); empty when the entry publishes no cells.
    """
    cells = {
        (int(cell["row"]), int(cell["col"]))
        for cell in (range_record or {}).get("grids") or []
        if "row" in cell and "col" in cell
    }
    if not cells:
        return []
    rows = [row for row, _ in cells]
    cols = [col for _, col in cells]
    return [
        [
            "" if (row, col) not in cells else ("s" if (row, col) == (0, 0) else "r")
            for col in range(min(cols), max(cols) + 1)
        ]
        for row in range(min(rows), max(rows) + 1)
    ]


def _darken_white(payload: bytes) -> bytes:
    """Repaint a white glyph so it stays visible on a light background.

    The facility icons of the game are drawn white with alpha, which vanishes
    on the card's light panel. Only icons whose opaque pixels are essentially
    white are recoloured; coloured icons are returned untouched.

    Args:
        payload: Original PNG bytes.

    Returns:
        The recoloured PNG bytes, or the original payload when it cannot be
        processed or is not a white glyph.
    """
    try:
        from PIL import Image  # Shipped with AstrBot.

        with Image.open(io.BytesIO(payload)) as source:
            image = source.convert("RGBA")
    except (ImportError, OSError, ValueError):
        return payload

    counts = image.getcolors(maxcolors=1 << 16)
    if counts is None:
        return payload
    opaque = [(count, colour) for count, colour in counts if colour[3] > 32]
    total = sum(count for count, _ in opaque)
    if not total:
        return payload
    white = sum(count for count, colour in opaque if min(colour[:3]) > 200)
    if white < total * 0.8:
        return payload

    black = Image.new("L", image.size, 45)
    flattened = Image.merge("RGBA", (black, black, black, image.getchannel("A")))
    buffer = io.BytesIO()
    flattened.save(buffer, format="PNG")
    return buffer.getvalue()


#: Attribute rows of the device page, in template order.
_DEVICE_ATTRIBUTES = (
    ("maxHp", "最大生命值"),
    ("atk", "攻击力"),
    ("def", "防御力"),
    ("magicResistance", "魔法抗性"),
    ("baseAttackTime", "攻击间隔"),
    ("blockCnt", "阻挡数"),
    ("cost", "部署费用"),
    ("respawnTime", "再部署时间"),
)

#: Deployment-position pill colours of the device page.
_POSITION_COLORS = {
    "近战位": "#f44336",
    "远程位": "#4caf50",
    "不限部署位": "#9c27b0",
}


def _device_card(
    token_id: str,
    token: dict[str, Any],
    skill_table: dict[str, Any],
    range_table: dict[str, Any],
) -> dict[str, Any]:
    """Collect the fields of one deployable device.

    Args:
        token_id: ``character_table`` key of the device.
        token: Raw device record.
        skill_table: Skill table used to resolve the device skill.
        range_table: Range table used to resolve the attack range.

    Returns:
        The device block consumed by :func:`providers.card.render_token_card`.
    """
    position = _POSITIONS.get(str(token.get("position") or ""), "")
    phases = token.get("phases") or []
    frames = (phases[-1].get("attributesKeyFrames") or []) if phases else []
    data = (frames[-1].get("data") or {}) if frames else {}

    attributes = []
    for key, label in _DEVICE_ATTRIBUTES:
        value = data.get(key)
        if value is not None:
            attributes.append((label, format_number(value)))

    skills = []
    seen_skills: set[str] = set()
    for item in token.get("skills") or []:
        skill_id = str((item or {}).get("skillId") or "")
        levels = ((skill_table.get(skill_id) or {}).get("levels")) or []
        if not skill_id or skill_id in seen_skills or not levels:
            continue
        level = levels[-1]
        board = {
            str(entry.get("key")): entry.get("value")
            for entry in level.get("blackboard") or []
            if "key" in entry
        }
        sp = level.get("spData") or {}
        text = _render(level.get("description") or "", board)
        if not text:
            # The reference template skips levels without a description.
            continue
        seen_skills.add(skill_id)
        skills.append(
            {
                "name": str(level.get("name") or skill_id),
                "sp_type": str(sp.get("spType") or ""),
                "sp_type_name": _SP_TYPES.get(str(sp.get("spType") or ""), ""),
                "skill_type_name": _SKILL_TYPES.get(
                    str(level.get("skillType") or ""), ""
                ),
                "sp_init": format_number(sp.get("initSp") or 0),
                "sp_cost": format_number(sp["spCost"]) if sp.get("spCost") else "",
                "duration": format_number(max(float(level.get("duration") or 0), 0.0)),
                "text": text,
            }
        )

    talents = []
    for talent in token.get("talents") or []:
        candidates = talent.get("candidates") or []
        if not candidates:
            continue
        best = candidates[-1]
        name = str(best.get("name") or "").strip()
        text = _render(str(best.get("description") or ""), {})
        if name and text and text != "-":
            talents.append({"name": name, "text": text})

    range_id = str((phases[-1].get("rangeId") if phases else "") or "")

    return {
        "id": token_id,
        "name": str(token.get("name") or token_id),
        "en_name": str(token.get("appellation") or ""),
        "type": position,
        "type_color": _POSITION_COLORS.get(position, "#9c27b0"),
        "description": _render(str(token.get("description") or ""), {}),
        "attributes": attributes,
        "range": _range_cells(range_table.get(range_id)),
        "talents": talents,
        "skills": skills,
    }


def _archive_facts(handbooks: dict[str, Any], char_id: str) -> dict[str, str]:
    """Extract the ``【key】value`` pairs of an operator's basic archive.

    The archive block is published as one plain-text story whose lines look
    like ``【生日】7月6日``; multi-line values such as the originium report are
    ignored because the card only shows single-line facts.

    Args:
        handbooks: ``handbook_info_table.handbookDict`` of the current version.
        char_id: Operator identifier.

    Returns:
        Facts keyed by their Chinese label, first occurrence wins.
    """
    facts: dict[str, str] = {}
    record = handbooks.get(char_id) or {}
    for block in record.get("storyTextAudio") or []:
        if str(block.get("storyTitle") or "") != "基础档案":
            continue
        for story in block.get("stories") or []:
            for line in str(story.get("storyText") or "").splitlines():
                match = _ARCHIVE_FACT.match(line.strip())
                if match and match.group(2).strip():
                    facts.setdefault(match.group(1), match.group(2).strip())
    return facts


def _building_cards(char_id: str, building: dict[str, Any]) -> list[dict[str, str]]:
    """Collect the base skills of one operator.

    Args:
        char_id: Operator identifier.
        building: ``building_data`` table.

    Returns:
        One entry per base skill with its elite phase, room icon, name and
        description; empty when the operator works in no room.
    """
    chars = building.get("chars") or {}
    buffs = building.get("buffs") or {}
    record = chars.get(char_id) or {}
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in record.get("buffChar") or []:
        for item in group.get("buffData") or []:
            buff_id = str(item.get("buffId") or "")
            if not buff_id or buff_id in seen:
                continue
            buff = buffs.get(buff_id) or {}
            if not buff:
                continue
            seen.add(buff_id)
            phase = str((item.get("cond") or {}).get("phase") or "")
            room = str(buff.get("roomType") or "").lower()
            # Each base skill has its own badge; the room glyph is only a
            # fallback for buffs that publish none.
            skill_icon = str(buff.get("skillIcon") or "")
            if skill_icon:
                icon = _asset_url("build_skill_icon", f"{skill_icon}.png")
            elif room:
                icon = _asset_url("building_buff_icon", f"{room}.png")
            else:
                icon = ""
            cards.append(
                {
                    "elite": phase.removeprefix("PHASE_") if phase else "?",
                    "icon": icon,
                    "monochrome": not skill_icon,
                    "name": str(buff.get("buffName") or ""),
                    "text": _render(str(buff.get("description") or ""), {}),
                }
            )
    return cards


#: Recruit tags that are not part of ``tagList``.
_RECRUIT_RARITY_TAGS = ("新手", "资深干员", "高级资深干员")

#: Aliases the recruit screen and players use for the senior tag.
_RECRUIT_ALIASES = {
    "高资": "高级资深干员",
    "高级资深": "高级资深干员",
    "资深": "资深干员",
}

#: Maximum number of tags combined into one lock.
_RECRUIT_MAX_COMBINATION = 3


def match_recruit_tags(text: str, vocabulary: list[str]) -> list[str]:
    """Pick the recruitment tags out of OCR text.

    The screenshot text arrives as one run of characters without separators,
    so the vocabulary is scanned longest-first and the first hit wins.

    Args:
        text: Text returned by the OCR engine.
        vocabulary: Known tags, sorted longest first.

    Returns:
        Tags in the order they appear, duplicates removed.
    """
    squashed = re.sub(r"\s+", "", text or "")
    found: list[str] = []
    index = 0
    while index < len(squashed):
        for tag in vocabulary:
            if squashed.startswith(tag, index):
                if tag not in found:
                    found.append(tag)
                index += len(tag)
                break
        else:
            index += 1
    return found


#: Inline markup wrapping the operators that cannot be obtained elsewhere.
_RECRUIT_MARKUP = re.compile(r"<[^<>]*>")


def _recruit_tags(record: dict[str, Any]) -> set[str]:
    """Collect the recruit tags of one operator.

    Args:
        record: Raw ``character_table`` record.

    Returns:
        Job tags, the profession name and the deployment position.
    """
    tags = {str(tag) for tag in record.get("tagList") or [] if tag}
    profession = _PROFESSIONS.get(str(record.get("profession") or ""))
    if profession:
        tags.add(profession)
    position = _POSITIONS.get(str(record.get("position") or ""))
    if position:
        tags.add(position)
    return tags


class ArknightsProvider(GameDataProvider):
    """Read Arknights characters and resources from ``torappu.prts.wiki``.

    The mirror serves two trees: ``gamedata`` holds the extracted JSON tables
    and ``raw`` holds assets whose files are published under ``/assets/``.
    """

    game = "arknights"
    root = "https://torappu.prts.wiki"

    def __init__(self, net: Any) -> None:
        """Initialize the provider.

        Args:
            net: Client exposing asynchronous ``get_bytes`` and ``get_json``.
        """
        self.net = net
        self._latest_res: str | None = None
        # Parsed tables are kept for the provider lifetime: the card needs
        # nine of them and several weigh more than eight megabytes.
        self._tables: dict[str, dict[str, Any]] = {}
        # Small icons are inlined into the card so the render service does
        # not have to fetch them while the page is being captured.
        self._icons: dict[str, str] = {}
        self._drawers_cache: dict[str, str] | None = None
        self._voices_cache: dict[str, dict[str, list[str]]] | None = None

    @staticmethod
    def _url(path: str) -> str:
        """Map a provider path to its public download URL.

        Args:
            path: Path relative to the mirror root, under ``raw/`` or ``gamedata/``.

        Returns:
            The absolute download URL.

        Raises:
            ValueError: If the path is outside the two published trees.
        """
        if path.startswith("raw/"):
            return f"{ArknightsProvider.root}/assets/{quote(path[4:], safe='/')}"
        if path.startswith("gamedata/"):
            return f"{ArknightsProvider.root}/{quote(path, safe='/')}"
        raise ValueError(f"Unsupported Arknights path: {path}")

    async def _latest_resource_dir(self) -> str:
        """Resolve the newest published gamedata directory.

        Returns:
            The path of the newest version directory.

        Raises:
            LookupError: If the mirror publishes no version directory.
        """
        if self._latest_res is not None:
            return self._latest_res
        versions = [
            ref.path
            for ref in await self.list_dir("gamedata")
            if ref.is_dir and ref.path.rsplit("/", 1)[-1] != "latest"
        ]
        if not versions:
            raise LookupError("No Arknights gamedata version is published")
        self._latest_res = max(versions)
        return self._latest_res

    async def _table(self, name: str) -> dict[str, Any]:
        """Load one extracted table of the newest version.

        Args:
            name: Table file name without the ``.json`` suffix.

        Returns:
            The decoded table, empty when upstream publishes no object.

        Raises:
            ValueError: If the upstream table is not a JSON object.
        """
        cached = self._tables.get(name)
        if cached is not None:
            return cached
        directory = await self._latest_resource_dir()
        table = await self.net.get_json(self._url(f"{directory}/excel/{name}.json"))
        if not isinstance(table, dict):
            raise ValueError(f"Unexpected Arknights table: {name}")
        self._tables[name] = table
        return table

    async def _drawers(self) -> dict[str, str]:
        """Resolve the artist of every operator.

        ``skin_table`` weighs eleven megabytes and only its drawer list is
        needed, so the map is built once and the table is released.

        Returns:
            Artist keyed by operator identifier; operators without a listed
            artist are absent.
        """
        if self._drawers_cache is not None:
            return self._drawers_cache
        skins = (await self._table("skin_table")).get("charSkins") or {}
        self._tables.pop("skin_table", None)
        drawers: dict[str, str] = {}
        for skin_id, skin in skins.items():
            names = ((skin.get("displaySkin") or {}).get("drawerList")) or []
            if not names:
                continue
            char_id = str(skin_id).split("#", 1)[0].split("@", 1)[0]
            drawers.setdefault(char_id, str(names[0]))
        self._drawers_cache = drawers
        return drawers

    async def _voice_cvs(self) -> dict[str, dict[str, list[str]]]:
        """Resolve the voice actors of every operator.

        ``charword_table`` weighs fifty megabytes and only four cast lists per
        operator are needed, so the map is built once and the table released.

        Returns:
            Voice actors keyed by operator identifier, then by language code.
        """
        if self._voices_cache is not None:
            return self._voices_cache
        languages = (await self._table("charword_table")).get("voiceLangDict") or {}
        self._tables.pop("charword_table", None)
        voices: dict[str, dict[str, list[str]]] = {}
        for char_id, record in languages.items():
            cast = {}
            for code, name in _VOICE_LANGS:
                names = ((record.get("dict") or {}).get(code) or {}).get("cvName") or []
                if names:
                    cast[code] = [str(item) for item in names]
            if cast:
                voices[str(char_id)] = cast
        self._voices_cache = voices
        return voices

    async def _art_data(self, url: str) -> str:
        """Inline the character illustration as a data URI.

        Args:
            url: Absolute illustration URL, or an empty string.

        Returns:
            A ``data:`` URI, or an empty string when the illustration is absent.

        Raises:
            UpstreamError: If the illustration was published but cannot be
                downloaded; a card without artwork is worse than no card.
        """
        if not url:
            return ""
        cached = self._icons.get(url)
        if cached is not None:
            return cached
        payload = await self.net.get_bytes(url)
        # The card paints the illustration about 690 px wide, so it travels
        # as a downscaled JPEG instead of the multi-megabyte original.
        cached = encode_art(payload)
        if not cached:
            return url
        self._icons[url] = cached
        return cached

    async def _icon_data(self, url: str, *, darken_white: bool = False) -> str:
        """Inline one icon as a data URI for the card template.

        Args:
            url: Absolute icon URL, or an empty string.
            darken_white: Recolour an all-white glyph to near black. Base
                facility icons are published white for the game's dark UI and
                would vanish on the card's light panel.

        Returns:
            A ``data:`` URI, or an empty string when the icon is missing or
            cannot be downloaded.
        """
        if not url:
            return ""
        cached = self._icons.get(url)
        if cached is not None:
            return cached
        try:
            payload = await self.net.get_bytes(url)
        except (UpstreamError, OSError, ValueError):
            return ""
        if darken_white:
            payload = _darken_white(payload)
        encoded = base64.b64encode(payload).decode("ascii")
        cached = f"data:image/png;base64,{encoded}"
        self._icons[url] = cached
        return cached

    async def _character_table(self) -> dict[str, Any]:
        """Load the character table of the newest version.

        Returns:
            Mapping of character identifier to its raw table record.

        Raises:
            ValueError: If the upstream table is not a JSON object.
        """
        return await self._table("character_table")

    @staticmethod
    def _entry(entry_id: str, record: dict[str, Any], source_url: str) -> GameDataEntry:
        """Normalize one character table record.

        Args:
            entry_id: Character table key.
            record: Raw upstream record.
            source_url: URL the record was read from.

        Returns:
            The normalized entry with raw fields preserved in ``attributes``.
        """
        title = str(record.get("name") or record.get("appellation") or entry_id)
        return GameDataEntry(
            game="arknights",
            category="operator",
            entry_id=entry_id,
            title=title,
            summary=str(record.get("description") or ""),
            source_url=source_url,
            attributes=dict(record),
        )

    async def search(self, keyword: str, limit: int = 5) -> list[GameDataEntry]:
        """Search playable operators by identifier or name.

        Only ``char_*`` records are considered; tokens and traps that share
        the table are never returned.

        Args:
            keyword: Case-insensitive text matched against identifier and name.
            limit: Maximum number of entries.

        Returns:
            Matching operators in table order.
        """
        query = keyword.strip().casefold()
        if not query or limit <= 0:
            return []

        directory = await self._latest_resource_dir()
        source_url = self._url(f"{directory}/excel/character_table.json")
        results: list[GameDataEntry] = []
        for entry_id, record in (await self._character_table()).items():
            if not entry_id.startswith("char_") or not isinstance(record, dict):
                continue
            name = str(record.get("name") or "")
            if query in entry_id.casefold() or query in name.casefold():
                results.append(self._entry(entry_id, record, source_url))
                if len(results) >= limit:
                    break
        return results

    async def get_detail(self, entry_id: str) -> GameDataEntry:
        """Resolve one operator by its exact table key.

        Args:
            entry_id: Character table key such as ``char_391_rosmon``.

        Returns:
            The operator with raw table fields in ``attributes``.

        Raises:
            LookupError: If the key is absent from the character table.
        """
        directory = await self._latest_resource_dir()
        table = await self._character_table()
        record = table.get(entry_id)
        if not isinstance(record, dict):
            raise LookupError(f"Unknown Arknights operator: {entry_id}")
        return self._entry(
            entry_id, record, self._url(f"{directory}/excel/character_table.json")
        )

    async def match(self, text: str) -> GameDataEntry | None:
        """Resolve a single operator name, preferring exact matches.

        A name, an English appellation and a table identifier all count as a
        match. Substring matches are only accepted when exactly one operator
        matches, so a bare word never answers with an arbitrary operator.

        Args:
            text: A single token typed after the wake word.

        Returns:
            The matched operator, or ``None`` when nothing matched or the
            substring search was ambiguous.
        """
        query = text.strip().casefold()
        if not query:
            return None
        directory = await self._latest_resource_dir()
        source_url = self._url(f"{directory}/excel/character_table.json")
        partial: list[tuple[str, dict[str, Any]]] = []
        for entry_id, record in (await self._character_table()).items():
            if not entry_id.startswith("char_") or not isinstance(record, dict):
                continue
            names = {
                entry_id.casefold(),
                str(record.get("name") or "").casefold(),
                str(record.get("appellation") or "").casefold(),
            }
            names.discard("")
            if query in names:
                return self._entry(entry_id, record, source_url)
            if any(query in name for name in names):
                partial.append((entry_id, record))
        if len(partial) != 1:
            return None
        entry_id, record = partial[0]
        return self._entry(entry_id, record, source_url)

    async def recruit_vocabulary(self) -> list[str]:
        """Collect every tag the recruitment screen can show.

        Returns:
            Tags sorted longest first, so the OCR matcher prefers
            ``高级资深干员`` over ``资深干员``.
        """
        table = await self._character_table()
        vocabulary = set(_RECRUIT_RARITY_TAGS)
        for entry_id, record in table.items():
            if not entry_id.startswith("char_"):
                continue
            vocabulary |= _recruit_tags(record)
        vocabulary -= {""}
        return sorted(vocabulary, key=len, reverse=True)

    async def _recruit_pool(self) -> set[tuple[str, int]]:
        """Read the operators the recruitment pool currently offers.

        The pool is published only as text inside ``gacha_table.recruitDetail``:
        one block per rarity, introduced by a line of stars and holding the
        names separated by ``/``.

        Returns:
            ``(name, rarity)`` pairs; empty when the text cannot be parsed, in
            which case the caller falls back to every obtainable operator.
        """
        table = await self._table("gacha_table")
        # The upstream text mixes real CRLF with literal ``\\n`` escapes.
        detail = (
            str(table.get("recruitDetail") or "")
            .replace("\\n", "\n")
            .replace("\r\n", "\n")
        )
        pool: set[tuple[str, int]] = set()
        rarity = 0
        for line in detail.splitlines():
            text = line.strip()
            if not text:
                continue
            if set(text) == {"★"}:
                rarity = len(text)
                continue
            if not rarity:
                continue
            for name in _RECRUIT_MARKUP.sub("", text).split("/"):
                name = name.strip()
                if name:
                    pool.add((name, rarity))
            rarity = 0
        return pool

    async def recruit(self, tags: list[str]) -> dict[str, Any]:
        """Plan a recruitment by tag combinations.

        Mirrors the reference plugin: every combination of at most three tags
        is checked, a six-star operator is only offered together with
        ``高级资深干员``, and only operators the tags can actually lock are
        listed.

        Args:
            tags: Tags typed by the user, already normalised by aliases.

        Returns:
            Mapping with ``tags``, ``groups`` and the resolved ``max_rarity``;
            ``groups`` is empty when no combination locks a rare operator.
        """
        table = await self._character_table()
        # Keep the order the user typed: the reference plugin appends tags as
        # it parses them and the pill order follows that list.
        wanted: list[str] = []
        for tag in tags:
            if tag and tag not in wanted:
                wanted.append(tag)
        max_rarity = 6 if "高级资深干员" in wanted else 5
        pool = await self._recruit_pool()

        candidates: list[dict[str, Any]] = []
        for entry_id, record in table.items():
            if not entry_id.startswith("char_"):
                continue
            rarity = int(
                str(record.get("rarity") or "TIER_1").removeprefix("TIER_") or 1
            )
            if rarity > max_rarity:
                continue
            if pool and (str(record.get("name") or ""), rarity) not in pool:
                continue
            own = _recruit_tags(record)
            if rarity == 6:
                own.add("高级资深干员")
            elif rarity == 5:
                own.add("资深干员")
            elif rarity <= 2:
                own.add("新手")
            matched = own & set(wanted)
            if not matched:
                continue
            candidates.append(
                {
                    "id": entry_id,
                    "name": str(record.get("name") or entry_id),
                    "rarity": rarity,
                    "matched": matched,
                }
            )

        candidates.sort(key=lambda item: -item["rarity"])

        # The reference plugin merges one operator's several matched tags by
        # concatenation, so a later combination is tested with a substring
        # check against that combined string.
        operators: dict[str, dict[str, Any]] = {}
        for item in candidates:
            merged = operators.get(item["name"])
            if merged is None:
                operators[item["name"]] = {
                    "id": item["id"],
                    "name": item["name"],
                    "rarity": item["rarity"],
                    "tags": "".join(sorted(item["matched"])),
                }
            else:
                merged["tags"] += "".join(sorted(item["matched"]))

        combos: list[list[str]] = []
        if len(wanted) == 1:
            combos.append(wanted)
        else:
            for size in range(1, min(len(wanted), _RECRUIT_MAX_COMBINATION) + 1):
                for combo in _combinations(wanted, size):
                    pair = list(combo)
                    if "高级资深干员" in pair and "资深干员" in pair:
                        continue
                    combos.append(pair)

        groups: list[dict[str, Any]] = []
        for combo in combos:
            listed: list[dict[str, Any]] = []
            best = 0
            stopped = False
            for item in operators.values():
                if any(tag not in item["tags"] for tag in combo):
                    continue
                if item["rarity"] == 6 and "高级资深干员" not in combo:
                    continue
                if item["rarity"] >= 4 or item["rarity"] == 1:
                    best = max(best, item["rarity"])
                    listed.append(
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "rarity": item["rarity"],
                            "avatar": _asset_url(
                                "char_portrait", f"{item['id']}_1.png"
                            ),
                        }
                    )
                else:
                    stopped = True
                    break
            if stopped or not listed:
                continue
            groups.append({"tags": combo, "max_rarity": best, "operators": listed})

        groups.sort(key=lambda group: (-len(group["tags"]), -group["max_rarity"]))
        return {
            "tags": wanted,
            "groups": groups,
            "max_rarity": max_rarity,
            "candidates": len(operators),
        }

    async def _avatar(self, char_id: str) -> str:
        """Inline the round avatar of one character or device.

        Args:
            char_id: Character or device identifier.

        Returns:
            A ``data:`` URI, or an empty string when no avatar is published.
        """
        refs = [ref for ref in await self.locate("avatar", char_id) if not ref.is_dir]
        if not refs:
            return ""
        # Prefer the plain ``<id>.png`` avatar; alternate skins carry a
        # ``#<n>`` suffix that the page cannot resolve without escaping.
        preferred = next(
            (ref for ref in refs if ref.path.rsplit("/", 1)[-1] == f"{char_id}.png"),
            refs[0],
        )
        return await self._icon_data(preferred.url)

    async def token_card(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the device page of one operator.

        Args:
            entry: Operator returned by :meth:`match` or :meth:`get_detail`.

        Returns:
            The page data, or ``None`` when the operator deploys no device.
        """
        record = entry.attributes
        token_ids = [str(key) for key in (record.get("displayTokenDict") or {})]
        if not token_ids:
            return None
        characters = await self._character_table()
        skill_table = await self._table("skill_table")
        range_table = await self._table("range_table")
        tokens = []
        for token_id in token_ids:
            token = characters.get(token_id)
            if not isinstance(token, dict):
                continue
            device = _device_card(token_id, token, skill_table, range_table)
            device["avatar"] = await self._avatar(token_id)
            tokens.append(device)
        if not tokens:
            return None
        return {
            "name": entry.title,
            "avatar": await self._avatar(entry.entry_id),
            "tokens": tokens,
        }

    async def card(self, entry: GameDataEntry) -> dict[str, Any]:
        """Collect every field the operator card displays.

        The layout follows the AmiyaBot ``operatorArchives`` card, so the
        mapping is grouped the same way: attribute cells, affiliation cells,
        potential ranks, voice actors, the base range grid and the skill,
        talent, base-skill and device blocks.

        Args:
            entry: Operator returned by :meth:`match` or :meth:`get_detail`.

        Returns:
            Card sections consumed by :func:`providers.card.render_card`.
        """
        record = entry.attributes
        char_id = entry.entry_id
        skill_table = await self._table("skill_table")
        range_table = await self._table("range_table")
        equip = await self._table("uniequip_table")
        item_table = await self._table("item_table")
        items = item_table.get("items") or {}
        building = await self._table("building_data")
        handbooks = await self._table("handbook_info_table")
        teams = await self._table("handbook_team_table")

        profession = str(record.get("profession") or "")
        sub_professions = equip.get("subProfDict") or {}
        sub_record = sub_professions.get(str(record.get("subProfessionId") or "")) or {}
        class_name = _PROFESSIONS.get(profession, profession)
        sub_class = str(sub_record.get("subProfessionName") or "")
        position = _POSITIONS.get(str(record.get("position") or ""), "")

        rarity = int(str(record.get("rarity") or "TIER_1").removeprefix("TIER_") or 1)
        tags = [str(tag) for tag in record.get("tagList") or []]
        for extra in (class_name, position):
            if extra and extra not in tags:
                tags.append(extra)
        if record.get("isSpChar"):
            tags.insert(0, "异格干员")

        phases = record.get("phases") or []
        final: dict[str, Any] = {}
        if phases:
            frames = phases[-1].get("attributesKeyFrames") or []
            if frames:
                final = frames[-1].get("data") or {}
        trust_frames = record.get("favorKeyFrames") or []
        trust = (trust_frames[-1].get("data") if trust_frames else {}) or {}

        attributes: list[dict[str, str]] = []
        for key, label in _ATTRIBUTES:
            base = final.get(key)
            if base is None:
                continue
            if key == "respawnTime":
                attributes.append(
                    {"label": label, "value": f"{format_number(base)}秒", "bonus": ""}
                )
                continue
            bonus = trust.get(key) or 0
            attributes.append(
                {
                    "label": label,
                    "value": format_number(base),
                    "bonus": format_number(bonus) if bonus else "",
                }
            )

        facts = _archive_facts(handbooks.get("handbookDict") or {}, char_id)

        def power(key: Any) -> str:
            team = teams.get(str(key or ""))
            name = team.get("powerName") if isinstance(team, dict) else None
            return str(name or "未知")

        drawer = ""
        skins = await self._drawers()
        if char_id in skins:
            drawer = skins[char_id]

        affiliation = [
            {"label": "势力", "value": power(record.get("nationId"))},
            {"label": "阵营", "value": power(record.get("groupId"))},
            {"label": "队伍", "value": power(record.get("teamId"))},
            {"label": "种族", "value": facts.get("种族") or "未知"},
            {"label": "画师", "value": drawer or "未知"},
            {"label": "生日", "value": facts.get("生日") or "未知"},
        ]

        potential = []
        for rank, item in enumerate(record.get("potentialRanks") or []):
            text = _render(str(item.get("description") or ""), {})
            if not text:
                continue
            icon = _asset_url("potential_icon", f"potential_{rank + 1}.png")
            potential.append(
                {"rank": rank, "text": text, "icon": await self._icon_data(icon)}
            )

        voice_map = (await self._voice_cvs()).get(char_id, {})
        cv = [
            {"lang": display, "names": "、".join(voice_map[code])}
            for code, display in _VOICE_LANGS
            if voice_map.get(code)
        ]

        base_range = _range_cells(
            range_table.get(str((phases[-1].get("rangeId") if phases else "") or ""))
        )

        trait_candidates = (record.get("trait") or {}).get("candidates") or []
        trait_board = {
            str(item.get("key")): item.get("value")
            for item in (
                trait_candidates[0].get("blackboard") if trait_candidates else []
            )
            or []
            if "key" in item
        }
        trait = _render(entry.summary, trait_board)

        impression = []
        usage = str(record.get("itemUsage") or "").strip()
        quote = str(record.get("itemDesc") or "").strip()
        if usage:
            impression.append(usage)
        if quote:
            impression.append(f"「{quote}」")

        token_item = items.get(str(record.get("potentialItemId") or "")) or {}
        token_name = str(token_item.get("name") or "").strip()
        token_text = str(token_item.get("description") or "").strip()
        token = (
            {"name": token_name or "信物", "text": token_text} if token_text else None
        )

        talents = []
        for talent in record.get("talents") or []:
            candidates = talent.get("candidates") or []
            if not candidates:
                continue
            best = candidates[-1]
            name = str(best.get("name") or "").strip()
            board = {
                str(item.get("key")): item.get("value")
                for item in best.get("blackboard") or []
                if "key" in item
            }
            description = _render(str(best.get("description") or ""), board)
            if name or description:
                talents.append({"name": name, "text": description})

        skills = []
        for item in record.get("skills") or []:
            skill_id = str(item.get("skillId") or "")
            levels = ((skill_table.get(skill_id) or {}).get("levels")) or []
            if not levels:
                continue
            level = levels[-1]
            board = {
                str(pair.get("key")): pair.get("value")
                for pair in level.get("blackboard") or []
                if "key" in pair
            }
            sp = level.get("spData") or {}
            sp_type = str(sp.get("spType") or "")
            range_id = level.get("rangeId") or (
                phases[-1].get("rangeId") if phases else None
            )
            icon_url = _asset_url("skill_icon", f"skill_icon_{skill_id}.png")
            skills.append(
                {
                    "name": str(level.get("name") or skill_id),
                    "icon": await self._icon_data(icon_url),
                    "sp_type": sp_type,
                    "sp_type_name": _SP_TYPES.get(sp_type, ""),
                    "skill_type_name": _SKILL_TYPES.get(
                        str(level.get("skillType") or ""), ""
                    ),
                    "sp_init": format_number(sp.get("initSp") or 0),
                    "sp_cost": format_number(sp["spCost"]) if sp.get("spCost") else "",
                    "duration": format_number(
                        max(float(level.get("duration") or 0), 0.0)
                    ),
                    "text": _render(level.get("description") or "", board),
                    "range": _range_cells(range_table.get(str(range_id or ""))),
                }
            )

        devices: list[dict[str, Any]] = []

        base_skills = _building_cards(char_id, building)
        for item in base_skills:
            # Room glyphs ship white for the in-game dark UI; skill badges are
            # already coloured.
            item["icon"] = await self._icon_data(
                item["icon"], darken_white=item.pop("monochrome", False)
            )

        class_icon = await self._icon_data(
            _asset_url("profession_icon", f"icon_profession_{profession.lower()}.png")
        )

        art_url = ""
        portrait = ""
        for kind, key in (("arts", "art"), ("portrait", "portrait")):
            for ref in await self.locate(kind, char_id):
                if ref.is_dir:
                    continue
                if key == "art":
                    art_url = ref.url
                else:
                    portrait = ref.url
                break

        # The illustration is inlined instead of hot-linked: the render service
        # silently drops multi-megabyte remote images when they are slow, which
        # produced cards without artwork.
        art = await self._art_data(art_url)

        return {
            "game": self.game,
            "name": entry.title,
            "number": str(record.get("displayNumber") or ""),
            "en_name": str(record.get("appellation") or ""),
            "rarity": rarity,
            "class_name": class_name,
            "class_icon": class_icon,
            "class_enum": profession,
            "sub_class": sub_class,
            "position": position,
            "tags": tags,
            "attributes": attributes,
            "affiliation": affiliation,
            "potential": potential,
            "real_name": facts.get("真名") or "未知",
            "cv": cv,
            "range": base_range,
            "trait": trait,
            "impression": impression,
            "token": token,
            "talents": talents,
            "base_skills": base_skills,
            "skills": skills,
            "devices": devices,
            "art": art,
            "portrait": portrait or art_url,
        }

    async def module_panel(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the modules of one operator.

        Args:
            entry: The operator entry.

        Returns:
            Panel data for ``render_panel``, or ``None`` when every module is a
            plain badge without any upgrade.
        """
        equip = await self._table("uniequip_table")
        battle = await self._table("battle_equip_table")
        modules = (equip.get("charEquip") or {}).get(entry.entry_id) or []
        infos = equip.get("equipDict") or {}
        sections = []
        for module_id in modules:
            info = infos.get(module_id) or {}
            rows = []
            for phase in (battle.get(module_id) or {}).get("phases") or []:
                gained = [
                    f"{_MODULE_ATTRS.get(str(item.get('key')), item.get('key'))}"
                    f" +{format_number(item.get('value'))}"
                    for item in phase.get("attributeBlackboard") or []
                    if item.get("value")
                ]
                if gained:
                    rows.append((f"Lv{phase.get('equipLevel')}", "  ".join(gained)))
            if not rows:
                continue
            type_name = " ".join(
                str(part)
                for part in (info.get("typeName1"), info.get("typeName2"))
                if part
            )
            sections.append(
                {
                    "title": str(info.get("uniEquipName") or module_id),
                    "meta": type_name or None,
                    "rows": rows,
                    "text": _render(str(info.get("uniEquipDesc") or ""), {}),
                }
            )
        if not sections:
            return None
        return {
            "title": entry.title,
            "subtitle": "模组",
            "avatar": await self._avatar(entry.entry_id),
            "sections": sections,
        }

    async def skin_panel(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the published skins of one operator.

        Args:
            entry: The operator entry.

        Returns:
            Panel data with one image per skin, or ``None`` when the operator
            has no skin record.
        """
        skins = await self._table("skin_table")
        records = skins.get("charSkins") or {}
        images = []
        seen: dict[str, int] = {}
        for skin_id, skin in records.items():
            if not str(skin_id).startswith(entry.entry_id):
                continue
            display = skin.get("displaySkin") or {}
            caption = str(display.get("skinName") or display.get("skinGroupName") or "")
            portrait = str(skin.get("portraitId") or "")
            if not portrait:
                continue
            if not caption:
                caption = "默认服装"
            # Several skins share the default group name; number the repeats so
            # the captions stay distinguishable.
            seen[caption] = seen.get(caption, 0) + 1
            if seen[caption] > 1:
                caption = f"{caption} {seen[caption]}"
            images.append(
                {
                    "src": _asset_url("char_portrait", f"{portrait}.png"),
                    "caption": caption,
                }
            )
        if not images:
            return None
        return {
            "title": entry.title,
            "subtitle": f"皮肤 · {len(images)} 套",
            "avatar": await self._avatar(entry.entry_id),
            "sections": [{"title": "立绘", "images": images}],
        }

    async def story_panel(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the archive text of one operator.

        Args:
            entry: The operator entry.

        Returns:
            Panel data with one section per archive block, or ``None`` when the
            operator publishes no archive.
        """
        handbooks = await self._table("handbook_info_table")
        record = (handbooks.get("handbookDict") or {}).get(entry.entry_id) or {}
        sections = []
        for block in record.get("storyTextAudio") or []:
            title = str(block.get("storyTitle") or "")
            if title not in _ARCHIVE_BLOCKS:
                continue
            text = "\n".join(
                str(story.get("storyText") or "").strip()
                for story in block.get("stories") or []
                if str(story.get("storyText") or "").strip()
            )
            if text:
                sections.append({"title": title, "text": text})
        if not sections:
            return None
        sections.sort(key=lambda item: _ARCHIVE_BLOCKS.index(item["title"]))
        return {
            "title": entry.title,
            "subtitle": "档案",
            "avatar": await self._avatar(entry.entry_id),
            "sections": sections,
        }

    async def faction_panel(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the operators of the same faction.

        Args:
            entry: The operator entry.

        Returns:
            Panel data listing every operator in the same group, or ``None``
            when the operator belongs to no group.
        """
        group = str(entry.attributes.get("groupId") or "")
        if not group:
            return None
        teams = await self._table("handbook_team_table")
        characters = await self._character_table()
        members = [
            (char_id, record)
            for char_id, record in characters.items()
            if char_id.startswith("char_") and str(record.get("groupId") or "") == group
        ]
        if not members:
            return None
        rows = []
        for char_id, record in members:
            tier = str(record.get("rarity") or "TIER_1").removeprefix("TIER_") or "1"
            rows.append((str(record.get("name") or char_id), f"{tier}★"))
        name = str(((teams.get(group) or {}).get("powerName")) or group)
        return {
            "title": entry.title,
            "subtitle": f"阵营 · {name}",
            "avatar": await self._avatar(entry.entry_id),
            "sections": [{"title": f"{name}（{len(rows)} 人）", "rows": rows}],
        }

    async def voice_panel(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the voice lines of one operator.

        Args:
            entry: The operator entry.

        Returns:
            Panel data with one section per voice language, or ``None`` when
            the operator publishes no voice line.
        """
        words = (await self._table("charword_table")).get("charWords") or {}
        lines = [
            word
            for word in words.values()
            if str(word.get("charId") or "") == entry.entry_id
            and str(word.get("voiceText") or "").strip()
        ]
        if not lines:
            return None
        lines.sort(key=lambda item: int(item.get("voiceIndex") or 0))
        rows = [
            (str(word.get("voiceTitle") or ""), str(word.get("voiceText") or ""))
            for word in lines
            if str(word.get("voiceText") or "").strip()
        ]
        if not rows:
            return None
        return {
            "title": entry.title,
            "subtitle": f"语音 · {len(rows)} 条",
            "avatar": await self._avatar(entry.entry_id),
            "sections": [{"title": "语音记录", "rows": rows}],
        }

    async def list_dir(self, path: str) -> list[ResourceRef]:
        """List one directory of the mirror.

        Args:
            path: Relative directory path; an empty string lists the root.

        Returns:
            The immediate children of the directory.

        Raises:
            ValueError: If the path leaves the published trees.
            LookupError: If the directory does not exist upstream.
        """
        path = unquote(path).strip("/")
        parts = path.split("/") if path else []
        if any(part in (".", "..") for part in parts) or (
            parts and parts[0] not in ("raw", "gamedata")
        ):
            raise ValueError("Directory must stay under raw/ or gamedata/")

        payload = await self.net.get_json(
            f"{self.root}/api/v1/files/{quote(path, safe='')}"
        )
        children = payload.get("children") if isinstance(payload, dict) else None
        if not isinstance(children, list):
            raise LookupError(f"Directory not found: {path or '/'}")

        refs: list[ResourceRef] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            name = str(child.get("name") or "")
            if not name or name == "latest" or name in (".", ".."):
                continue
            is_dir = bool(child.get("is_dir"))
            child_path = str(child.get("path") or (f"{path}/{name}" if path else name))
            size = child.get("size")
            refs.append(
                ResourceRef(
                    game=self.game,
                    kind="directory" if is_dir else "file",
                    path=child_path,
                    url=(
                        f"{self.root}/{quote(child_path, safe='/')}"
                        if is_dir
                        else self._url(child_path)
                    ),
                    size=size if isinstance(size, int) else None,
                    is_dir=is_dir,
                )
            )
        return refs

    async def locate(self, kind: str, key: str) -> list[ResourceRef]:
        """Resolve resources by kind and identifier.

        Supported kinds are ``table`` (a file name in the newest ``excel``
        directory), ``asset`` and ``directory`` (an explicit path), plus
        ``portrait``, ``avatar`` and ``arts`` (an operator identifier).

        Args:
            kind: Resource category.
            key: Table name, path, or operator identifier.

        Returns:
            Matching references; empty when nothing matches.

        Raises:
            ValueError: If the kind is unsupported or the key is malformed.
        """
        if kind == "table":
            name = key if key.endswith(".json") else f"{key}.json"
            if "/" in name or name in (".json", "..json"):
                raise ValueError("Table name must be a single file name")
            candidates = {name}
            if not key.endswith("_table"):
                candidates.add(f"{key}_table.json")
            directory = await self._latest_resource_dir()
            prefix = f"{directory}/excel/"
            return [
                ref
                for ref in await self.list_dir(f"{directory}/excel")
                if not ref.is_dir
                and ref.path.startswith(prefix)
                and ref.path[len(prefix) :] in candidates
            ]

        if kind in ("asset", "directory"):
            path = unquote(key).strip("/")
            if any(part in (".", "..") for part in path.split("/")):
                raise ValueError("Resource path must not contain traversal")
            parent, _, name = path.rpartition("/")
            if not name:
                raise ValueError("Resource path must name a file or directory")
            return [
                ref
                for ref in await self.list_dir(parent)
                if ref.path == path and (kind != "directory" or ref.is_dir)
            ]

        if kind in _CHARACTER_DIRS:
            directory = _CHARACTER_DIRS[kind]
            stem = key.strip()
            if not stem or "/" in stem:
                raise ValueError("Character identifier must be a single component")
            return [
                ref
                for ref in await self.list_dir(directory)
                if not ref.is_dir and ref.path.rsplit("/", 1)[-1].startswith(stem)
            ]

        raise ValueError(f"Unsupported Arknights resource kind: {kind}")

    async def fetch(self, ref: ResourceRef) -> bytes:
        """Download a resource returned by this provider.

        Args:
            ref: Reference produced by :meth:`list_dir` or :meth:`locate`.

        Returns:
            The resource bytes.

        Raises:
            ValueError: If the reference is not a fetchable Arknights file.
            IsADirectoryError: If the reference points at a directory.
        """
        if ref.game != self.game:
            raise ValueError("Resource belongs to a different game")
        if ref.is_dir:
            raise IsADirectoryError(ref.path)

        path = unquote(ref.path).strip("/")
        if any(part in (".", "..") for part in path.split("/")):
            raise ValueError("Resource path must not contain traversal")
        expected = self._url(path)

        parts = urlsplit(ref.url)
        if (
            ref.url != expected
            or parts.scheme != "https"
            or parts.hostname != "torappu.prts.wiki"
        ):
            raise ValueError("Resource URL does not match the PRTS mirror")
        return await self.net.get_bytes(expected)
