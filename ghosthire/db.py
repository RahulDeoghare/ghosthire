"""SQLite access. No ORM — see plan §3A.3.

The database is a cache of `data/snapshots/`, never the source of truth. Any
reviewer can delete it and rebuild from the archived JSON without spending a
credit, which is the whole provenance argument.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

from . import DATA_DIR, ROOT, SNAPSHOT_DIR

# `.env.example` has always documented this; it now works. Serverless hosts
# mount the deployment read-only except for a scratch directory, so the path
# has to be movable without editing code.
DB_PATH = Path(os.environ.get("GHOSTHIRE_DB") or DATA_DIR / "ghosthire.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database with the two pragmas that actually matter.

    WAL so a read (the API serving a page) is never blocked by a write (a
    sweep ingesting), and foreign_keys because SQLite leaves them off by
    default and a ghost_scores row pointing at a deleted listing is exactly the
    corruption this project cannot afford.
    """
    path = Path(path) if path else DB_PATH
    writable = _writable(path.parent)
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)

    if writable:
        conn = sqlite3.connect(path)
        # WAL buys concurrency between a reader and a writer, which is the
        # normal case: the API serves pages while a sweep ingests.
        conn.execute("PRAGMA journal_mode = WAL")
    else:
        # A read-only mount — a serverless deployment shipping a prebuilt
        # database. Opened explicitly read-only so SQLite does not try to
        # create the journal it would otherwise expect to own.
        #
        # Such a database must be shipped in DELETE journal mode, not WAL: a
        # WAL database cannot be read at all without writing its sidecars, so
        # the mode is part of the artifact, not a runtime detail. See
        # `read_only_copy`.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _writable(directory: Path) -> bool:
    try:
        return os.access(directory, os.W_OK) or not directory.exists()
    except OSError:
        return False


def read_only_copy(source: Path, destination: Path) -> Path:
    """Write a copy that can be served from a read-only filesystem.

    Only the journal mode differs, and it is the whole point: a WAL database
    needs to write its sidecar files even to answer a SELECT, so shipping one
    to a read-only host produces "attempt to write a readonly database" on the
    first query rather than at deploy time.
    """
    import shutil

    shutil.copyfile(source, destination)
    conn = sqlite3.connect(destination)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.commit()
    finally:
        conn.close()
    for sidecar in ("-wal", "-shm"):
        Path(str(destination) + sidecar).unlink(missing_ok=True)
    return destination


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
