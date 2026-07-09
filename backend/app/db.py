from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings

connect_args = {}
engine_kwargs: dict = {"future": True}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    # Streaming/regeneration tasks each hold a DB session open for the whole (multi-second)
    # response. With a bounded pool, many concurrent regenerations exhaust it and every new
    # request blocks waiting for a connection. NullPool gives each task its own SQLite
    # connection (cheap for a local file), so concurrency is bounded only by SQLite itself.
    engine_kwargs["poolclass"] = NullPool

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    **engine_kwargs,
)

if settings.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # Wait up to 30s for a write lock instead of failing/blocking indefinitely, so
        # concurrent writers (multiple lanes finishing at once) queue rather than error.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
