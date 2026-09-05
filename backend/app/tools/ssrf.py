from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx

MAX_REDIRECTS = 3


class UnsafeURL(ValueError):
    """The URL or one of its resolved addresses is not a public HTTP target."""


def _resolve_public_url(url: str) -> tuple[httpx.URL, str]:
    """Validate every answer before selecting a numeric connection destination."""
    if not isinstance(url, str) or not url:
        raise UnsafeURL("Invalid URL")
    try:
        parsed = httpx.URL(url)
        # Validate the actual transport host, never userinfo that merely looks
        # like a host. Public endpoints using URL-based Basic auth remain usable.
        host = parsed.raw_host.decode("ascii")
        port = parsed.port
    except (httpx.InvalidURL, ValueError, TypeError):
        raise UnsafeURL("Invalid URL") from None
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("Only http/https URLs are allowed")
    if not host:
        raise UnsafeURL("Missing host")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeURL("Invalid port")
    if "%" in host:
        raise UnsafeURL("Scoped addresses are not allowed")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (OSError, ValueError, TypeError):
        raise UnsafeURL("Could not resolve host") from None
    if not infos:
        raise UnsafeURL("Could not resolve host")
    addresses = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, TypeError, IndexError):
            raise UnsafeURL("Invalid resolved address") from None
        # is_global also excludes shared-address space (e.g. 100.64.0.0/10).
        if not ip.is_global or ip.is_multicast or ip.is_reserved or "%" in str(ip):
            raise UnsafeURL("Blocked non-public address")
        addresses.append(str(ip))
    return parsed, addresses[0]


def is_safe_url(url: str) -> tuple[bool, str]:
    """Compatibility preflight only; use the transports below to pin connections.

    A successful check alone does not protect a subsequent ordinary HTTP client
    from DNS rebinding. The fetch tools resolve and pin inside their transport.
    """
    try:
        _resolve_public_url(url)
    except UnsafeURL as exc:
        return False, str(exc)
    return True, "ok"


def _pin_request(request: httpx.Request) -> httpx.Request:
    url, address = _resolve_public_url(str(request.url))
    headers = request.headers.copy()
    headers["Host"] = url.netloc.decode("ascii")
    # httpx forwards these to httpcore. In httpcore 1.x sni_hostname is passed
    # as start_tls(server_hostname=...), controlling BOTH SNI and cert checking.
    extensions = {**request.extensions, "sni_hostname": url.raw_host.decode("ascii")}
    # Never mutate the outer request: redirects, auth, cookies, response.url and
    # citations must keep the original origin. Only TCP sees the validated IP.
    return httpx.Request(request.method, url.copy_with(host=address), headers=headers,
                         stream=request.stream, extensions=extensions)


class PublicHTTPTransport(httpx.HTTPTransport):
    """Direct HTTP/1 transport with a validated numeric destination on every hop."""

    def __init__(self) -> None:
        # IP-based pool keys could merge different TLS hostnames on the same IP.
        # Disable idle reuse AND HTTP/2 multiplexing rather than maintaining extra
        # per-origin pools. The client owns and closes this one pool as usual.
        super().__init__(trust_env=False, http2=False,
                         limits=httpx.Limits(max_keepalive_connections=0))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return super().handle_request(_pin_request(request))


class PublicAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Async counterpart; no global resolver changes or unmanaged child pools."""

    def __init__(self) -> None:
        super().__init__(trust_env=False, http2=False,
                         limits=httpx.Limits(max_keepalive_connections=0))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pinned = await asyncio.to_thread(_pin_request, request)
        return await super().handle_async_request(pinned)
