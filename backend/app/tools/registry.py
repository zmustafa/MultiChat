from __future__ import annotations

from .base import Tool
from .calculator import CalculatorTool
from .current_date import CurrentDateTool
from .docx_generate import DocxGenerateTool
from .fetch_url import FetchUrlTool
from .image_generate import ImageGenerateTool
from .mcp_tool import workiq_tools
from .pdf_generate import PdfGenerateTool
from .pptx_generate import PptxGenerateTool
from .read_document import ReadDocumentTool
from .web_search import WebSearchTool
from .xlsx_generate import XlsxGenerateTool

_ALL: dict[str, Tool] = {
    "web_search": WebSearchTool(),
    "fetch_url": FetchUrlTool(),
    "calculator": CalculatorTool(),
    "current_date": CurrentDateTool(),
    "read_document": ReadDocumentTool(),
    "generate_image": ImageGenerateTool(),
    "generate_pptx": PptxGenerateTool(),
    "generate_docx": DocxGenerateTool(),
    "generate_xlsx": XlsxGenerateTool(),
    "generate_pdf": PdfGenerateTool(),
}


def all_tools() -> dict[str, Tool]:
    return {**_ALL, **workiq_tools()}


def get_tool(name: str) -> Tool | None:
    if name in _ALL:
        return _ALL[name]
    return workiq_tools().get(name)


def resolve_enabled_tools(tool_config: dict | None) -> list[Tool]:
    """Return the Tool instances enabled for a session.

    Built-in tools follow the session's `enabled` selection; Work IQ tools (when the
    integration is connected) are always included so M365 access is available in every
    chat that has tools turned on.
    """
    extra = list(workiq_tools().values())
    if tool_config is None:
        # No config at all — default to all tools (caller only invokes this when
        # tools are enabled for the session).
        return list(_ALL.values()) + extra
    enabled = tool_config.get("enabled")
    if enabled is None:
        # default: all tools when tools_enabled is true
        return list(_ALL.values()) + extra
    return [_ALL[name] for name in enabled if name in _ALL] + extra
