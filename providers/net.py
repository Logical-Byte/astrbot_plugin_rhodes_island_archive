"""Shared upstream HTTP client with memory and disk caching.

The plugin talks to two public mirrors. Both requests and responses are
untrusted, so the client validates the host of every URL, caps the size of a
single download, caches payloads in memory for a configurable time-to-live and
keeps a copy on disk so a restart does not download the same multi-megabyte
table again.

The Endfield mirror answers ``If-None-Match`` with a full body instead of a 304,
so the disk copy is revalidated with a ``HEAD`` request and its validator
(``ETag`` or ``Last-Modified``) compared locally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_USER_AGENT = "astrbot-plugin-rhodes-island-archive/0.1"

#: Payloads above this size are served from disk instead of memory.
_MEMORY_LIMIT = 4 * 1024 * 1024


class UpstreamError(RuntimeError):
    """Raised when an upstream request fails or breaks the download policy."""


class NetClient:
    """Host-restricted HTTP client with memory and disk caching.

    Attributes:
        hosts: Hostnames the client is allowed to contact.
        timeout: Per-request timeout in seconds.
        cache_ttl: Freshness window in seconds; zero always revalidates.
        max_bytes: Maximum size of a single downloaded payload.
        user_agent: User-Agent header sent upstream.
        cache_dir: Optional directory holding the on-disk copies.
    """

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        timeout: float = 20.0,
        cache_ttl: float = 600.0,
        max_bytes: int = 32 * 1024 * 1024,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_dir: Path | None = None,
        logger: Any | None = None,
    ) -> None:
        """Store the download policy; no connection is opened yet.

        Args:
            hosts: Hostnames that may be contacted.
            timeout: Per-request timeout in seconds.
            cache_ttl: Freshness window in seconds; zero always revalidates.
            max_bytes: Maximum size of a single downloaded payload.
            user_agent: User-Agent header sent upstream.
            cache_dir: Directory used for the on-disk cache; ``None`` keeps
                every payload in memory only.
            logger: Optional logger receiving one line per download.
        """
        self.hosts = frozenset(hosts)
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.logger = logger
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        """Create the shared client on first use.

        Returns:
            The active client.
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    def _debug(self, message: str, *args: Any) -> None:
        """Log one diagnostic line when a logger was supplied.

        Args:
            message: Format string.
            *args: Values for the format string.
        """
        if self.logger is not None:
            self.logger.debug(message, *args)

    def _check(self, url: str) -> None:
        """Reject URLs outside the allowed host set.

        Args:
            url: URL about to be fetched.

        Raises:
            UpstreamError: If the URL is not an HTTPS URL on an allowed host.
        """
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.hosts:
            raise UpstreamError(f"Refusing to fetch untrusted URL: {url}")

    def _disk_files(self, url: str) -> tuple[Path, Path]:
        """Locate the payload and metadata files of one URL.

        Args:
            url: URL whose cached copy is requested.

        Returns:
            The payload path and its metadata path.
        """
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
        assert self.cache_dir is not None
        return self.cache_dir / f"{key}.bin", self.cache_dir / f"{key}.json"

    def _read_disk(self, url: str) -> tuple[bytes, dict[str, Any]] | None:
        """Load a stored payload and its metadata.

        Args:
            url: URL whose cached copy is requested.

        Returns:
            The stored body with its metadata, or ``None`` when it is missing
            or unusable.
        """
        if self.cache_dir is None:
            return None
        body_path, meta_path = self._disk_files(url)
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            body = body_path.read_bytes()
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or meta.get("url") != url:
            return None
        if not body or len(body) > self.max_bytes:
            return None
        return body, meta

    def _write_disk(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        """Persist a payload together with its validators.

        Args:
            url: URL the payload was read from.
            body: Response body.
            headers: Response headers carrying ``ETag`` / ``Last-Modified``.
        """
        if self.cache_dir is None:
            return
        body_path, meta_path = self._disk_files(url)
        meta = {
            "url": url,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "stored_at": time.time(),
        }
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = meta_path.with_suffix(".tmp")
            tmp_path.write_bytes(body)
            os.replace(tmp_path, body_path)
            tmp_path.write_text(json.dumps(meta), "utf-8")
            os.replace(tmp_path, meta_path)
        except OSError:
            # A read-only or full disk only costs a later download.
            pass

    @staticmethod
    def _unchanged(headers: Mapping[str, str], meta: Mapping[str, Any]) -> bool:
        """Compare a revalidation response with the stored validators.

        Args:
            headers: Headers of the ``HEAD`` response.
            meta: Metadata of the stored copy.

        Returns:
            ``True`` when the upstream copy is provably the stored one.
        """
        current_etag = str(headers.get("etag") or "").removeprefix("W/")
        stored_etag = str(meta.get("etag") or "").removeprefix("W/")
        if current_etag and stored_etag and current_etag == stored_etag:
            return True
        current_modified = str(headers.get("last-modified") or "")
        stored_modified = str(meta.get("last_modified") or "")
        return bool(current_modified) and current_modified == stored_modified

    def _remember(self, url: str, body: bytes) -> bytes:
        """Cache a payload in memory for the configured window.

        Payloads above :data:`_MEMORY_LIMIT` stay on disk only: a portrait is
        sixteen megabytes and keeping several of them resident buys nothing,
        because the disk read is already faster than one round trip.

        Args:
            url: URL the payload was read from.
            body: Response body.

        Returns:
            The payload, unchanged.
        """
        if self.cache_ttl > 0 and len(body) <= _MEMORY_LIMIT:
            self._cache[url] = (time.monotonic() + self.cache_ttl, body)
        return body

    async def get_bytes(self, url: str) -> bytes:
        """Download a URL, reusing memory or disk copies when possible.

        Args:
            url: Absolute HTTPS URL on an allowed host.

        Returns:
            The response body.

        Raises:
            UpstreamError: If the host is not allowed, the request fails, the
                response is not HTTP 200, or the body exceeds ``max_bytes``.
        """
        self._check(url)
        cached = self._cache.get(url)
        if cached and cached[0] > time.monotonic():
            self._debug("memory hit %s", url)
            return cached[1]

        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._cache.get(url)
            if cached and cached[0] > time.monotonic():
                return cached[1]

            stored = self._read_disk(url)
            if stored is not None:
                body, meta = stored
                stored_at = meta.get("stored_at")
                fresh = (
                    self.cache_ttl > 0
                    and isinstance(stored_at, int | float)
                    and stored_at + self.cache_ttl > time.time()
                )
                if fresh:
                    self._debug("disk hit %s (%d bytes)", url, len(body))
                    return self._remember(url, body)

                client = await self._get_client()
                try:
                    head = await client.head(url)
                except httpx.HTTPError:
                    head = None
                if (
                    head is not None
                    and head.status_code == 200
                    and self._unchanged(head.headers, meta)
                ):
                    self._debug("revalidated %s (%d bytes)", url, len(body))
                    self._write_disk(url, body, head.headers)
                    return self._remember(url, body)

            client = await self._get_client()
            started = time.monotonic()
            self._debug("GET %s", url)
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                self._debug(
                    "GET %s failed after %.2fs: %s",
                    url,
                    time.monotonic() - started,
                    exc,
                )
                raise UpstreamError(f"Request failed: {url}") from exc
            self._debug(
                "GET %s -> %d in %.2fs (%d bytes)",
                url,
                response.status_code,
                time.monotonic() - started,
                len(response.content),
            )

            if response.status_code != 200:
                raise UpstreamError(
                    f"Upstream returned HTTP {response.status_code}: {url}"
                )

            body = response.content
            if len(body) > self.max_bytes:
                raise UpstreamError(f"Payload exceeds {self.max_bytes} bytes: {url}")
            self._write_disk(url, body, response.headers)
            return self._remember(url, body)

    async def get_json(self, url: str) -> Any:
        """Download and decode a JSON document.

        Args:
            url: Absolute HTTPS URL on an allowed host.

        Returns:
            The decoded JSON value.

        Raises:
            UpstreamError: If the request fails or the body is not JSON.
        """
        body = await self.get_bytes(url)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"Upstream returned invalid JSON: {url}") from exc

    async def aclose(self) -> None:
        """Close the underlying client and drop in-memory payloads."""
        client, self._client = self._client, None
        self._cache.clear()
        self._locks.clear()
        if client is not None and not client.is_closed:
            await client.aclose()
