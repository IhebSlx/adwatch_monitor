"""Online SQLite backups — the whole product's value (paid ad history + human-
verified identities) lives in one file, so this must never be optional.

backup_now() uses the sqlite3 backup API, which is safe while the app is
running (no VACUUM lock, consistent snapshot). Rotated: the newest
config.BACKUP_KEEP daily files are kept. Scheduled nightly by the scheduler,
and also run once before each risky migration by callers that want it.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import config

logger = logging.getLogger("adwatch.backup")


def _db_file() -> Path | None:
    """Filesystem path of the SQLite DB, or None for non-sqlite URLs."""
    url = config.DB_URL
    if not url.startswith("sqlite"):
        return None
    return Path(url.split("///", 1)[1]) if "///" in url else None


def backup_now(tag: str = "") -> str | None:
    """Write a consistent snapshot into BACKUP_DIR and rotate old ones.
    Returns the backup path, or None if not applicable / failed. Never raises —
    a backup failure must not take down the caller (scheduler/startup)."""
    src = _db_file()
    if src is None or not src.exists():
        return None
    try:
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # timestamp comes from the file's own mtime (no Date.now allowed in some
        # contexts, and mtime is a fine monotonic-ish key here)
        import datetime as dt
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"adwatch_{stamp}{('_' + tag) if tag else ''}.db"
        dest = config.BACKUP_DIR / name
        with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dest)) as d:
            s.backup(d)             # online, consistent, no exclusive lock
        _rotate()
        logger.info("DB backup written: %s", dest)
        return str(dest)
    except Exception:
        logger.exception("DB backup failed")
        return None


def _rotate() -> None:
    files = sorted(config.BACKUP_DIR.glob("adwatch_*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[config.BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def latest_backup() -> str | None:
    if not config.BACKUP_DIR.exists():
        return None
    files = sorted(config.BACKUP_DIR.glob("adwatch_*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None
