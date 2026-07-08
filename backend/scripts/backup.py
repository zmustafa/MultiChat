#!/usr/bin/env python
"""One-shot backup of the MultiChat data (SQLite DB + uploads).

Usage:
  python scripts/backup.py                 # create a timestamped backup under ./backups
  python scripts/backup.py --restore PATH  # restore DB + uploads from a backup folder

Run from the backend/ directory.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB_CANDIDATES = ["chatbot.db", "devui.db"]
UPLOADS = BACKEND / "uploads"
BACKUPS = BACKEND / "backups"


def _db_path() -> Path | None:
    for name in DB_CANDIDATES:
        p = BACKEND / name
        if p.exists():
            return p
    return None


def backup() -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS / ts
    dest.mkdir(parents=True, exist_ok=True)
    db = _db_path()
    if db:
        # copy DB + WAL/SHM sidecars if present
        for suffix in ("", "-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copy2(side, dest / side.name)
    if UPLOADS.exists():
        shutil.copytree(UPLOADS, dest / "uploads", dirs_exist_ok=True)
    print(f"Backup written to {dest}")


def restore(path: str) -> None:
    src = Path(path)
    if not src.exists():
        print(f"No such backup: {src}", file=sys.stderr)
        sys.exit(1)
    for f in src.glob("*.db*"):
        shutil.copy2(f, BACKEND / f.name)
    up = src / "uploads"
    if up.exists():
        shutil.copytree(up, UPLOADS, dirs_exist_ok=True)
    print(f"Restored from {src}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", metavar="PATH", help="restore from a backup folder")
    args = ap.parse_args()
    if args.restore:
        restore(args.restore)
    else:
        backup()
