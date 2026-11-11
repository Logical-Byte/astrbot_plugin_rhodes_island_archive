"""MCP gateway and OCR helpers used by the plugin."""

from .mcp import MCPError, MCPGateway
from .ocr import OCRError, recognize

__all__ = ["MCPError", "MCPGateway", "OCRError", "recognize"]
