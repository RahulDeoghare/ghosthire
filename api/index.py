"""Vercel entry point.

The deployment is read-only, so the database is rebuilt into the scratch
directory on cold start from the snapshot archive committed to the repo. That
is the same `db --from-snapshots` a reviewer runs locally, which means the
hosted numbers and the local ones come from one source and cannot drift.

The rebuild takes well under a second for the current archive. If it ever
stops being cheap, build the database at deploy time instead and ship it
read-only — the code path is the same.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The function bundle puts this file under api/; the package sits beside it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCRATCH = Path(os.environ.get("GHOSTHIRE_DB") or "/tmp/ghosthire.db")
os.environ["GHOSTHIRE_DB"] = str(SCRATCH)

from ghosthire import db, pipeline  # noqa: E402
from ghosthire.api import app  # noqa: E402  — re-exported for the runtime

_ready = False


def _ensure_database() -> None:
    """Build the read model once per cold start."""
    global _ready
    if _ready:
        return
    conn = db.connect(SCRATCH)
    db.init(conn)
    # An empty database means this container has not built one yet. A warm
    # container skips straight past.
    if not conn.execute("SELECT COUNT(*) FROM job_listings").fetchone()[0]:
        pipeline.rebuild(conn, quiet=True)
    conn.close()
    _ready = True


@app.on_event("startup")
def _startup() -> None:
    _ensure_database()


_ensure_database()
