"""Local HTML rendering with a headless Chromium driven by Playwright.

AstrBot's built-in renderer only speaks to a remote text-to-image service, and
its local strategy draws text with Pillow instead of laying out HTML. The
character card needs real CSS layout and a specific font, so this module starts
a local headless browser, writes the document next to the font it references and
screenshots the result.

The browser is optional: when Playwright or its Chromium build is missing the
caller falls back to the remote renderer.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

#: Font file the template pins with ``!important``.
FONT_FILE = "HarmonyOS_Sans_SC.ttf"

#: Width of the card in CSS pixels.
VIEWPORT_WIDTH = 1400

#: Height used while the page is laid out; ``full_page`` overrides it.
VIEWPORT_HEIGHT = 900


class LocalRenderError(RuntimeError):
    """Raised when the local browser cannot be started or cannot render."""


def browsers_dir(data_dir: Path) -> Path:
    """Return the directory this plugin installs Chromium into.

    Args:
        data_dir: The plugin data directory.

    Returns:
        The ``browsers`` directory used by ``playwright install``.
    """
    return data_dir / "browsers"


class LocalRenderer:
    """Render HTML to PNG in a local headless Chromium.

    Attributes:
        assets_dir: Directory holding the template font.
        cache_dir: Directory holding the generated documents and font copy.
        timeout: Per-render timeout in seconds.
    """

    name = "local"

    def __init__(
        self,
        assets_dir: Path,
        cache_dir: Path,
        *,
        timeout: float = 30.0,
        browsers: Path | None = None,
        logger: Any | None = None,
    ) -> None:
        """Store the render configuration; no browser is started yet.

        Args:
            assets_dir: Directory holding the template font.
            cache_dir: Directory used for the temporary document and font copy.
            timeout: Per-render timeout in seconds.
            browsers: Directory holding a plugin-local Chromium installation.
            logger: Logger used for diagnostics.
        """
        self.assets_dir = assets_dir
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.browsers = browsers
        self.logger = logger
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    @property
    def font_family(self) -> str | None:
        """Return the font file name when the bundled font is present.

        Returns:
            ``HarmonyOS_Sans_SC.ttf`` when shipped with the plugin, else
            ``None`` so the stylesheet keeps its fallback stack.
        """
        return FONT_FILE if (self.assets_dir / FONT_FILE).is_file() else None

    async def start(self) -> None:
        """Launch the browser once and reuse it for every render.

        Raises:
            LocalRenderError: If Playwright or the Chromium build is missing.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LocalRenderError("playwright is not installed") from exc

        if self.browsers is not None and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            # A plugin-local installation is used when the user installed the
            # browser through this plugin instead of the global cache.
            if self.browsers.is_dir() and any(self.browsers.glob("chromium-*")):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers)

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            await self.close()
            raise LocalRenderError(f"cannot start Chromium: {exc}") from exc

        if self.logger is not None:
            self.logger.info("local renderer ready: %s", self._browser.version)

    async def render(
        self,
        html: str,
        *,
        width: int = VIEWPORT_WIDTH,
        height: int = VIEWPORT_HEIGHT,
    ) -> bytes:
        """Screenshot one HTML document.

        Args:
            html: Complete HTML document.
            width: Viewport width in CSS pixels.
            height: Viewport height in CSS pixels.

        Returns:
            The rendered PNG bytes.

        Raises:
            LocalRenderError: If the browser is not running or the render fails
                or times out.
        """
        if self._browser is None:
            raise LocalRenderError("browser is not running")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.font_family and not (self.cache_dir / FONT_FILE).is_file():
            # The document references the font by a relative URL, so both files
            # have to live in the same directory.
            shutil.copyfile(self.assets_dir / FONT_FILE, self.cache_dir / FONT_FILE)

        document = self.cache_dir / f"card_{uuid.uuid4().hex}.html"
        document.write_text(html, encoding="utf-8")

        async with self._lock:
            page = await self._browser.new_page(
                viewport={"width": width, "height": height},
            )
            try:
                await page.goto(
                    document.as_uri(), wait_until="load", timeout=self.timeout * 1000
                )
                await page.wait_for_timeout(50)
                shot = await page.screenshot(full_page=True, type="png")
            except Exception as exc:
                raise LocalRenderError(f"render failed: {exc}") from exc
            finally:
                await page.close()
                document.unlink(missing_ok=True)
        return shot

    async def close(self) -> None:
        """Close the browser and stop Playwright."""
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
