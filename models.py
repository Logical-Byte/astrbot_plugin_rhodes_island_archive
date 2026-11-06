"""Data models shared by providers and future command handlers."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GameDataEntry:
    """A single normalised record returned by a game data provider.

    Attributes:
        game: Game identifier, either ``arknights`` or ``endfield``.
        category: Record category, e.g. ``operator`` or ``item``.
        entry_id: Provider-specific identifier of the record.
        title: Display title of the record.
        summary: Short plain-text summary of the record.
        source_url: Upstream page the record was taken from, if any.
        attributes: Extra free-form fields the provider wants to expose.
    """

    game: str
    category: str
    entry_id: str
    title: str
    summary: str = ""
    source_url: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResourceRef:
    """A game resource or directory exposed by an upstream provider.

    Attributes:
        game: Provider identifier.
        kind: Resource category.
        path: Provider-relative path.
        url: Canonical upstream URL.
        size: File size in bytes when known.
        is_dir: Whether the reference is a directory.
    """

    game: str
    kind: str
    path: str
    url: str
    size: int | None = None
    is_dir: bool = False

    def __str__(self) -> str:
        return f"{self.game}:{self.kind}:{self.path}"


class SafeHtml(str):
    """A string that already carries HTML markup.

    Provider helpers return descriptions with ``<span class="mark">`` around
    the values the game highlights. The card template must not escape those,
    while every other string it renders is escaped as usual.
    """
