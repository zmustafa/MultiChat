"""Offline URL/download regressions; runnable with --noconftest (no app/DB startup)."""
from __future__ import annotations

import asyncio
import base64
import gzip
import socket
import zipfile
from collections.abc import Callable
from unittest.mock import MagicMock
from urllib.parse import quote_from_bytes
from xml.etree import ElementTree

import httpx
import pytest

from app.tools import artifacts, fetch_url, ssrf
from app.tools.base import ToolContext

PUBLIC_IP = "93.184.216.34"
LIMIT = 128
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII="
)


def dns_answer(address: str = PUBLIC_IP) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """All DNS is fake; fail loudly if anything escapes the mock HTTP transport."""
    # Windows creates a local socketpair for event-loop wakeups. Create that IPC
    # before blocking sockets, then run every download with networking blocked.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def no_network(*args, **kwargs):
        pytest.fail("Live networking is forbidden in URL download tests")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [dns_answer()])
    monkeypatch.setattr(artifacts.settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(artifacts, "MAX_IMAGE_BYTES", LIMIT, raising=False)
    monkeypatch.setattr(fetch_url, "MAX_BYTES", LIMIT)
    try:
        yield
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class Body(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, fail: bool = False):
        self.chunks = chunks
        self.fail = fail
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.fail:
            raise httpx.ReadError("mock broken stream")

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    def close(self):
        self.closed = True

    async def aclose(self):
        self.close()


@pytest.fixture
def mock_http(monkeypatch):
    # Intercept AFTER the real public transport has validated and pinned the
    # request, not at Client construction (which would bypass that protection).
    requests: list[httpx.Request] = []

    def install(handler: Callable[[httpx.Request], httpx.Response]):
        def dispatch(self, request: httpx.Request):
            requests.append(request)
            return handler(request)

        async def async_dispatch(self, request: httpx.Request):
            return dispatch(self, request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", dispatch)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_dispatch)
        return requests

    return install


def download(kind: str, ref):
    if kind == "image":
        return artifacts.resolve_image_bytes(ref)
    return asyncio.get_event_loop().run_until_complete(
        fetch_url.FetchUrlTool().run({"url": ref}, ToolContext(user_id="test"))
    )


def assert_rejected(kind: str, result):
    if kind == "image":
        assert result is None
    else:
        assert result.citations == []
        assert result.content.startswith(("Refused", "Fetch failed", "Fetch error"))


@pytest.fixture
def local_owner(monkeypatch):
    """A scoped ownership DB mock; never open a DB, including with --noconftest.

    The real ownership query must constrain both filename and identity. Only the
    known fixture image belongs to this user; all other combinations are denied.
    """
    monkeypatch.setattr(artifacts.settings, "DATABASE_URL", "sqlite://")
    from app import db as database

    session = MagicMock()
    session.__enter__.return_value = session

    def scalar(statement):
        params = statement.compile().params
        return "owned-row" if (
            params.get("stored_name_1") == "image.png" and params.get("user_id_1") == "test"
        ) else None

    session.scalar.side_effect = scalar
    monkeypatch.setattr(database, "SessionLocal", lambda: session)
    return "test"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/",
    "http://[::1]/", "http://[fd00::1]/", "http://[::ffff:127.0.0.1]/",
    "http://100.64.0.1/", "http://224.0.0.1/", "http://0.0.0.0/",
])
def test_guard_blocks_non_public_literals(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw: [dns_answer(host)])
    assert ssrf.is_safe_url(url)[0] is False


@pytest.mark.parametrize("url", [
    "", None, 42, [], "file:///tmp/image.png", "ftp://public.example/image.png",
    "https:///missing-host", "https://[bad]/", "https://public.example:bogus/",
    "https://public.example:65536/", "https://public.example:0/",
    "https://public.example\n.private/", "https://public.example\x00/",
    "https://\ud800.example/", "https://\uff0f.example/",
])
def test_guard_malformed_urls_fail_closed_without_raising(url):
    assert ssrf.is_safe_url(url)[0] is False


@pytest.mark.parametrize("answers", [
    [], [dns_answer("not-an-address")], [dns_answer(), dns_answer("10.0.0.1")],
    [dns_answer(), dns_answer("not-an-address")],
])
def test_guard_requires_nonempty_all_public_dns(monkeypatch, answers):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers)
    assert ssrf.is_safe_url("https://public.example/")[0] is False


@pytest.mark.parametrize("error", [socket.gaierror("no DNS"), OSError("DNS failed"),
                                        UnicodeError("bad IDNA"), ValueError("bad host")])
def test_guard_dns_errors_do_not_escape(monkeypatch, error):
    def broken_dns(*a, **kw):
        raise error

    monkeypatch.setattr(socket, "getaddrinfo", broken_dns)
    assert ssrf.is_safe_url("https://public.example/")[0] is False


@pytest.mark.parametrize("url,host", [
    ("https://public.example/path?token=a%2Fb", "public.example"),
    ("HTTP://public.example:8443/image.png", "public.example"),
    ("https://bücher.example/image.png", "xn--bcher-kva.example"),
    ("https://user:secret@public.example/image.png", "public.example"),
    ("https://127.0.0.1@public.example/image.png", "public.example"),
])
def test_guard_accepts_public_urls_and_uses_transport_hostname(monkeypatch, url, host):
    hosts = []

    def resolve(hostname, *a, **kw):
        hosts.append(hostname)
        return [dns_answer(), dns_answer("2606:4700:4700::1111")]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert ssrf.is_safe_url(url)[0] is True
    assert hosts == [host]


@pytest.mark.parametrize("kind", ["image", "page"])
@pytest.mark.parametrize("ref", [
    "http://127.0.0.1/x", "http://private.example/x", "https://public.example:99999/x",
    "https://public.example@127.0.0.1/x", "https://\ud800.example/x", None, 42,
])
def test_download_rejects_initial_target_before_request(monkeypatch, mock_http, kind, ref):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [dns_answer("127.0.0.1")])
    requests = mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png"},
                                                content=b"private bytes"))
    assert_rejected(kind, download(kind, ref))
    assert requests == []


@pytest.mark.parametrize("kind", ["image", "page"])
@pytest.mark.parametrize("location", [
    "http://127.0.0.1/secret", "//private.example/secret", "file:///secret",
    "https://public.example:bad/x", "https://public.example:99999/x",
    "https://public.example@127.0.0.1/x",
])
def test_redirect_target_is_validated_before_request(monkeypatch, mock_http, kind, location):
    def resolve(host, *a, **kw):
        return [dns_answer("127.0.0.1" if host in ("127.0.0.1", "private.example") else PUBLIC_IP)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    redirect_body = Body(b"not needed")

    def respond(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": location}, stream=redirect_body)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"secret")

    requests = mock_http(respond)
    assert_rejected(kind, download(kind, "https://public.example/start"))
    assert len(requests) == 1
    assert redirect_body.reads == 0
    assert redirect_body.closed


@pytest.mark.parametrize("kind", ["image", "page"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_relative_redirects_resolve_against_each_request(mock_http, kind, status):
    bodies = []
    paths = ["/start", "/dir/next", "/dir/final"]

    def respond(request):
        index = paths.index(request.url.path)
        body = Body(b"ok" if index == 2 else b"ignored")
        bodies.append(body)
        if index < 2:
            return httpx.Response(status, headers={"location": ["/dir/next", "final"][index]},
                                  stream=body)
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=body)

    requests = mock_http(respond)
    result = download(kind, "https://public.example/start")
    assert result == b"ok" if kind == "image" else result.content == "ok"
    assert [r.url.path for r in requests] == paths
    assert [b.reads for b in bodies] == [0, 0, 1]
    assert all(b.closed for b in bodies)


@pytest.mark.parametrize("kind", ["image", "page"])
def test_even_same_host_redirect_rechecks_dns(monkeypatch, mock_http, kind):
    lookups = iter([[dns_answer()], [dns_answer("10.0.0.2")]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: next(lookups))
    requests = mock_http(lambda r: httpx.Response(302, headers={"location": "/next"}))
    assert_rejected(kind, download(kind, "https://public.example/start"))
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["image", "page"])
@pytest.mark.parametrize("hops", [3, 4])
def test_redirect_limit_has_exact_boundary(mock_http, kind, hops):
    def respond(request):
        index = int(request.url.path[1:])
        if index < hops:
            return httpx.Response(302, headers={"location": f"/{index + 1}"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

    requests = mock_http(respond)
    result = download(kind, "https://public.example/0")
    if hops == 3:
        assert result == b"ok" if kind == "image" else result.content == "ok"
    else:
        assert_rejected(kind, result)
    assert len(requests) == 4


@pytest.mark.parametrize("kind", ["image", "page"])
def test_redirect_loop_is_bounded(mock_http, kind):
    requests = mock_http(lambda r: httpx.Response(302, headers={"location": "/loop"}))
    assert_rejected(kind, download(kind, "https://public.example/loop"))
    assert len(requests) == 4


@pytest.mark.parametrize("kind", ["image", "page"])
@pytest.mark.parametrize("status", [302, 404, 500])
def test_unsuccessful_responses_are_closed_without_reading(mock_http, kind, status):
    body = Body(b"do not read")
    mock_http(lambda r: httpx.Response(status, headers={"content-type": "image/png"}, stream=body))
    assert_rejected(kind, download(kind, "https://public.example/image"))
    assert body.closed and body.reads == 0


@pytest.mark.parametrize("kind", ["image", "page"])
@pytest.mark.parametrize("chunks", [
    (b"x" * (LIMIT * 100), b"must not read"),
    (b"x" * (LIMIT - 1), b"yz", b"must not read"),
])
def test_large_chunks_never_overflow_buffer(monkeypatch, mock_http, kind, chunks):
    buffered = []

    class MeasuredBuffer(bytearray):
        def extend(self, data):
            super().extend(data)
            buffered.append(len(self))

    monkeypatch.setattr(artifacts if kind == "image" else fetch_url,
                        "bytearray", MeasuredBuffer, raising=False)
    body = Body(*chunks)
    # Deliberately dishonest length: the streaming limit must be authoritative.
    mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png",
                                                   "content-length": "1"}, stream=body))
    result = download(kind, "https://public.example/image")
    if kind == "image":
        assert result is None
    else:
        assert result.content.encode() == b"".join(chunks)[:LIMIT]
        assert result.citations
    assert all(size <= LIMIT for size in buffered)
    assert body.reads == len(chunks) - 1
    assert body.closed


@pytest.mark.parametrize("kind", ["image", "page"])
def test_exact_byte_limit_is_accepted(mock_http, kind):
    body = Body(b"x" * (LIMIT - 1), b"y")
    mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png"}, stream=body))
    result = download(kind, "https://public.example/image")
    expected = b"x" * (LIMIT - 1) + b"y"
    assert result == expected if kind == "image" else result.content.encode() == expected
    assert body.closed


@pytest.mark.parametrize("kind", ["image", "page"])
def test_stream_failure_is_closed(mock_http, kind):
    body = Body(b"partial", fail=True)
    mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png"}, stream=body))
    assert_rejected(kind, download(kind, "https://public.example/image"))
    assert body.closed


def test_nonimage_response_is_not_read(mock_http):
    body = Body(b"not an image")
    mock_http(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, stream=body))
    assert artifacts.resolve_image_bytes("https://public.example/page") is None
    assert body.reads == 0 and body.closed


def test_external_artifact_path_is_remote_not_local(mock_http, tmp_path):
    root = tmp_path / "generated"
    root.mkdir()
    (root / "image.png").write_bytes(b"local secret")
    requests = mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png"},
                                                content=b"remote image"))
    result = artifacts.resolve_image_bytes("https://public.example/api/files/image.png?name=x")
    assert result == b"remote image"
    assert len(requests) == 1


@pytest.mark.parametrize("ref", [
    "/prefix/api/files/image.png", "//public.example/api/files/image.png",
    "garbage/api/files/image.png", "/api/files/../image.png", "/api/files/%2e%2e/image.png",
    "/api/files/..", "/api/files/.", "/api/files/image.png/extra",
    "/api/files/image.png\\extra", "/api/files/image.png\x00", "/api/files/image.png:stream",
])
def test_malformed_local_refs_do_not_read_files(mock_http, tmp_path, ref, local_owner):
    root = tmp_path / "generated"
    root.mkdir()
    (root / "image.png").write_bytes(b"local secret")
    requests = mock_http(lambda r: pytest.fail("Malformed local reference was fetched"))
    assert artifacts.resolve_image_bytes(ref, user_id=local_owner) is None
    assert not requests


@pytest.mark.parametrize("suffix", ["", "?name=picture.png", "#preview", "?name=x#preview",
                                    "?name=my picture.png"])
def test_local_generated_images_still_resolve(tmp_path, suffix, local_owner):
    root = tmp_path / "generated"
    root.mkdir()
    (root / "image.png").write_bytes(PNG)
    assert artifacts.resolve_image_bytes(f"/api/files/image.png{suffix}", user_id=local_owner) == PNG


def test_local_image_reads_are_bounded(monkeypatch, local_owner):
    import io

    reads = []

    class File(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr(artifacts.os.path, "exists", lambda p: True)
    monkeypatch.setattr(artifacts.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(artifacts, "open", lambda *a, **kw: File(b"x" * (LIMIT + 1)), raising=False)
    assert artifacts.resolve_image_bytes("/api/files/image.png", user_id=local_owner) is None
    assert reads == [LIMIT + 1]


@pytest.mark.parametrize("encoding", ["base64", "wrapped-base64", "percent-base64", "percent"])
def test_data_uri_formats_still_resolve(encoding):
    b64 = base64.b64encode(PNG).decode()
    if encoding == "percent":
        ref = "data:image/png," + quote_from_bytes(PNG, safe="")
    elif encoding == "percent-base64":
        ref = "data:image/png;base64," + quote_from_bytes(b64.encode(), safe="")
    elif encoding == "wrapped-base64":
        ref = "data:image/png;base64," + b64[:20] + "\r\n" + b64[20:]
    else:
        ref = "data:image/png;base64," + b64
    assert artifacts.resolve_image_bytes(ref) == PNG


@pytest.mark.parametrize("size", [LIMIT, LIMIT + 1, LIMIT * 100])
@pytest.mark.parametrize("encoded", [True, False])
def test_data_uri_size_limit(size, encoded):
    data = b"x" * size
    ref = ("data:image/png;base64," + base64.b64encode(data).decode() if encoded
           else "data:image/svg+xml," + quote_from_bytes(data))
    assert artifacts.resolve_image_bytes(ref) == (data if size <= LIMIT else None)


def test_oversized_base64_is_rejected_before_decode(monkeypatch):
    monkeypatch.setattr(base64, "b64decode", lambda *a, **kw: pytest.fail("Unbounded decode"))
    assert artifacts.resolve_image_bytes("data:image/png;base64," + "A" * (LIMIT * 100)) is None


@pytest.mark.parametrize("ref", ["data:image/png;base64", "data:image/png;base64,@@@",
                                  "data:image/png;base64,abc", "data:image/png;base64,\ud800"])
def test_malformed_data_uris_return_none(ref):
    assert artifacts.resolve_image_bytes(ref) is None


def test_text_sanitization_and_citation_stay_compatible(mock_http):
    mock_http(lambda r: httpx.Response(200, content=b"<p>Hi</p><script>secret</script> there"))
    result = download("page", "https://public.example/start")
    assert result.content == "Hi there"
    assert result.citations == [{"title": "https://public.example/start",
                                "url": "https://public.example/start", "snippet": "Hi there"}]


@pytest.mark.parametrize("kind", ["image", "page"])
def test_basic_auth_stays_on_same_origin_only(mock_http, kind):
    def respond(request):
        if request.url.path == "/dir/start":
            return httpx.Response(302, headers={"location": "next"})
        if request.url.path == "/dir/next":
            return httpx.Response(302, headers={"location": "https://other.example/final"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

    requests = mock_http(respond)
    result = download(kind, "https://user:secret@public.example/dir/start")
    assert result == b"ok" if kind == "image" else result.content == "ok"
    basic_auth = "Basic " + base64.b64encode(b"user:secret").decode()
    assert [r.headers.get("authorization") for r in requests] == [basic_auth, basic_auth, None]


@pytest.mark.parametrize("kind", ["image", "page"])
def test_private_target_on_later_hop_is_blocked(monkeypatch, mock_http, kind):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw: [
        dns_answer("10.0.0.1" if host == "private.example" else PUBLIC_IP)
    ])

    def respond(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(307, headers={"location": "https://private.example/secret"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"secret")

    requests = mock_http(respond)
    assert_rejected(kind, download(kind, "https://public.example/start"))
    assert [r.url.path for r in requests] == ["/start", "/next"]


@pytest.mark.parametrize("kind", ["image", "page"])
def test_redirect_fails_closed_when_dns_disappears(monkeypatch, mock_http, kind):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw:
                        [] if host == "missing.example" else [dns_answer()])
    requests = mock_http(lambda r: httpx.Response(302, headers={"location": "//missing.example/x"}))
    assert_rejected(kind, download(kind, "https://public.example/start"))
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["image", "page"])
def test_byte_limit_applies_to_decompressed_body(mock_http, kind):
    body = Body(gzip.compress(b"x" * (LIMIT * 100)))
    mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png",
                                                   "content-encoding": "gzip"}, stream=body))
    result = download(kind, "https://public.example/image")
    assert result is None if kind == "image" else result.content == "x" * LIMIT
    assert body.closed


def test_private_external_artifact_url_does_not_read_local_file(monkeypatch, mock_http, tmp_path):
    root = tmp_path / "generated"
    root.mkdir()
    (root / "image.png").write_bytes(PNG)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [dns_answer("127.0.0.1")])
    requests = mock_http(lambda r: pytest.fail("Private host was contacted"))
    assert artifacts.resolve_image_bytes("http://127.0.0.1/api/files/image.png") is None
    assert not requests


def test_local_image_embedding_rejects_other_user_and_keeps_own(tmp_path, local_owner):
    """Offline mocked-DB compatibility; real ownership/payload coverage is separate."""
    from app.tools.docx_generate import DocxGenerateTool

    root = tmp_path / "generated"
    root.mkdir()
    (root / "other-user-image.png").write_bytes(PNG)
    (root / "image.png").write_bytes(PNG)
    result = asyncio.get_event_loop().run_until_complete(DocxGenerateTool().run(
        {"title": "Image embedding", "sections": [
            {"image": "/api/files/other-user-image.png"},
            {"image": "/api/files/image.png"},
        ]},
        ToolContext(user_id=local_owner),
    ))
    documents = list(root.glob("*.docx"))
    assert len(documents) == 1 and "/api/files/" in result.content
    with zipfile.ZipFile(documents[0]) as document:
        images = [name for name in document.namelist() if name.startswith("word/media/")]
        assert len(images) == 1
        assert document.read(images[0]) == PNG
        # python-docx deduplicates identical PNGs, so count placements too.
        xml = ElementTree.fromstring(document.read("word/document.xml"))
        assert len(xml.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")) == 1


@pytest.mark.parametrize("origin", [
    "http://public.example", "http://localhost.public.example", "http://localhost:5001",
    "https://localhost:5000", "http://user@localhost:5000", "http://@localhost:5000",
])
def test_other_origins_never_map_to_local_storage(monkeypatch, mock_http, local_owner, origin):
    monkeypatch.setattr(artifacts.settings, "FRONTEND_ORIGIN", "http://localhost:5000")
    monkeypatch.setattr(artifacts, "open", lambda *a, **kw: pytest.fail("Read a local image"),
                        raising=False)
    requests = mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png"},
                                                content=b"remote image"))
    assert artifacts.resolve_image_bytes(
        f"{origin}/api/files/image.png", user_id=local_owner,
    ) == b"remote image"
    assert len(requests) == 1


def test_same_origin_port_zero_is_not_normalized_to_default(monkeypatch, mock_http, local_owner):
    monkeypatch.setattr(artifacts.settings, "FRONTEND_ORIGIN", "http://public.example")
    requests = mock_http(lambda r: pytest.fail("Port zero was fetched"))
    assert artifacts.resolve_image_bytes(
        "http://public.example:0/api/files/image.png", user_id=local_owner,
    ) is None
    assert not requests


@pytest.mark.parametrize("kind", ["data", "remote"])
def test_nonlocal_images_with_context_do_not_query_ownership(monkeypatch, mock_http, local_owner, kind):
    from app import db as database

    monkeypatch.setattr(database, "SessionLocal", lambda: pytest.fail("Nonlocal image queried DB"))
    mock_http(lambda r: httpx.Response(200, headers={"content-type": "image/png"}, content=PNG))
    ref = ("data:image/png;base64," + base64.b64encode(PNG).decode() if kind == "data"
           else "https://public.example/api/files/image.png")
    assert artifacts.resolve_image_bytes(ref, user_id=local_owner) == PNG

