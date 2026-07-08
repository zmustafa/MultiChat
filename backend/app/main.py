from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import (
    analytics,
    auth,
    evals,
    files,
    folders,
    integrations,
    personas,
    providers,
    sessions,
    settings_router,
    snippets,
    snapshots,
    system,
    tools,
    uploads,
)
from .security import hash_password

app = FastAPI(title="MultiChat Compare API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_ORIGIN,
        "http://localhost:5000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _seed_admin() -> None:
    """Seed a default admin/admin account for quick sign-in."""
    from .models import User

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == "admin").first()
        if not existing:
            db.add(User(email="admin", password_hash=hash_password("admin")))
            db.commit()
    finally:
        db.close()


def _migrate() -> None:
    """Lightweight SQLite migration: add columns introduced after a DB was created.

    Avoids an Alembic dependency for this single-user app while still letting the
    schema evolve without wiping the database.
    """
    if not settings.DATABASE_URL.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    additions = {
        "users": {"custom_instructions": "TEXT"},
        "sessions": {
            "folder_id": "VARCHAR",
            "pinned": "BOOLEAN DEFAULT 0",
            "archived": "BOOLEAN DEFAULT 0",
            "trashed": "BOOLEAN DEFAULT 0",
        },
        "personas": {"tools_enabled": "BOOLEAN DEFAULT 0", "is_default": "BOOLEAN DEFAULT 0"},
        "lanes": {"hidden": "BOOLEAN DEFAULT 0"},
        "integrations": {"eula_accepted": "BOOLEAN DEFAULT 0"},
        "attachments": {"extracted_text": "TEXT"},
    }
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in additions.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))


def _cleanup_generated() -> None:
    """Prune generated files (and their DB rows) older than the retention window."""
    import os
    import time

    from sqlalchemy import select

    from .models import GeneratedFile
    from .tools.artifacts import GENERATED_SUBDIR

    gen_dir = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR)
    if not os.path.isdir(gen_dir):
        return
    cutoff = time.time() - settings.GENERATED_RETENTION_DAYS * 86400
    removed: set[str] = set()
    for name in os.listdir(gen_dir):
        path = os.path.join(gen_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed.add(name)
        except OSError:
            pass
    if removed:
        db = SessionLocal()
        try:
            for row in db.scalars(select(GeneratedFile)).all():
                if row.stored_name in removed:
                    db.delete(row)
            db.commit()
        finally:
            db.close()


def _reset_orphaned_lanes() -> None:
    """Reset lanes left mid-generation (state 'streaming'/'thinking') by a crash or
    reload back to 'idle' so they aren't stuck forever."""
    from sqlalchemy import select

    from .models import Lane

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(Lane).where(Lane.state.in_(["streaming", "thinking"]))
        ).all()
        for l in rows:
            l.state = "idle"
        if rows:
            db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    # import models so metadata is populated before create_all
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate()
    _seed_admin()
    _cleanup_generated()
    _reset_orphaned_lanes()
    _reconnect_integrations()
    from .broadcast import _sweep_stale_run_files

    _sweep_stale_run_files()


def _reconnect_integrations() -> None:
    """Re-establish saved integration connections (e.g. Work IQ) in the background."""
    import asyncio

    from .mcp.workiq import workiq
    from .models import Integration

    db = SessionLocal()
    try:
        row = db.query(Integration).filter(
            Integration.kind == "workiq", Integration.enabled == True  # noqa: E712
        ).first()
        if not row:
            return
        command = row.command
        args = list(row.args_json or [])
        # Seed persisted EULA acceptance so connect() can auto-replay it and the model
        # is never asked to accept again.
        workiq.eula_accepted = bool(row.eula_accepted)
    finally:
        db.close()

    async def _go() -> None:
        try:
            await workiq.connect(command, args)
        except Exception:  # noqa: BLE001
            pass  # surfaced via the integrations status endpoint

    try:
        asyncio.get_event_loop().create_task(_go())
    except RuntimeError:
        pass


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(providers.router)
app.include_router(personas.router)
app.include_router(folders.router)
app.include_router(snippets.router)
app.include_router(settings_router.router)
app.include_router(sessions.router)
app.include_router(tools.router)
app.include_router(uploads.router)
app.include_router(files.router)
app.include_router(analytics.router)
app.include_router(evals.router)
app.include_router(integrations.router)
app.include_router(system.router)
app.include_router(snapshots.router)
