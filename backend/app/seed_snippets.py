"""Prebuilt "starter" snippets shipped with MultiChat.

These are seeded into a user's account on first run so a fresh install comes with a small
set of reusable prompt snippets instead of an empty library.

Seeding is idempotent: a starter snippet is only created if the user has no snippet with the
same title, so it never duplicates or overwrites a user's own snippets.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .models import Snippet, User


# ---------------------------------------------------------------------------
# Catalog. Each entry ships to end users.
# ---------------------------------------------------------------------------

STARTER_SNIPPETS: list[dict] = [
    {
        "title": "Help me investigate",
        "content": "Help me investigate. Search over internet and help me investigate this.",
    },
    {
        "title": "How to respond",
        "content": "How to respond",
    },
    {
        "title": "What is this",
        "content": "What is this",
    },
]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_starter_snippets(db: DbSession, user: User) -> int:
    """Create any missing starter snippets for `user`. Idempotent (dedup by title).

    Returns the number of snippets created.
    """
    existing_titles = {
        (s.title or "").strip().lower()
        for s in db.scalars(select(Snippet).where(Snippet.user_id == user.id)).all()
    }

    created = 0
    for spec in STARTER_SNIPPETS:
        if spec["title"].strip().lower() in existing_titles:
            continue
        db.add(
            Snippet(
                user_id=user.id,
                title=spec["title"],
                content=spec.get("content", ""),
            )
        )
        created += 1

    if created:
        db.commit()
    return created
