"""Reconciling the download links models produce with the files that were really created.

A ``generate_*`` tool returns the real ``/api/files/<id>`` URL, but models routinely omit
it, or invent a plausible-looking link in its place. Everything needed to detect that, to
repair it, and to log the resulting files into the session's library lives here — it is
self-contained string/regex work, and keeping it out of the streaming module makes it
directly testable.
"""
from __future__ import annotations

import os
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .models import GeneratedFile
from .models import Session as ChatSession
from .providers.base import ChatMessage

GENERATED_LINK_RE = re.compile(
    r"/api/files/([0-9a-f]{32}\.(pptx|docx|xlsx|pdf))\?name=([^)\s]+)"
)
GEN_MIME = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
FILE_GENERATOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("generate_pptx", re.compile(r"\b(power\s*point|pptx|slide deck|presentation)\b", re.I)),
    ("generate_docx", re.compile(r"\b(word document|word doc|docx)\b", re.I)),
    ("generate_xlsx", re.compile(r"\b(excel|xlsx|spreadsheet)\b", re.I)),
    ("generate_pdf", re.compile(r"\bpdf\b", re.I)),
)
GENERATOR_EXTENSIONS = {
    "generate_pptx": "pptx",
    "generate_docx": "docx",
    "generate_xlsx": "xlsx",
    "generate_pdf": "pdf",
}

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_FILE_LINK_RE = re.compile(r"\[[^\]]+\]\((/api/files/[^)\s]+)\)")


def message_text(message: ChatMessage) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def requested_file_generator(message: ChatMessage) -> str | None:
    """Which generate_* tool this message is asking for, if any."""
    text = message_text(message)
    for generator, pattern in FILE_GENERATOR_PATTERNS:
        if pattern.search(text):
            return generator
    return None


def tool_created_requested_file(
    tool_call_rows: list[tuple[str, dict, Any]], generator: str | None
) -> bool:
    if not generator:
        return True
    suffix = f".{GENERATOR_EXTENSIONS[generator]}"
    for name, _args, result in tool_call_rows:
        if name != generator:
            continue
        text = str((result or {}).get("result") or "")
        if any(
            match.group(1).lower().endswith(suffix)
            for match in _FILE_LINK_RE.finditer(text)
        ):
            return True
    return False


def reconcile_generated_links(
    text: str, real_links: list[tuple[str, str]]
) -> tuple[str, list[tuple[str, str]]]:
    """Replace unverified file links and return real links omitted by the model."""
    real_urls = [url for _label, url in real_links]
    unused_urls = list(real_urls)

    def _take_replacement(url: str) -> str | None:
        path = url.split("?", 1)[0]
        extension_match = re.search(r"\.([a-z0-9]+)$", path, re.I)
        if extension_match:
            extension = extension_match.group(1).lower()
            for candidate in unused_urls:
                if candidate.split("?", 1)[0].lower().endswith(f".{extension}"):
                    unused_urls.remove(candidate)
                    return candidate
            return None
        if unused_urls:
            return unused_urls.pop(0)
        return None

    def _fix_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if url in real_urls:
            if url in unused_urls:
                unused_urls.remove(url)
            return match.group(0)
        is_file_link = url.startswith("/api/files/")
        looks_like_download = "download" in label.lower() or "\U0001f4e5" in label
        if is_file_link or looks_like_download:
            replacement = _take_replacement(url)
            return f"[{label}]({replacement})" if replacement else label
        return match.group(0)

    fixed = _MARKDOWN_LINK_RE.sub(_fix_link, text)
    missing = [(label, url) for label, url in real_links if url not in fixed]
    return fixed, missing


def collect_real_links(
    tool_call_rows: list[tuple[str, dict, Any]],
) -> list[tuple[str, str]]:
    """Every distinct download link the tools actually produced, in call order."""
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _name, _args, result in tool_call_rows:
        text = (result or {}).get("result") or ""
        for match in _MARKDOWN_LINK_RE.finditer(text):
            label, url = match.group(1), match.group(2)
            if url.startswith("/api/files/") and url not in seen:
                seen.add(url)
                links.append((label, url))
    return links


def record_generated_files(db: DbSession, session: ChatSession, text: str) -> None:
    """Scan a tool result for generated-file download links and log them so they
    appear in the session's Files library."""
    found = GENERATED_LINK_RE.findall(text or "")
    if not found:
        return
    names = {stored_name for stored_name, _ext, _dl in found}
    existing = set(
        db.scalars(
            select(GeneratedFile.stored_name).where(GeneratedFile.stored_name.in_(names))
        ).all()
    )
    added = False
    for stored_name, ext, download_name in found:
        if stored_name in existing:
            continue
        existing.add(stored_name)
        path = os.path.join(settings.UPLOAD_DIR, "generated", stored_name)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        db.add(
            GeneratedFile(
                user_id=session.user_id,
                session_id=session.id,
                stored_name=stored_name,
                download_name=download_name,
                mime_type=GEN_MIME.get(ext, "application/octet-stream"),
                size_bytes=size,
                kind=ext,
            )
        )
        added = True
    if added:
        db.commit()
