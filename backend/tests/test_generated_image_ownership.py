"""Offline ownership regressions using the conftest DB, real images and documents.

No app lifespan, HTTP server, provider call or production database is used.
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
import socket
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app import db as database
from app import export, models
from app.markdown_render import markdown_pdf_flowables
from app.security import create_access_token, current_user
from app.tools import artifacts
from app.tools.base import ToolContext
from app.tools.docx_generate import DocxGenerateTool
from app.tools.pdf_generate import PdfGenerateTool
from app.tools.pptx_generate import PptxGenerateTool


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    # Windows needs its local wakeup socketpair before sockets are blocked.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def forbidden(*args, **kwargs):
        pytest.fail("No network or HTTP client is allowed in local image tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(artifacts.settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(artifacts.settings, "FRONTEND_ORIGIN", "http://localhost:5000")
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def png(color):
    output = io.BytesIO()
    Image.new("RGB", (8, 2), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def owned(db, tmp_path):
    # Refuse to initialize any DB other than the parent conftest's throwaway SQLite.
    bind = db.get_bind()
    assert bind is database.engine
    assert bind.url.get_backend_name() == "sqlite"
    assert Path(bind.url.database).parent.name.startswith("multichat-tests-")
    database.Base.metadata.create_all(bind)
    owner = models.User(email=f"{uuid.uuid4().hex}@example.test", password_hash="unused")
    other = models.User(email=f"{uuid.uuid4().hex}@example.test", password_hash="unused")
    db.add_all([owner, other])
    db.flush()
    root = tmp_path / "generated"
    root.mkdir()
    images = {"own": png("red"), "other": png("blue"), "orphan": png("green")}
    names = {key: f"{uuid.uuid4().hex}.png" for key in images}
    for key, data in images.items():
        (root / names[key]).write_bytes(data)
        if key != "orphan":
            db.add(models.GeneratedFile(
                user_id=owner.id if key == "own" else other.id,
                stored_name=names[key], download_name=f"{key}.png",
                mime_type="image/png", kind="image", size_bytes=len(data),
            ))
    db.commit()
    return SimpleNamespace(db=db, owner=owner, other=other, root=root,
                           images=images, names=names)


def local_ref(owned, key="own", absolute=False):
    origin = artifacts.settings.FRONTEND_ORIGIN if absolute else ""
    return f"{origin}/api/files/{owned.names[key]}"


@pytest.mark.parametrize("kwargs", [{}, {"user_id": None}, {"user_id": ""}])
def test_local_default_denies_without_opening_db_or_file(owned, monkeypatch, kwargs):
    def forbidden(*args, **kw):
        pytest.fail("Unscoped image accessed a database or file")

    monkeypatch.setattr(database, "SessionLocal", forbidden)
    monkeypatch.setattr(artifacts, "open", forbidden, raising=False)
    assert artifacts.resolve_image_bytes(local_ref(owned), **kwargs) is None


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("key", ["other", "orphan"])
def test_local_cross_user_and_unregistered_images_denied_before_read(
    owned, monkeypatch, absolute, key,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Unowned image was opened")

    monkeypatch.setattr(artifacts, "open", forbidden, raising=False)
    assert artifacts.resolve_image_bytes(
        local_ref(owned, key, absolute), user_id=owned.owner.id,
    ) is None


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("use_existing_db", [False, True])
@pytest.mark.parametrize("suffix", ["", "?name=photo.png", "#preview", "?name=x#preview"])
def test_owned_images_and_legacy_urls_preserved(owned, absolute, use_existing_db, suffix):
    kwargs = {"db": owned.db} if use_existing_db else {}
    assert artifacts.resolve_image_bytes(
        local_ref(owned, absolute=absolute) + suffix, user_id=owned.owner.id, **kwargs,
    ) == owned.images["own"]


def test_ownership_is_not_inferred_from_db_session(owned):
    assert artifacts.resolve_image_bytes(local_ref(owned), db=owned.db) is None


def test_existing_db_is_reused_and_left_open(owned, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Existing session must not be replaced, committed, rolled back or closed")

    with monkeypatch.context() as patch:
        patch.setattr(database, "SessionLocal", forbidden)
        for method in ("close", "commit", "rollback"):
            patch.setattr(owned.db, method, forbidden)
        assert artifacts.resolve_image_bytes(
            local_ref(owned), user_id=owned.owner.id, db=owned.db,
        ) == owned.images["own"]
        assert owned.db.scalar(select(models.User.id).where(models.User.id == owned.owner.id))


@pytest.mark.parametrize("failure", [False, True])
def test_dedicated_db_session_closed_even_on_query_failure(owned, monkeypatch, failure):
    session = database.SessionLocal()
    close = Mock(wraps=session.close)
    monkeypatch.setattr(session, "close", close)
    if failure:
        monkeypatch.setattr(session, "scalar", Mock(side_effect=RuntimeError("DB unavailable")))
        monkeypatch.setattr(artifacts, "open", lambda *a, **kw: pytest.fail("Read after DB failure"),
                            raising=False)
    monkeypatch.setattr(database, "SessionLocal", lambda: session)
    result = artifacts.resolve_image_bytes(local_ref(owned), user_id=owned.owner.id)
    assert result == (None if failure else owned.images["own"])
    close.assert_called_once()


def test_existing_db_query_failure_denies_before_read(owned, monkeypatch):
    monkeypatch.setattr(owned.db, "scalar", Mock(side_effect=RuntimeError("DB unavailable")))
    monkeypatch.setattr(artifacts, "open", lambda *a, **kw: pytest.fail("Read after DB failure"),
                        raising=False)
    assert artifacts.resolve_image_bytes(
        local_ref(owned), user_id=owned.owner.id, db=owned.db,
    ) is None


def test_owned_missing_file_returns_none(owned):
    (owned.root / owned.names["own"]).unlink()
    assert artifacts.resolve_image_bytes(local_ref(owned), user_id=owned.owner.id) is None


def embedded_images(path):
    """Inspect payloads, not mocked resolver calls or successful output filenames."""
    if path.suffix == ".pdf":
        return [item.image.convert("RGB").tobytes()
                for page in PdfReader(path).pages for item in page.images]
    prefix = "word/media/" if path.suffix == ".docx" else "ppt/media/"
    with zipfile.ZipFile(path) as document:
        return [document.read(name) for name in document.namelist() if name.startswith(prefix)]


def expected_image(kind, data):
    return Image.open(io.BytesIO(data)).convert("RGB").tobytes() if kind == "pdf" else data


@pytest.mark.parametrize("kind", ["docx", "pdf", "pptx"])
@pytest.mark.parametrize("source", ["local", "legacy", "data"])
def test_authenticated_generators_preserve_only_authorized_images(owned, offline, kind, source):
    # A real JWT/current_user lookup supplies ToolContext identity, not model arguments.
    tool = {"docx": DocxGenerateTool, "pdf": PdfGenerateTool, "pptx": PptxGenerateTool}[kind]()
    for actor in (owned.owner, owned.other, owned.owner):
        authenticated = current_user(token=create_access_token(actor.id), db=owned.db)
        refs = [local_ref(owned, key, source == "legacy") for key in owned.images]
        if source == "data":
            refs = ["data:image/png;base64," + base64.b64encode(owned.images["own"]).decode()]
        items = [{"title": "Picture", "image": ref} for ref in refs]
        result = offline.run_until_complete(tool.run(
            {"title": "Ownership regression", "slides" if kind == "pptx" else "sections": items,
             "user_id": owned.other.id},  # Untrusted args cannot override ctx.
            ToolContext(user_id=authenticated.id),
        ))
        match = re.search(r"/api/files/([^?\s)]+)", result.content)
        assert match, result.content
        path = owned.root / match.group(1)
        key = "own" if source == "data" or actor is owned.owner else "other"
        assert embedded_images(path) == [expected_image(kind, owned.images[key])]


@pytest.fixture
def chat(owned):
    db = owned.db
    provider = models.Provider(user_id=owned.owner.id, name="Offline", provider_type="openai_compatible")
    session = models.Session(user_id=owned.owner.id, title="Image ownership")
    db.add_all([provider, session])
    db.flush()
    lane = models.Lane(session_id=session.id, provider_id=provider.id, model="offline")
    turn = models.Turn(session_id=session.id, content="Render the images")
    db.add_all([lane, turn])
    db.flush()
    message = models.LaneMessage(lane_id=lane.id, turn_id=turn.id, content="Images")
    run = models.DeliberationRun(user_id=owned.owner.id, session_id=session.id, turn_id=turn.id,
                                prompt=turn.content, status="converged")
    db.add_all([message, run])
    db.commit()
    return SimpleNamespace(session=session, lane=lane, turn=turn, message=message, run=run)


@pytest.mark.parametrize("surface", ["session", "message", "draft", "critique", "synthesis", "minority"])
@pytest.mark.parametrize("absolute", [False, True])
def test_exports_reuse_db_and_scope_all_pdf_image_surfaces(owned, chat, monkeypatch, surface, absolute):
    def forbidden(*args, **kwargs):
        pytest.fail("Export did not reuse its database session")

    monkeypatch.setattr(database, "SessionLocal", forbidden)
    # Include a nested quote to exercise the PDF renderer's closure context.
    # Captions use KeepTogether, which ReportLab cannot split inside the existing
    # minority-report table. Uncaptioned images isolate ownership from that layout bug.
    md = "\n\n".join(f"> ![]({local_ref(owned, key, absolute)})" for key in owned.images)
    chat.message.content = md
    if surface in ("draft", "critique"):
        owned.db.add(models.DeliberationStep(
            run_id=chat.run.id, lane_id=chat.lane.id, phase=surface,
            round_index=0 if surface == "draft" else 1, model="offline",
            output_json={"answer" if surface == "draft" else "revised_answer": md},
        ))
    elif surface == "synthesis":
        chat.run.synthesis = md
    elif surface == "minority":
        chat.run.minority_report = md
    owned.db.flush()
    if surface == "session":
        stored, _, _ = export.export_session(owned.db, chat.session, "pdf")
    elif surface == "message":
        stored, _, _ = export.export_message_pdf(owned.db, chat.session, chat.message)
    else:
        stored, _, _ = export.export_deliberation_pdf(owned.db, chat.run)
    assert embedded_images(owned.root / stored) == [expected_image("pdf", owned.images["own"])]


def test_deliberation_uses_run_owner_not_session_owner(owned, chat):
    # Deliberation routes authorize run.user_id, so it is authoritative for images too.
    chat.run.user_id = owned.other.id
    chat.run.synthesis = "\n\n".join(f"![{key}]({local_ref(owned, key)})" for key in owned.images)
    owned.db.flush()
    stored, _, _ = export.export_deliberation_pdf(owned.db, chat.run)
    assert embedded_images(owned.root / stored) == [expected_image("pdf", owned.images["other"])]


def test_markdown_pdf_default_denies_local_but_preserves_data(owned):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate

    data_uri = "data:image/png;base64," + base64.b64encode(owned.images["own"]).decode()
    md = f"![local]({local_ref(owned, 'other')})\n\n![data]({data_uri})"
    path = owned.root / "unscoped.pdf"
    SimpleDocTemplate(str(path)).build(markdown_pdf_flowables(md, getSampleStyleSheet()["BodyText"]))
    assert embedded_images(path) == [expected_image("pdf", owned.images["own"])]


@pytest.mark.parametrize("origin,url_origin", [
    ("http://localhost", "HTTP://LOCALHOST:80"),
    ("https://app.example:443", "https://app.example"),
])
def test_legacy_origin_normalization_keeps_owned_images(owned, monkeypatch, origin, url_origin):
    monkeypatch.setattr(artifacts.settings, "FRONTEND_ORIGIN", origin)
    assert artifacts.resolve_image_bytes(
        f"{url_origin}/api/files/{owned.names['own']}", user_id=owned.owner.id,
    ) == owned.images["own"]


def test_owned_path_cannot_escape_generated_directory(owned, monkeypatch):
    realpath = artifacts.os.path.realpath
    target = owned.root / owned.names["own"]

    def resolved(path):
        if Path(path) == target:
            return str(owned.root.parent / "outside.png")
        return realpath(path)

    monkeypatch.setattr(artifacts.os.path, "realpath", resolved)
    monkeypatch.setattr(artifacts, "open", lambda *a, **kw: pytest.fail("Path escaped root"),
                        raising=False)
    assert artifacts.resolve_image_bytes(local_ref(owned), user_id=owned.owner.id) is None
