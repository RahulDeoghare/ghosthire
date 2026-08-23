"""SQLite access. No ORM — see plan §3A.3.

The database is a cache of `data/snapshots/`, never the source of truth. Any
reviewer can delete it and rebuild from the archived JSON without spending a
credit, which is the whole provenance argument.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

from . import DATA_DIR, ROOT, SNAPSHOT_DIR

DB_PATH = DATA_DIR / "ghosthire.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database with the two pragmas that actually matter.

    WAL so a read (the API serving a page) is never blocked by a write (a
    sweep ingesting), and foreign_keys because SQLite leaves them off by
    default and a ghost_scores row pointing at a deleted listing is exactly the
    corruption this project cannot afford.
    """
    path = Path(path) if path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def iter_snapshots(
    directory: Path | None = None, warn: bool = True
) -> Iterator[tuple[Path, Any]]:
    """Every readable snapshot, oldest first.

    Skips what it cannot parse instead of aborting. `publish_snapshot` promotes
    any non-empty file, including one holding a truncated or malformed
    response, so a single bad file must not take the whole rebuild down with
    it — the rebuild is the thing that lets a reviewer check us without paying.
    """
    directory = directory or SNAPSHOT_DIR
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if warn:
                try:
                    shown = path.relative_to(ROOT)
                except ValueError:
                    shown = path
                print(f"  skipping unreadable snapshot {shown}: {exc}",
                      file=sys.stderr)
            continue
        yield path, payload


def record_run(
    conn: sqlite3.Connection,
    source: str,
    collector_id: str,
    target_url: str | None,
    status: str,
    rows_returned: int,
    rows_rejected: int = 0,
    started_at: str | None = None,
    completed_at: str | None = None,
    heal_event: str | None = None,
    raw_path: str | None = None,
) -> int:
    """One row per invocation, success or failure. Failures are data."""
    cur = conn.execute(
        """
        INSERT INTO scrape_runs (source, collector_id, target_url, status,
                                 rows_returned, rows_rejected, started_at,
                                 completed_at, heal_event, raw_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, collector_id, target_url, status, rows_returned, rows_rejected,
         started_at, completed_at, heal_event, raw_path),
    )
    conn.commit()
    return int(cur.lastrowid or 0)
