from __future__ import annotations

import os
import re
import uuid

from ..config import settings
from .base import ToolResult

# Subdirectory (under UPLOAD_DIR) where generated downloadable files are written.
GENERATED_SUBDIR = "generated"


def generated_dir() -> str:
    path = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def safe_download_name(title: str, ext: str, fallback: str = "document") -> str:
    base = re.sub(r"[^A-Za-z0-9 _-]+", "", title or "").strip().replace(" ", "-")
    base = re.sub(r"-{2,}", "-", base).strip("-")
    if not base:
        base = fallback
    return f"{base[:60]}.{ext}"


def new_stored_name(ext: str) -> str:
    return f"{uuid.uuid4().hex}.{ext}"


def download_result(stored_name: str, download_name: str, message: str) -> ToolResult:
    url = f"/api/files/{stored_name}?name={download_name}"
    return ToolResult(
        content=f"{message} [\U0001F4E5 Download {download_name}]({url})",
        citations=None,
    )


def resolve_image_bytes(ref: str) -> bytes | None:
    """Resolve an image reference to raw bytes for embedding in a document.

    Supports data: URIs, http(s) URLs, and local generated files served at
    /api/files/<stored-name>. Returns None if it cannot be resolved.
    """
    import base64 as _b64

    if not ref or not isinstance(ref, str):
        return None
    ref = ref.strip()
    try:
        if ref.startswith("data:"):
            _, _, b64 = ref.partition(",")
            return _b64.b64decode(b64)
        if "/api/files/" in ref:
            stored = ref.split("/api/files/", 1)[1].split("?", 1)[0]
            if re.fullmatch(r"[0-9A-Za-z._-]+", stored):
                path = os.path.join(generated_dir(), stored)
                if os.path.exists(path):
                    with open(path, "rb") as fh:
                        return fh.read()
            return None
        if ref.startswith("http://") or ref.startswith("https://"):
            import httpx

            resp = httpx.get(ref, timeout=20, follow_redirects=True)
            if resp.status_code < 400 and resp.headers.get("content-type", "").startswith(
                "image/"
            ):
                return resp.content
            return None
    except Exception:  # noqa: BLE001
        return None
    return None
