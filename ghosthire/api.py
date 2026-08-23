"""HTTP surface. FastAPI, serving the dashboard and the JSON behind it.

Two rules the endpoints all obey:

- **An unassessed listing never carries a score.** `ghost_score` is null and
  `career_page_checked` is 0, and the client is expected to render that as
  "not assessed" rather than as a low score. The database enforces the pairing.
- **Every row names the collector that produced it.** A number in the UI that
  cannot be traced back to a `c_*` ID and a snapshot on disk should not be on
  screen.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import WEB_DIR
from .bdata import BdataError, load_collectors, run_collector
from .db import DB_PATH, connect, record_run
from .ingest import ingest_rows
from .pipeline import score_all
from .score import BANDS, SIGNALS, band

app = FastAPI(
    title="GhostHire",
    description=(
        "Job listings advertised on boards that the hiring company does not "
        "list on its own careers page. Every score cites two URLs and the "
        "collector that produced them."
    ),
    version="0.1.0",
)


def db() -> sqlite3.Connection:
    return connect(DB_PATH)


def _listing(row: sqlite3.Row) -> dict[str, Any]:
    """One listing plus its score, shaped for the UI."""
    signals = json.loads(row["signals"]) if row["signals"] else []
    checked = bool(row["career_page_checked"])
    score = row["ghost_score"]
    return {
        "id": row["id"],
        "company": row["company_name"],
        "job_title": row["job_title"],
        "location": row["location"],
        "date_posted": row["date_posted"],
        # 'exact' | 'relative' | 'absent'. A relative date is accurate only to
        # its own unit, so the UI must not print it as if it were a calendar
        # date we read off the page.
        "date_confidence": row["date_posted_confidence"],
        "ghost_score": score,
        "band": band(score) if score is not None else None,
        "signals": signals,
        "match_confidence": row["match_confidence"],
        "career_page_checked": 1 if checked else 0,
        "assessed": checked,
        "board_url": row["job_url"],
        "collector_id": row["collector_id"],
        "source": row["source"],
        "salary_range": row["salary_range"],
    }


_SELECT = """
    SELECT l.*, s.ghost_score, s.signals, s.match_confidence,
           s.career_page_checked, s.matched_career_listing_id
      FROM job_listings l
      LEFT JOIN ghost_scores s ON s.listing_id = l.id
     WHERE l.source != 'career_page'
"""


@app.get("/api/listings")
def listings(
    company: str | None = None,
    assessed: bool | None = Query(None, description="filter by career_page_checked"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    sql, params = _SELECT, []
    if company:
        sql += " AND l.company_name_normalized = ?"
        params.append(company.lower())
    if assessed is not None:
        sql += " AND COALESCE(s.career_page_checked, 0) = ?"
        params.append(1 if assessed else 0)
    sql += " ORDER BY l.company_name, l.job_title LIMIT ?"
    params.append(limit)
    with db() as conn:
        return [_listing(r) for r in conn.execute(sql, params)]


@app.get("/api/leaderboard")
def leaderboard(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    """Ranked by ghost score.

    Assessed listings only. An unassessed listing has no score, so ranking it
    would mean inventing one — they are available from /api/listings with
    ``assessed=false`` and belong in the UI as an honest empty state.
    """
    sql = _SELECT + """
        AND s.career_page_checked = 1
        ORDER BY s.ghost_score DESC, l.company_name LIMIT ?
    """
    with db() as conn:
        return [_listing(r) for r in conn.execute(sql, [limit])]


@app.get("/api/listings/{listing_id}")
def listing_detail(listing_id: int) -> dict[str, Any]:
    """One listing with the careers-page role it was compared against.

    The evidence view: both URLs side by side so a reader can check the claim
    themselves in ten seconds. That is the difference between a finding and an
    accusation.
    """
    with db() as conn:
        row = conn.execute(_SELECT + " AND l.id = ?", [listing_id]).fetchone()
        if row is None:
            raise HTTPException(404, f"no listing {listing_id}")
        payload = _listing(row)

        matched_id = row["matched_career_listing_id"]
        payload["matched_career_listing"] = None
        if matched_id:
            career = conn.execute(
                "SELECT * FROM job_listings WHERE id = ?", [matched_id]
            ).fetchone()
            if career:
                payload["matched_career_listing"] = {
                    "id": career["id"],
                    "job_title": career["job_title"],
                    "location": career["location"],
                    "career_url": career["job_url"],
                    "collector_id": career["collector_id"],
                }

        # Every careers-page role we hold for this company, so a reader can see
        # what the match was made against rather than trusting the verdict.
        payload["career_page_roles"] = [
            {"job_title": r["job_title"], "career_url": r["job_url"]}
            for r in conn.execute(
                "SELECT job_title, job_url FROM job_listings "
                "WHERE source = 'career_page' AND company_name_normalized = ? "
                "ORDER BY job_title",
                [row["company_name_normalized"]],
            )
        ]
        payload["signal_weights"] = {s: SIGNALS[s] for s in payload["signals"]}
        return payload


@app.get("/api/collectors")
def collectors() -> list[dict[str, Any]]:
    """Every configured collector and its last run. §0.4 wants the c_* IDs on
    screen, not just claimed in a README."""
    doc = load_collectors()
    with db() as conn:
        runs = {
            r["collector_id"]: dict(r)
            for r in conn.execute(
                "SELECT collector_id, MAX(completed_at) completed_at, status, "
                "rows_returned, rows_rejected, target_url FROM scrape_runs "
                "GROUP BY collector_id"
            )
        }
        # Keyed by (source, collector_id), not by collector_id alone: two
        # collector keys can share one c_* — the board firehose and the
        # per-company board search do — and counting by ID alone showed each
        # of them the other's rows as well.
        stored = {
            (r["source"], r["collector_id"]): r["n"]
            for r in conn.execute(
                "SELECT source, collector_id, COUNT(*) n FROM job_listings "
                "WHERE collector_id IS NOT NULL GROUP BY source, collector_id"
            )
        }

    out = []
    for collector in doc.get("collectors") or []:
        cid = collector.get("collector_id")
        last = runs.get(cid, {})
        out.append({
            "key": collector["key"],
            "kind": collector["kind"],
            "collector_id": cid,
            "created": bool(cid),
            "enabled": collector.get("enabled", True),
            "targets": len(collector.get("targets") or []),
            "listings_stored": stored.get((collector.get("source"), cid), 0) if cid else 0,
            "last_run_at": last.get("completed_at"),
            "last_status": last.get("status"),
            "last_rows": last.get("rows_returned"),
            "last_rejected": last.get("rows_rejected"),
        })
    return out


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    """What the numbers mean, so the UI never hardcodes them."""
    with db() as conn:
        counts = conn.execute(
            "SELECT COALESCE(s.career_page_checked, 0) checked, COUNT(*) n "
            "FROM job_listings l LEFT JOIN ghost_scores s ON s.listing_id = l.id "
            "WHERE l.source != 'career_page' GROUP BY 1"
        ).fetchall()
    tally = {int(r["checked"]): r["n"] for r in counts}
    return {
        "signals": SIGNALS,
        "bands": [{"min": lo, "max": hi, "label": label} for lo, hi, label in BANDS],
        "assessed": tally.get(1, 0),
        "not_assessed": tally.get(0, 0),
    }


class TriggerRequest(BaseModel):
    key: str
    company: str | None = None
    sync: bool = False


@app.post("/api/scrape/trigger")
def trigger(request: TriggerRequest) -> JSONResponse:
    """Run a collector now, ingest what it returns, re-score. §0.4.

    The company guard applies here exactly as it does in a CLI sweep: a
    triggered run is not a licence to attribute rows to whoever was asked for.
    """
    doc = load_collectors()
    matches = [c for c in (doc.get("collectors") or []) if c["key"] == request.key]
    if not matches:
        raise HTTPException(404, f"no collector {request.key!r}")
    collector = matches[0]
    if not collector.get("collector_id"):
        raise HTTPException(409, f"{request.key} has no collector_id yet")

    targets = collector.get("targets") or []
    if request.company:
        targets = [t for t in targets if t.get("slug") == request.company]
        if not targets:
            raise HTTPException(404, f"no target {request.company!r} in {request.key}")
    urls = [t["url"] for t in targets if t.get("url")]
    if not urls:
        raise HTTPException(409, f"{request.key} has no target URLs")

    source = collector["source"] + (f"_{request.company}" if request.company else "")
    try:
        result = run_collector(
            collector["collector_id"], urls, source, sync=request.sync
        )
    except BdataError as exc:
        raise HTTPException(502, str(exc)) from exc

    target = targets[0]
    with db() as conn:
        ingested = ingest_rows(
            conn, result.rows, source=collector["source"],
            collector_id=result.collector_id,
            company=target.get("company"), slug=target.get("slug"),
            company_from_target=collector["kind"] == "career",
        )
        record_run(
            conn, source, result.collector_id, urls[0], result.status,
            result.rows_returned, ingested.rejected_company,
            result.started_at, result.completed_at,
            raw_path=str(result.snapshot_path) if result.snapshot_path else None,
        )
        scored, unassessed = score_all(conn)

    return JSONResponse({
        "collector_id": result.collector_id,
        "status": result.status,
        "rows_returned": result.rows_returned,
        "accepted": ingested.accepted,
        "rejected_not_at_company": ingested.rejected_company,
        "rejected_names": sorted(set(ingested.rejected_names)),
        "snapshot": str(result.snapshot_path) if result.snapshot_path else None,
        "duration_s": result.duration_s,
        "assessed_total": scored,
        "not_assessed_total": unassessed,
        "error": result.error,
    })


if WEB_DIR.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
