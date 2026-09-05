from __future__ import annotations

import os
import re
import uuid
from contextlib import nullcontext
from urllib.parse import unquote_to_bytes, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import settings
from .base import ToolResult
from .ssrf import MAX_REDIRECTS, PublicHTTPTransport

# Subdirectory (under UPLOAD_DIR) where generated downloadable files are written.
GENERATED_SUBDIR = "generated"
# Use the existing image-upload policy (10 MiB by default) for every image source.
MAX_IMAGE_BYTES = settings.MAX_UPLOAD_BYTES


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


def resolve_image_bytes(
    ref: str, *, user_id: str | None = None, db: DbSession | None = None,
) -> bytes | None:
    """Resolve an image reference to raw bytes for embedding in a document.

    Supports base64/percent-encoded data: URIs, public http(s) URLs, and local
    generated /api/files/<stored-name> paths. Oversized/invalid images return None;
    image data is never truncated. Remote redirects are validated at every hop.

    Local images require an explicit user_id matching a GeneratedFile owner. Reuse
    the caller's db when supplied, otherwise open a short-lived SessionLocal. Legacy
    absolute links on FRONTEND_ORIGIN use the same check; other origins stay remote.
    Data/remote references need no user context and never query the ownership DB.
    """
    import base64 as _b64

    if not ref or not isinstance(ref, str):
        return None
    ref = ref.strip()
    try:
        if ref[:5].lower() == "data:":
            comma = ref.find(",")
            if comma < 0:
                return None
            is_base64 = ref[max(5, comma - 7):comma].lower() == ";base64"
            encoded_limit = 4 * ((MAX_IMAGE_BYTES + 2) // 3) if is_base64 else MAX_IMAGE_BYTES
            # Percent escapes can triple the encoded length. Check before copying
            # the payload or decoding, without restricting MIME types/parameters.
            if len(ref) - comma - 1 > 3 * encoded_limit:
                return None
            data = unquote_to_bytes(ref[comma + 1:])
            if is_base64:
                # Preserve ordinary line-wrapped base64, but reject invalid symbols.
                data = data.translate(None, b" \t\r\n")
                if len(data) > encoded_limit:
                    return None
                data = _b64.b64decode(data, validate=True)
            return data if len(data) <= MAX_IMAGE_BYTES else None
        # Only the configured app origin identifies an absolute URL as local. An
        # arbitrary public/private host with /api/files/ in its path must never be
        # mapped to the filesystem. Do not normalize malformed URLs into local refs.
        parts = urlsplit(ref)
        if parts.scheme in ("http", "https") and parts.path.startswith("/api/files/"):
            if any(ord(ch) <= 32 or ord(ch) == 127 for ch in ref):
                return None
            origin = urlsplit(settings.FRONTEND_ORIGIN)
            default_ports = {"http": 80, "https": 443}
            if (
                parts.hostname and parts.username is None and parts.password is None
                and origin.username is None and origin.password is None
                and (parts.scheme, parts.hostname,
                     parts.port if parts.port is not None else default_ports[parts.scheme])
                == (origin.scheme, origin.hostname,
                    origin.port if origin.port is not None else default_ports.get(origin.scheme))
            ):
                ref = parts.path
        if ref.startswith("/api/files/"):
            if not user_id:
                return None
            stored = ref[len("/api/files/"):].split("?", 1)[0].split("#", 1)[0]
            if re.fullmatch(r"[0-9A-Za-z._-]+", stored) and stored not in (".", ".."):
                # Import lazily: data/remote images do not need a database. Never
                # infer identity from an ambient request or the DB session itself.
                from ..db import SessionLocal
                from ..models import GeneratedFile

                with nullcontext(db) if db is not None else SessionLocal() as owner_db:
                    owned = owner_db.scalar(
                        select(GeneratedFile.id).where(
                            GeneratedFile.stored_name == stored,
                            GeneratedFile.user_id == user_id,
                        ).limit(1)
                    )
                if not owned:
                    return None
                root = os.path.realpath(generated_dir())
                path = os.path.realpath(os.path.join(root, stored))
                if os.path.commonpath((root, path)) == root and os.path.isfile(path):
                    with open(path, "rb") as fh:
                        data = fh.read(MAX_IMAGE_BYTES + 1)
                    return data if len(data) <= MAX_IMAGE_BYTES else None
            return None
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False,
                          transport=PublicHTTPTransport()) as client:
            for redirects in range(MAX_REDIRECTS + 1):
                with client.stream("GET", ref) as resp:
                    if resp.has_redirect_location:
                        if redirects == MAX_REDIRECTS:
                            return None
                        ref = str(resp.url.join(resp.headers["location"]))
                        continue
                    if not resp.is_success or not resp.headers.get("content-type", "").lower().startswith("image/"):
                        return None
                    data = bytearray()
                    for chunk in resp.iter_bytes():
                        if len(chunk) > MAX_IMAGE_BYTES - len(data):
                            return None
                        data.extend(chunk)
                    return bytes(data)
    except Exception:  # noqa: BLE001
        return None
    return None
