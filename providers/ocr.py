"""Local OCR for recruitment screenshots.

The engine is imported lazily so the plugin still loads when the optional
dependency is missing; callers get :class:`OCRError` instead of an import
failure.
"""

from __future__ import annotations

import asyncio
from typing import Any


class OCRError(RuntimeError):
    """Raised when the local OCR engine is unavailable or fails."""


_engine: Any | None = None


def _get_engine() -> Any:
    """Create the OCR engine on first use.

    Returns:
        A ``RapidOCR`` instance.

    Raises:
        OCRError: If ``rapidocr-onnxruntime`` is not installed.
    """
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - depends on the用户环境
            raise OCRError("本地 OCR 未安装：pip install rapidocr-onnxruntime") from exc
        _engine = RapidOCR()
    return _engine


def _recognize(path: str) -> str:
    """Run OCR over one image file.

    Args:
        path: Absolute path of the screenshot.

    Returns:
        All recognised text joined into one line.

    Raises:
        OCRError: If the engine is unavailable or returns nothing usable.
    """
    engine = _get_engine()
    result, _ = engine(path)
    if not result:
        raise OCRError("本地 OCR 没有识别出文字")
    return "".join(str(line[1]) for line in result if len(line) > 1)


async def recognize(path: str) -> str:
    """Run the blocking OCR engine in a worker thread.

    Args:
        path: Absolute path of the screenshot.

    Returns:
        All recognised text joined into one line.

    Raises:
        OCRError: If the engine is unavailable or returns nothing usable.
    """
    try:
        return await asyncio.to_thread(_recognize, path)
    except OCRError:
        raise
    except Exception as exc:  # noqa: BLE001 - engine failures vary widely
        raise OCRError(f"本地 OCR 失败：{type(exc).__name__}: {exc}") from exc
