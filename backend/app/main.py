from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import (
    analytics,
    auth,
    deliberation,
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


def _seed_personas() -> None:
    """Seed the curated starter personas for every user (idempotent, dedup by name), so a
    fresh install ships with a useful persona library instead of an empty one."""
    from sqlalchemy import select

    from .models import User
    from .seed_personas import seed_starter_personas

    db = SessionLocal()
    try:
        for user in db.scalars(select(User)).all():
            try:
                seed_starter_personas(db, user)
            except Exception:  # noqa: BLE001 — never let seeding break startup
                db.rollback()
    finally:
        db.close()


def _seed_snippets() -> None:
    """Seed the curated starter snippets for every user (idempotent, dedup by title), so a
    fresh install ships with a few reusable prompt snippets instead of an empty library."""
    from sqlalchemy import select

    from .models import User
    from .seed_snippets import seed_starter_snippets

    db = SessionLocal()
    try:
        for user in db.scalars(select(User)).all():
            try:
                seed_starter_snippets(db, user)
            except Exception:  # noqa: BLE001 — never let seeding break startup
                db.rollback()
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
        "users": {
            "custom_instructions": "TEXT",
            "new_chat_use_default_persona": "BOOLEAN DEFAULT 0",
        },
        "sessions": {
            "folder_id": "VARCHAR",
            "pinned": "BOOLEAN DEFAULT 0",
            "archived": "BOOLEAN DEFAULT 0",
            "trashed": "BOOLEAN DEFAULT 0",
            "mode": "VARCHAR DEFAULT 'compare'",
        },
        "personas": {
            "tools_enabled": "BOOLEAN DEFAULT 0",
            "is_default": "BOOLEAN DEFAULT 0",
            "deliberation_json": "JSON",
        },
        "lanes": {"hidden": "BOOLEAN DEFAULT 0"},
        "integrations": {"eula_accepted": "BOOLEAN DEFAULT 0"},
        "attachments": {"extracted_text": "TEXT"},
        "lane_messages": {"ttft_ms": "INTEGER"},
        "deliberation_runs": {"vote_json": "JSON"},
        "deliberation_steps": {"message_id": "VARCHAR"},
    }
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        added: set[str] = set()
        for table, cols in additions.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))
                    added.add(f"{table}.{col}")
        if "deliberation_steps.message_id" in added:
            _backfill_step_messages(conn)


# Phases whose output is a real answer, and therefore has a transcript message. A vote
# ballot or a synthesis critique produces neither, so it must not be paired with one.
_ANSWER_PHASES = ("draft", "critique", "synthesis", "synthesis_revise")


def _backfill_step_messages(conn) -> None:
    """Pair deliberation steps written before ``message_id`` existed with their message.

    Rounds are not unique on their own: a vote ballot shares round 0 with the draft it
    ranks, and the synthesis and its revision share round 99. So steps and messages are
    paired *in order* within each (lane, turn, round) group, and only for the phases that
    actually produce an answer. Getting this wrong would hand two steps the same message
    and offer a PDF of the wrong text.
    """
    from sqlalchemy import text

    phases = ", ".join(f"'{p}'" for p in _ANSWER_PHASES)  # module constant, no user input
    steps = conn.execute(
        text(
            f"""
            SELECT ds.id, ds.lane_id, r.turn_id, ds.round_index
              FROM deliberation_steps ds
              JOIN deliberation_runs r ON r.id = ds.run_id
             WHERE ds.message_id IS NULL
               AND ds.lane_id IS NOT NULL
               AND ds.error IS NULL
               AND ds.phase IN ({phases})
             ORDER BY ds.created_at, ds.id
            """
        )
    ).fetchall()
    if not steps:
        return
    messages = conn.execute(
        text(
            "SELECT id, lane_id, turn_id, order_index FROM lane_messages "
            "WHERE role = 'assistant' ORDER BY created_at, id"
        )
    ).fetchall()
    pool: dict[tuple, list[str]] = {}
    for msg_id, lane_id, turn_id, order_index in messages:
        pool.setdefault((lane_id, turn_id, order_index), []).append(msg_id)
    for step_id, lane_id, turn_id, round_index in steps:
        queue = pool.get((lane_id, turn_id, round_index))
        if not queue:
            continue
        conn.execute(
            text("UPDATE deliberation_steps SET message_id = :m WHERE id = :s"),
            {"m": queue.pop(0), "s": step_id},
        )


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


def _reset_orphaned_deliberations() -> None:
    """Fail deliberation runs left mid-flight by a crash or reload.

    A run's in-memory driver dies with the process, so a run still marked
    'pending'/'running' at startup can never make progress — without this it would
    render as "running…" in the sidebar forever.
    """
    from sqlalchemy import select

    from .models import DeliberationRun

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(DeliberationRun).where(
                DeliberationRun.status.in_(["pending", "running"])
            )
        ).all()
        for r in rows:
            r.status = "failed"
            r.error = r.error or "Interrupted by a server restart."
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
    _seed_personas()
    _seed_snippets()
    _cleanup_generated()
    _reset_orphaned_lanes()
    _reset_orphaned_deliberations()
    _reconnect_integrations()
    from .broadcast import _sweep_stale_run_files

    _sweep_stale_run_files()


def _reconnect_integrations() -> None:
    """Re-establish saved integration connections (e.g. Work IQ) in the background."""
    import asyncio

    from .mcp.workiq import DEFAULT_ARGS, DEFAULT_COMMAND, workiq
    from .models import Integration

    db = SessionLocal()
    try:
        row = db.query(Integration).filter(
            Integration.kind == "workiq", Integration.enabled == True  # noqa: E712
        ).first()
        if not row:
            return
        # The launch command is fixed in code, not read from the row — a restored backup
        # must not be able to change what process the server spawns.
        command = DEFAULT_COMMAND
        args = list(DEFAULT_ARGS)
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
app.include_router(deliberation.router)
