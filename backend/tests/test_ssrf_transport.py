"""DNS-rebinding regressions: no network, real HTTPX/HTTPCore above fake TCP/TLS.

TLS tests verify the hostname and verification context passed to start_tls; they
do not perform a real TLS handshake or prove OS routing/certificate validation.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import ssl

import httpcore
import httpx
import pytest

from app.tools import artifacts, fetch_url, ssrf
from app.tools.base import ToolContext

IPV4 = "93.184.216.34"
IPV6 = "2606:4700:4700::1111"


def answer(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Windows event loops require a local IPC socketpair before sockets are blocked.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def forbidden(*args, **kwargs):
        pytest.fail("Live DNS/socket connection is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    try:
        yield loop
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        asyncio.set_event_loop(None)


def download(kind, url):
    if kind == "sync":
        return artifacts.resolve_image_bytes(url)
    return asyncio.get_event_loop().run_until_complete(
        fetch_url.FetchUrlTool().run({"url": url}, ToolContext(user_id="offline"))
    )


@pytest.fixture
def base_http(monkeypatch):
    """Observe exactly what the numeric-destination transport gives to HTTPX."""
    requests = []

    def send(self, request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

    async def async_send(self, request):
        return send(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_send)
    return requests


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("url,address,port", [
    ("http://public.example/a%2Fb?x=%2F", IPV4, 80),
    ("https://public.example:443/a", IPV4, 443),
    ("http://public.example:8080/a", IPV6, 8080),
    ("https://user:secret@public.example:8443/a", IPV4, 8443),
    ("https://bücher.example/a", IPV6, 443),
    (f"http://{IPV4}:80/a", IPV4, 80),
    (f"https://{IPV4}:8443/a", IPV4, 8443),
    (f"http://[{IPV6}]/a", IPV6, 80),
    (f"https://[{IPV6}]:443/a", IPV6, 443),
    (f"https://[{IPV6}]:8443/a", IPV6, 8443),
])
def test_transport_pins_once_preserving_host_sni_and_outer_request(
    monkeypatch, offline, base_http, kind, url, address, port,
):
    lookups = []

    def resolve(host, service, **kwargs):
        lookups.append((host, service))
        # A vulnerable second hostname lookup would now return a private address.
        return [answer(address if len(lookups) == 1 else "127.0.0.1")]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    original = httpx.Request("POST", url, content=b"payload",
                             headers={"Host": "wrong.example", "X-Custom": "kept"},
                             extensions={"sni_hostname": "wrong.example",
                                         "timeout": {"connect": 7}, "trace": object()})
    original_headers = original.headers.copy()
    original_extensions = original.extensions.copy()
    if original.url.userinfo:
        # HTTPX itself adds URL-based Basic auth before entering the transport.
        credentials = f"{original.url.username}:{original.url.password}".encode()
        original_headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()

    async def send_async():
        async with httpx.AsyncClient(transport=ssrf.PublicAsyncHTTPTransport(),
                                     trust_env=False) as client:
            return await client.send(original)

    if kind == "sync":
        with httpx.Client(transport=ssrf.PublicHTTPTransport(), trust_env=False) as client:
            response = client.send(original)
    else:
        response = offline.run_until_complete(send_async())

    assert response.content == b"ok"
    assert response.request is original
    assert response.url == httpx.URL(url)
    assert len(base_http) == 1
    pinned = base_http[0]
    assert pinned is not original
    assert pinned.url.host == address
    assert pinned.url.port == httpx.URL(url).port
    assert pinned.url.raw_path == original.url.raw_path
    assert pinned.method == original.method
    assert pinned.stream is original.stream
    assert pinned.headers["host"] == original.url.netloc.decode("ascii")
    assert pinned.headers["x-custom"] == "kept"
    assert pinned.extensions["sni_hostname"] == original.url.raw_host.decode("ascii")
    assert pinned.extensions["timeout"] == {"connect": 7}
    assert pinned.extensions["trace"] is original.extensions["trace"]
    assert original.headers == original_headers
    assert original.extensions == original_extensions
    assert lookups == [(original.url.raw_host.decode("ascii"), port)]


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("addresses", [[IPV4, IPV6], [IPV6, IPV4]])
def test_multiple_public_answers_select_one_numeric_destination(
    monkeypatch, base_http, kind, addresses,
):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [answer(ip) for ip in addresses])
    result = download(kind, "https://public.example/image")
    assert result == b"ok" if kind == "sync" else result.content == "ok"
    assert [request.url.host for request in base_http] == addresses[:1]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_scoped_ipv6_url_is_rejected_without_dns(base_http, kind):
    result = download(kind, f"https://[{IPV6}%25eth0]/image")
    assert result is None if kind == "sync" else result.citations == []
    assert not base_http


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("addresses", [
    [], [IPV4, "10.0.0.1"], [IPV6, "::1"], [IPV4, "100.64.0.1"],
    [IPV4, "not-an-ip"], [IPV4, "::ffff:127.0.0.1"], [IPV6, IPV6 + "%eth0"],
])
def test_transport_rejects_any_bad_dns_answer_before_dispatch(
    monkeypatch, base_http, kind, addresses,
):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [answer(ip) for ip in addresses])
    result = download(kind, "https://public.example/image")
    assert result is None if kind == "sync" else result.citations == []
    assert not base_http


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_separate_preflight_does_not_authorize_rebound_transport(monkeypatch, base_http, kind):
    answers = iter([[answer(IPV4)], [answer("169.254.169.254")]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: next(answers))
    assert ssrf.is_safe_url("https://public.example/image") == (True, "ok")
    result = download(kind, "https://public.example/image")
    assert result is None if kind == "sync" else result.citations == []
    assert not base_http


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_fetch_clients_and_transports_disable_environment_proxies(monkeypatch, base_http, kind):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:8888")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    seen = []
    client_type = httpx.Client if kind == "sync" else httpx.AsyncClient
    original_init = client_type.__init__

    def init(self, *args, **kwargs):
        assert kwargs["trust_env"] is False
        transport = kwargs["transport"]
        expected = ssrf.PublicHTTPTransport if kind == "sync" else ssrf.PublicAsyncHTTPTransport
        assert isinstance(transport, expected)
        seen.append(transport)
        original_init(self, *args, **kwargs)
        assert not self._mounts

    monkeypatch.setattr(client_type, "__init__", init)
    hosts = []

    def resolve(host, *args, **kwargs):
        hosts.append(host)
        return [answer(IPV4)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    result = download(kind, "https://public.example/image")
    assert result == b"ok" if kind == "sync" else result.content == "ok"
    assert len(seen) == len(base_http) == 1
    assert hosts == ["public.example"]


@pytest.fixture
def wire_http(monkeypatch):
    """Run the actual HTTPX -> HTTPCore -> HTTP/1 pipeline with fake TCP/TLS only."""
    connections = []
    tls = []
    writes = []
    closed = []

    def install(responses):
        responses = iter(responses)

        class Wire(httpcore.MockStream):
            def write(self, buffer, timeout=None):
                writes.append(buffer)

            def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                tls.append((server_hostname, ssl_context))
                return self

            def close(self):
                closed.append(self)
                super().close()

        class AsyncWire(httpcore.AsyncMockStream):
            async def write(self, buffer, timeout=None):
                writes.append(buffer)

            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                tls.append((server_hostname, ssl_context))
                return self

            async def aclose(self):
                closed.append(self)
                await super().aclose()

        def connect(self, host, port, **kwargs):
            # This is the actual destination httpcore would pass to its backend.
            assert ipaddress.ip_address(host).is_global
            stream = Wire([next(responses)])
            connections.append((host, port, stream))
            return stream

        async def async_connect(self, host, port, **kwargs):
            assert ipaddress.ip_address(host).is_global
            stream = AsyncWire([next(responses)])
            connections.append((host, port, stream))
            return stream

        monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
        monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", async_connect)
        return connections, tls, writes, closed

    return install


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("address", [IPV4, IPV6])
def test_real_httpcore_connects_only_to_pin_with_original_tls_identity(
    monkeypatch, wire_http, kind, address,
):
    hosts = []

    def resolve(host, *args, **kwargs):
        hosts.append(host)
        return [answer(address if len(hosts) == 1 else "10.0.0.1")]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    connections, tls, writes, closed = wire_http([
        b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 2\r\n\r\nok",
    ])
    result = download(kind, "https://public.example:8443/a%2Fb?x=%2F")
    assert result == b"ok" if kind == "sync" else result.content == "ok"
    assert hosts == ["public.example"]
    assert [(host, port) for host, port, _ in connections] == [(address, 8443)]
    assert [host for host, _ in tls] == ["public.example"]
    assert all(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
               for _, context in tls)
    sent = b"".join(writes)
    assert b"GET /a%2Fb?x=%2F HTTP/1.1\r\n" in sent
    assert b"Host: public.example:8443\r\n" in sent
    assert closed == [stream for _, _, stream in connections]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_shared_ip_redirects_get_separate_tls_connections_and_scoped_auth(
    monkeypatch, wire_http, kind,
):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [answer(IPV4)])
    connections, tls, writes, closed = wire_http([
        b"HTTP/1.1 302 Found\r\nLocation: next\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 302 Found\r\nLocation: https://other.example/final\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 2\r\n\r\nok",
    ])
    result = download(kind, "https://user:secret@public.example/dir/start")
    assert result == b"ok" if kind == "sync" else result.content == "ok"
    assert [(host, port) for host, port, _ in connections] == [(IPV4, 443)] * 3
    assert [host for host, _ in tls] == ["public.example", "public.example", "other.example"]
    sent = [part for part in writes if part.startswith(b"GET ")]
    assert len(sent) == 3
    assert b"GET /dir/next " in sent[1]
    assert b"Host: other.example\r\n" in sent[2]
    assert [b"Authorization: Basic " in part for part in sent] == [True, True, False]
    assert closed == [stream for _, _, stream in connections]


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("outcome", ["truncated", "read-error", "bad-status", "bad-dns", "cancelled"])
def test_pool_and_stream_cleanup_on_early_exit(monkeypatch, wire_http, kind, outcome):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [answer(IPV4)])
    monkeypatch.setattr(artifacts, "MAX_IMAGE_BYTES", 2)
    monkeypatch.setattr(fetch_url, "MAX_BYTES", 2)
    response = {
        "truncated": b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 5\r\n\r\nhello",
        "read-error": b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 5\r\n\r\nh",
        "bad-status": b"HTTP/1.1 500 Error\r\nContent-Length: 5\r\n\r\nhello",
        "bad-dns": b"HTTP/1.1 302 Found\r\nLocation: https://private.example/\r\nContent-Length: 0\r\n\r\n",
        "cancelled": b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 5\r\n\r\nhello",
    }[outcome]
    if outcome == "bad-dns":
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw:
                            [answer("127.0.0.1" if host == "private.example" else IPV4)])
    connections, _, _, closed = wire_http([response])
    pools = []
    pool_type = httpcore.ConnectionPool if kind == "sync" else httpcore.AsyncConnectionPool
    method = "close" if kind == "sync" else "aclose"
    original_close = getattr(pool_type, method)

    def close(self):
        pools.append(self)
        original_close(self)

    async def aclose(self):
        pools.append(self)
        await original_close(self)

    monkeypatch.setattr(pool_type, method, close if kind == "sync" else aclose)
    if outcome == "cancelled":
        def read(self, max_bytes, timeout=None):
            raise asyncio.CancelledError()

        async def aread(self, max_bytes, timeout=None):
            raise asyncio.CancelledError()

        monkeypatch.setattr(httpcore.MockStream, "read", read)
        monkeypatch.setattr(httpcore.AsyncMockStream, "read", aread)
        with pytest.raises(asyncio.CancelledError):
            download(kind, "https://public.example/image")
    else:
        result = download(kind, "https://public.example/image")
        if kind == "sync":
            assert result is None
        elif outcome == "truncated":
            assert result.content == "he"
        else:
            assert result.citations == []
    assert len(pools) == 1
    assert pools[0].connections == []
    assert closed == [stream for _, _, stream in connections]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_transport_does_not_swallow_programmer_callback_errors(monkeypatch, offline, kind):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [answer(IPV4)])

    def send(self, request):
        raise RuntimeError("broken callback")

    async def async_send(self, request):
        return send(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_send)

    async def run():
        async with ssrf.PublicAsyncHTTPTransport() as transport:
            await transport.handle_async_request(httpx.Request("GET", "https://public.example/"))

    with pytest.raises(RuntimeError, match="broken callback"):
        if kind == "sync":
            with ssrf.PublicHTTPTransport() as transport:
                transport.handle_request(httpx.Request("GET", "https://public.example/"))
        else:
            offline.run_until_complete(run())


def test_page_fetch_does_not_swallow_programmer_errors(monkeypatch):
    def broken_pin(request):
        raise TypeError("broken callback")

    monkeypatch.setattr(ssrf, "_pin_request", broken_pin)
    with pytest.raises(TypeError, match="broken callback"):
        download("async", "https://public.example/")
