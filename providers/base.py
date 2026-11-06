"""Common interface for game data and resource providers."""

from abc import ABC, abstractmethod
from typing import Any

from ..models import GameDataEntry, ResourceRef


def format_number(value: Any) -> str:
    """Render an upstream number without trailing zeros.

    Args:
        value: Integer or float taken from an upstream table.

    Returns:
        ``1944`` for ``1944.0``, ``15.5`` for ``15.5``, and the original
        text for anything that is not a number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


class GameDataProvider(ABC):
    """Expose character records and upstream resources."""

    game: str

    @abstractmethod
    async def search(self, keyword: str, limit: int = 5) -> list[GameDataEntry]:
        """Search character records.

        Args:
            keyword: Character name or identifier.
            limit: Maximum number of entries.

        Returns:
            Matching character records.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_detail(self, entry_id: str) -> GameDataEntry:
        """Get a character record by exact identifier.

        Args:
            entry_id: Provider-specific character identifier.

        Returns:
            The character record and its upstream fields.
        """
        raise NotImplementedError

    @abstractmethod
    async def match(self, text: str) -> GameDataEntry | None:
        """Resolve a character name with exact match taking priority.

        Args:
            text: A single character name, nickname or identifier.

        Returns:
            The matched character, or ``None`` when nothing matches or the
            result would be ambiguous.
        """
        raise NotImplementedError

    @abstractmethod
    async def card(self, entry: GameDataEntry) -> dict[str, Any]:
        """Collect every field the character card displays.

        Args:
            entry: The character to describe.

        Returns:
            Card sections shared by the text and the HTML renderer.
        """
        raise NotImplementedError

    @abstractmethod
    async def token_card(self, entry: GameDataEntry) -> dict[str, Any] | None:
        """Collect the separate device page of one character.

        Args:
            entry: Character whose devices are requested.

        Returns:
            Page data with a ``name``, ``avatar`` and ``tokens`` list, or
            ``None`` when the game publishes no devices for the character.
        """
        raise NotImplementedError

    @abstractmethod
    async def list_dir(self, path: str) -> list[ResourceRef]:
        """List immediate children of a provider-relative directory.

        Args:
            path: Directory path relative to the source root.

        Returns:
            File and directory references.
        """
        raise NotImplementedError

    @abstractmethod
    async def locate(self, kind: str, key: str) -> list[ResourceRef]:
        """Find resources matching a kind and identifier.

        Args:
            kind: Provider-specific resource category.
            key: Resource identifier or path.

        Returns:
            Matching resource references, possibly empty.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, ref: ResourceRef) -> bytes:
        """Fetch the bytes of a provider resource.

        Args:
            ref: Reference returned by this provider.

        Returns:
            The resource bytes.
        """
        raise NotImplementedError
