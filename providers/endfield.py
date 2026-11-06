"""Endfield (终末地) provider backed by the AKEData mirror."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..models import GameDataEntry, ResourceRef
from .base import GameDataProvider, format_number
from .card import encode_art
from .net import UpstreamError

# Dataset name used by the mirror's index, mapped to its public root.
_DATASETS = {"images": "public/images", "json": "public/Json"}

# Character sprite files, mapped from a kind to its directory suffix and
# file-name template.
_CHARACTER_FILES = {
    "image": ("/charimg510/", "{id}.png"),
    "portrait": ("/characterportrait/", "{id}.png"),
    "avatar": ("/charicon/", "icon_{id}.png"),
    "roundicon": ("/charroundicon/", "icon_round_{id}.png"),
}

# Max-level attribute rows of the character card, in display order. Defence is
# omitted on purpose: every published record carries 0 there.
_STAT_ROWS = ((1, "生命值"), (2, "攻击力"))

# Main attributes of the game, shown next to the combat values.
_ATTRIBUTE_ROWS = ((39, "力量"), (40, "敏捷"), (41, "智识"), (42, "意志"))

# Voice languages published by ``CharacterTable[].cvName``.
_VOICE_LANGS = (
    ("ChiCVName", "中文-普通话"),
    ("JapCVName", "日文"),
    ("KorCVName", "韩文"),
    ("EngCVName", "英文"),
)


def _checked(path: str) -> str:
    """Validate a site-relative path.

    Args:
        path: Unencoded path relative to the mirror root.

    Returns:
        The path without a leading or trailing slash.

    Raises:
        ValueError: If the path is unsafe or malformed.
    """
    path = unquote(path).strip("/")
    if not path or "\\" in path or "?" in path or "#" in path:
        raise ValueError(f"Invalid resource path: {path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError(f"Invalid resource path: {path!r}")
    return path


class EndfieldProvider(GameDataProvider):
    """Read Endfield characters and resources from ``data.akedata.wiki``.

    The mirror publishes a version manifest, an index of every static file and
    a per-version table directory. The table directory itself is not
    enumerable, so table names are resolved from a fixed list.
    """

    game = "endfield"
    root = "https://data.akedata.wiki"
    character_table = "CharacterTable"

    def __init__(self, net: Any) -> None:
        """Initialize the provider.

        Args:
            net: Client exposing asynchronous ``get_bytes`` and ``get_json``.
        """
        self.net = net
        self._index: dict[str, dict[str, Any]] | None = None
        self._table_dir: str | None = None
        self._i18n: tuple[str, dict[str, str]] | None = None
        self._tables: dict[str, dict[str, Any]] = {}
        self._icons: dict[str, str] = {}

    def _url(self, path: str) -> str:
        """Build the download URL of a site-relative path.

        Args:
            path: Validated path relative to the mirror root.

        Returns:
            The absolute download URL.
        """
        return f"{self.root}/{quote(path, safe='/')}"

    async def _load_index(self) -> dict[str, dict[str, Any]]:
        """Load the static file index.

        Returns:
            Mapping from site-relative path to its index metadata.

        Raises:
            ValueError: If the index has an unsupported shape.
        """
        if self._index is not None:
            return self._index

        document = await self.net.get_json(f"{self.root}/asset-sync-index.json")
        datasets = document.get("datasets") if isinstance(document, dict) else None
        if not isinstance(datasets, dict):
            raise ValueError("Unexpected Endfield asset index")

        index: dict[str, dict[str, Any]] = {}
        for name, root in _DATASETS.items():
            dataset = datasets.get(name)
            files = dataset.get("files") if isinstance(dataset, dict) else None
            if not isinstance(files, dict):
                raise ValueError(f"Missing '{name}' dataset in the asset index")
            for relative, metadata in files.items():
                if isinstance(metadata, dict):
                    index[self._url_path(root, relative)] = metadata
        self._index = index
        return index

    @staticmethod
    def _url_path(root: str, relative: str) -> str:
        """Join a dataset root with a relative index path.

        Args:
            root: Public dataset root such as ``public/images``.
            relative: Path recorded in the index.

        Returns:
            The site-relative path.
        """
        return f"{root}/{relative.lstrip('/')}"

    async def _latest_table_dir(self) -> str:
        """Resolve the table directory of the newest game version.

        Returns:
            The site-relative table directory.

        Raises:
            ValueError: If the manifest is malformed.
            LookupError: If the newest version cannot be resolved.
        """
        if self._table_dir is not None:
            return self._table_dir

        manifest = await self.net.get_json(f"{self.root}/manifest.json")
        versions = manifest.get("versions") if isinstance(manifest, dict) else None
        latest = manifest.get("latest") if isinstance(manifest, dict) else None
        if not isinstance(versions, list):
            raise ValueError("Unexpected Endfield manifest")

        for version in versions:
            if isinstance(version, dict) and version.get("id") == latest:
                directory = version.get("tableCfgPath")
                if isinstance(directory, str) and directory:
                    self._table_dir = _checked(directory)
                    return self._table_dir
        raise LookupError(f"Unknown Endfield version: {latest!r}")

    async def _character_table(self) -> dict[str, Any]:
        """Load the character table of the newest version.

        Returns:
            Mapping of character identifier to its raw table record.

        Raises:
            ValueError: If the upstream table is not a JSON object.
        """
        directory = await self._latest_table_dir()
        table = await self.net.get_json(
            self._url(f"{directory}/{self.character_table}.json")
        )
        if not isinstance(table, dict):
            raise ValueError("Unexpected Endfield character table")
        return table

    async def _table_cfg(self, name: str) -> dict[str, Any]:
        """Load one auxiliary table of the newest version.

        Args:
            name: Table file name without the ``.json`` suffix.

        Returns:
            The decoded table, memoized for the provider lifetime.

        Raises:
            ValueError: If the upstream table is not a JSON object.
        """
        cached = self._tables.get(name)
        if cached is not None:
            return cached
        directory = await self._latest_table_dir()
        table = await self.net.get_json(f"{self._url(directory)}/{name}.json")
        if not isinstance(table, dict):
            raise ValueError(f"Unexpected Endfield table: {name}")
        self._tables[name] = table
        return table

    @staticmethod
    def _localized(node: Any, i18n: dict[str, str]) -> str:
        """Resolve one ``{id, text}`` node through the localization table.

        Args:
            node: Upstream text node, or anything else.
            i18n: Localization mapping keyed by stringified id.

        Returns:
            The readable text, or an empty string when it is unpublished.
        """
        if not isinstance(node, dict):
            return ""
        text = str(node.get("text") or "")
        if text:
            return text
        identity = node.get("id")
        return i18n.get(str(identity), "") if identity is not None else ""

    @staticmethod
    def _archive_facts(record: dict[str, Any], i18n: dict[str, str]) -> dict[str, str]:
        """Parse the ``【key】value`` pairs of an operator's basic archive.

        Args:
            record: Raw ``CharacterTable`` record.
            i18n: Localization mapping.

        Returns:
            Facts keyed by their Chinese label, first occurrence wins.
        """
        facts: dict[str, str] = {}
        for entry in record.get("profileRecord") or []:
            text = EndfieldProvider._localized(entry.get("recordDesc"), i18n)
            for line in text.splitlines():
                match = re.match(r"^【([^】]{1,12})】\s*(.*)$", line.strip())
                if match and match.group(2).strip():
                    facts.setdefault(match.group(1), match.group(2).strip())
        return facts

    async def _i18n_table(self) -> dict[str, str]:
        """Load the simplified Chinese localization table of the newest version.

        The published character table keeps only ``name.id``: every ``text``
        field is empty and the readable strings live in this side table.

        Returns:
            Mapping from localization id to its Chinese text.

        Raises:
            ValueError: If the upstream table is not a JSON object.
        """
        directory = await self._latest_table_dir()
        if self._i18n is not None and self._i18n[0] == directory:
            return self._i18n[1]
        table = await self.net.get_json(f"{self._url(directory)}/I18nTextTable_CN.json")
        if not isinstance(table, dict):
            raise ValueError("Unexpected Endfield localization table")
        cleaned = {
            str(key): str(value)
            for key, value in table.items()
            if isinstance(value, str) and value
        }
        self._i18n = (directory, cleaned)
        return cleaned

    @staticmethod
    def _display_name(record: dict[str, Any], i18n: dict[str, str]) -> str:
        """Resolve the display name of one character.

        Args:
            record: Raw character table record.
            i18n: Localization mapping, possibly empty.

        Returns:
            The Chinese name when published, otherwise the English name or the
            identifier.
        """
        name = record.get("name")
        if isinstance(name, dict):
            text = str(name.get("text") or "")
            if not text:
                text = str(i18n.get(str(name.get("id"))) or "")
            if text:
                return text
        return str(record.get("engName") or "")

    def _entry(
        self,
        entry_id: str,
        record: dict[str, Any],
        source_url: str,
        i18n: dict[str, str],
    ) -> GameDataEntry:
        """Normalize one character table record.

        Args:
            entry_id: Character table key.
            record: Raw upstream record.
            source_url: URL the record was read from.
            i18n: Localization mapping used to resolve the display name.

        Returns:
            The normalized entry with raw fields preserved in ``attributes``.
        """
        title = self._display_name(record, i18n) or entry_id
        return GameDataEntry(
            game="endfield",
            category="character",
            entry_id=entry_id,
            title=title,
            summary=str(record.get("department") or ""),
            source_url=source_url,
            attributes=dict(record),
        )

    async def search(self, keyword: str, limit: int = 5) -> list[GameDataEntry]:
        """Search characters by identifier, English name or published name.

        Args:
            keyword: Case-insensitive text to match.
            limit: Maximum number of entries.

        Returns:
            Matching characters in table order.
        """
        query = keyword.strip().casefold()
        if not query or limit <= 0:
            return []

        directory = await self._latest_table_dir()
        source_url = self._url(f"{directory}/{self.character_table}.json")
        i18n = await self._i18n_table()
        results: list[GameDataEntry] = []
        for entry_id, record in (await self._character_table()).items():
            if not isinstance(record, dict):
                continue
            fields = (
                entry_id,
                str(record.get("engName") or ""),
                self._display_name(record, i18n),
            )
            if any(query in value.casefold() for value in fields if value):
                results.append(self._entry(entry_id, record, source_url, i18n))
                if len(results) >= limit:
                    break
        return results

    async def get_detail(self, entry_id: str) -> GameDataEntry:
        """Resolve one character by its exact table key.

        Args:
            entry_id: Character identifier such as ``chr_0004_pelica``.

        Returns:
            The character with raw table fields in ``attributes``.

        Raises:
            LookupError: If the key is absent from the character table.
        """
        directory = await self._latest_table_dir()
        record = (await self._character_table()).get(entry_id)
        if not isinstance(record, dict):
            raise LookupError(f"Unknown Endfield character: {entry_id}")
        return self._entry(
            entry_id,
            record,
            self._url(f"{directory}/{self.character_table}.json"),
            await self._i18n_table(),
        )

    async def match(self, text: str) -> GameDataEntry | None:
        """Resolve a single character name, preferring exact matches.

        Only English names and identifiers are usable here because the
        published table carries empty Chinese text.

        Args:
            text: A single token typed after the wake word.

        Returns:
            The matched character, or ``None`` when nothing matched or the
            substring search was ambiguous.
        """
        query = text.strip().casefold()
        if not query:
            return None
        directory = await self._latest_table_dir()
        source_url = self._url(f"{directory}/{self.character_table}.json")
        i18n = await self._i18n_table()
        partial: list[tuple[str, dict[str, Any]]] = []
        for entry_id, record in (await self._character_table()).items():
            if not isinstance(record, dict):
                continue
            names = {
                entry_id.casefold(),
                str(record.get("engName") or "").casefold(),
                self._display_name(record, i18n).casefold(),
            }
            names.discard("")
            if query in names:
                return self._entry(entry_id, record, source_url, i18n)
            if any(query in value for value in names):
                partial.append((entry_id, record))
        if len(partial) != 1:
            return None
        entry_id, record = partial[0]
        return self._entry(entry_id, record, source_url, i18n)

    async def _art_data(self, url: str) -> str:
        """Inline the character illustration as a downscaled JPEG data URI.

        Args:
            url: Absolute illustration URL, or an empty string.

        Returns:
            A ``data:`` URI, or an empty string when the illustration is
            absent or cannot be downloaded.
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
        encoded = encode_art(payload)
        if not encoded:
            return ""
        self._icons[url] = encoded
        return encoded

    async def token_card(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Return no device page.

        Args:
            entry: Character whose devices are requested.

        Returns:
            Always ``None``: Endfield publishes no deployable devices.
        """
        return None

    async def card(self, entry: GameDataEntry) -> dict[str, Any]:
        """Collect the fields the character card displays.

        The card reuses the Arknights layout: the attribute table holds the
        max-level values and the main attributes, the affiliation table holds
        the affiliation, race, birthday, class, abilities and weapon, and the
        potential and voice lines sit below.

        Args:
            entry: Character returned by :meth:`match` or :meth:`get_detail`.

        Returns:
            Card sections consumed by :func:`providers.card.render_card`.
        """
        record = entry.attributes
        i18n = await self._i18n_table()
        rarity = record.get("rarity")

        values: dict[int, Any] = {}
        frames = record.get("attributes") or []
        if frames:
            attrs = (frames[-1].get("Attribute") or {}).get("attrs") or []
            values = {
                int(item["attrType"]): item.get("attrValue")
                for item in attrs
                if isinstance(item, dict) and "attrType" in item
            }

        attributes = [
            {"label": label, "value": format_number(values[type_id])}
            for type_id, label in _STAT_ROWS + _ATTRIBUTE_ROWS
            if values.get(type_id) is not None
        ]

        facts = self._archive_facts(record, i18n)
        professions = await self._table_cfg("CharProfessionTable")
        profession = professions.get(str(record.get("profession") or "")) or {}
        class_name = self._localized(profession.get("name"), i18n)

        ability = dict(_ATTRIBUTE_ROWS)
        main_attr = ability.get(int(record.get("mainAttrType") or 0), "")
        sub_attr = ability.get(int(record.get("subAttrType") or 0), "")

        weapon = ""
        weapon_id = str(record.get("defaultWeaponId") or "")
        if weapon_id:
            items = await self._table_cfg("ItemTable")
            weapon = self._localized((items.get(weapon_id) or {}).get("name"), i18n)
            if not weapon:
                weapons = await self._table_cfg("WeaponBasicTable")
                weapon = self._localized(
                    (weapons.get(weapon_id) or {}).get("engName"), i18n
                )

        affiliation = [
            {"label": label, "value": value}
            for label, value in (
                ("阵营", facts.get("身份认证") or str(record.get("department") or "")),
                ("种族", facts.get("种族") or ""),
                ("生日", facts.get("生日") or ""),
                ("职业", class_name),
                ("主能力", main_attr),
                ("副能力", sub_attr),
                ("武器", weapon),
            )
            if value
        ]

        potential: list[dict[str, Any]] = []
        bundles = (
            (await self._table_cfg("CharacterPotentialTable")).get(record.get("charId"))
            or {}
        ).get("potentialUnlockBundle") or []
        for item in bundles:
            level = int(item.get("level") or 0)
            name = self._localized(item.get("name"), i18n)
            if not level or not name:
                continue
            potential.append({"rank": level - 1, "text": f"潜能{level} {name}"})

        cv = [
            {
                "lang": label,
                "names": self._localized((record.get("cvName") or {}).get(key), i18n),
            }
            for key, label in _VOICE_LANGS
            if self._localized((record.get("cvName") or {}).get(key), i18n)
        ]

        art = ""
        portrait = ""
        for kind in ("portrait", "image"):
            for ref in await self.locate(kind, entry.entry_id):
                if not ref.is_dir:
                    if not art:
                        art = ref.url
                    if kind == "portrait":
                        portrait = ref.url
                    break

        return {
            "game": self.game,
            "name": entry.title,
            "number": "",
            "en_name": str(record.get("engName") or ""),
            "rarity": int(rarity) if isinstance(rarity, int) else 0,
            "class_name": class_name,
            "class_enum": "",
            "sub_class": "",
            "position": "",
            "tags": [],
            "attributes": attributes,
            "affiliation": affiliation,
            "potential": potential,
            "real_name": "",
            "cv": cv,
            "range": [],
            "trait": "",
            "impression": [],
            "token": None,
            "talents": [],
            "base_skills": [],
            "skills": [],
            "devices": [],
            "art": await self._art_data(art or portrait),
            "portrait": portrait or art,
        }

    async def list_dir(self, path: str) -> list[ResourceRef]:
        """List one directory of the static index.

        Args:
            path: Site-relative directory; an empty string lists the roots.

        Returns:
            The immediate children known to the index.

        Raises:
            ValueError: If the path is unsafe.
            LookupError: If the directory has no indexed children.
        """
        path = path.strip("/")
        if not path:
            return [
                ResourceRef(
                    game=self.game,
                    kind="directory",
                    path=root,
                    url=self._url(root),
                    is_dir=True,
                )
                for root in _DATASETS.values()
            ]

        path = _checked(path)
        index = await self._load_index()
        prefix = f"{path}/"
        children: dict[str, ResourceRef] = {}
        for child_path, metadata in index.items():
            if not child_path.startswith(prefix):
                continue
            head, separator, _ = child_path[len(prefix) :].partition("/")
            child = f"{prefix}{head}"
            size = metadata.get("size")
            children[child] = ResourceRef(
                game=self.game,
                kind="directory" if separator else "file",
                path=child,
                url=self._url(child),
                size=size if isinstance(size, int) else None,
                is_dir=bool(separator),
            )
        if not children:
            raise LookupError(f"Directory not found: {path}")
        return [children[key] for key in sorted(children)]

    async def locate(self, kind: str, key: str) -> list[ResourceRef]:
        """Resolve resources by kind and identifier.

        ``images`` and ``json`` take a path relative to that dataset, ``table``
        takes a table file name (resolved against the newest version directory,
        which the mirror does not allow listing), and ``portrait``, ``avatar``
        and ``roundicon`` take a character identifier.

        Args:
            kind: Resource category.
            key: Dataset path, table name, or character identifier.

        Returns:
            Matching references; empty when nothing matches.

        Raises:
            ValueError: If the kind is unsupported or the key is malformed.
        """
        if kind in _DATASETS:
            path = self._url_path(_DATASETS[kind], _checked(key))
            index = await self._load_index()
            return (
                [
                    ResourceRef(
                        game=self.game,
                        kind="file",
                        path=path,
                        url=self._url(path),
                        size=index[path].get("size"),
                    )
                ]
                if path in index
                else []
            )

        if kind == "table":
            name = key if key.endswith(".json") else f"{key}.json"
            if "/" in name or name in (".json", "..json"):
                raise ValueError("Table name must be a single file name")
            path = f"{await self._latest_table_dir()}/{name}"
            return [ResourceRef(self.game, "table", path, self._url(path))]

        if kind in _CHARACTER_FILES:
            directory, template = _CHARACTER_FILES[kind]
            name = template.format(id=key.strip())
            if not name or "/" in name:
                raise ValueError("Character identifier must be a single component")
            index = await self._load_index()
            return [
                ResourceRef(
                    game=self.game,
                    kind="file",
                    path=path,
                    url=self._url(path),
                    size=index[path].get("size"),
                )
                for path in sorted(index)
                if path.endswith(f"{directory}{name}")
            ]

        raise ValueError(f"Unsupported Endfield resource kind: {kind}")

    async def fetch(self, ref: ResourceRef) -> bytes:
        """Download a resource returned by this provider.

        Args:
            ref: Reference produced by :meth:`list_dir` or :meth:`locate`.

        Returns:
            The resource bytes.

        Raises:
            ValueError: If the reference is not a fetchable Endfield file.
            IsADirectoryError: If the reference points at a directory.
        """
        if ref.game != self.game:
            raise ValueError("Resource belongs to a different game")
        if ref.is_dir:
            raise IsADirectoryError(ref.path)

        path = _checked(ref.path)
        expected = self._url(path)

        parts = urlsplit(ref.url)
        if (
            ref.url != expected
            or parts.scheme != "https"
            or parts.hostname != "data.akedata.wiki"
        ):
            raise ValueError("Resource URL does not match the AKEData mirror")
        return await self.net.get_bytes(expected)
