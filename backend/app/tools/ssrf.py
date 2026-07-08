from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only http/https and block private/loopback/link-local hosts."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False, "Invalid URL"
    if parsed.scheme not in ("http", "https"):
        return False, "Only http/https URLs are allowed"
    host = parsed.hostname
    if not host:
        return False, "Missing host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Could not resolve host"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, "Blocked non-public address"
    return True, "ok"
