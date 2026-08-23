"""Rebuild the database from the snapshot archive, then score it.

`data/snapshots/*.json` is the source of truth; this module is what makes the
"a reviewer can rebuild our numbers without spending a credit" claim true.

Snapshots record which collector produced them but not which company they were
filed under, so that is recovered from the filename, which `snapshot_path`
builds from the source label the run was given. Anything unrecognised is
skipped loudly rather than guessed at — attributing a snapshot to the wrong
company is the same accusation risk the ingest guard exists to stop.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bdata import extract_errors, extract_rows, load_collectors
from .db import iter_snapshots, record_run
from .ingest import ingest_rows
from .match import match_listing
from .score import score_listing

# `20260823T070349Z_board-company-razorpay.json` -> `board-company-razorpay`
_NAME = re.compile(r"^\d{8}T\d{6}Z_(.+)\.json$")


@dataclass
class SnapshotPlan:
    """How one archived file should be ingested."""

    path: Path
    source: str
    slug: str | None = None
    company: str | None = None
    company_from_target: bool = False
    collector_id: str | None = None
    collector_name: str | None = None


@dataclass
class RebuildReport:
    ingested: int = 0
    runs: int = 0
    rejected: int = 0
    scored: int = 0
    unassessed: int = 0
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def collector_history() -> list[tuple[str, str, str]]:
    """(created_at, name, collector_id) for every collector the archive records.

    Read from the `scraper create` envelopes rather than from config, because
    config only knows the collector that is current. The first career collector
    was built against a landing page, failed on three targets and was replaced —
    and without this its failures would be filed under the ID of the collector
    that replaced it, which is the opposite of provenance.
    """
    out: list[tuple[str, str, str]] = []
    for path, payload in iter_snapshots(warn=False):
        if not isinstance(payload, dict) or not payload.get("collector_id"):
            continue
        created = payload.get("created_at") or ""
        out.append((created, payload.get("name") or "", payload["collector_id"]))
    return sorted(out)


def _collector_at(name: str, when: str, history: list[tuple[str, str, str]]) -> str | None:
    """Which collector of this name existed when the snapshot was taken."""
    best = None
    for created, cname, cid in history:
        if cname != name:
            continue
        # created_at is ISO with a Z; the snapshot stamp is 20260820T172031Z.
        stamp = created.replace("-", "").replace(":", "")[:15]
        if stamp <= when:
            best = cid
    return best


def _collector_id_for(source: str, doc: dict[str, Any]) -> str | None:
    """Which `c_*` produced a snapshot with this source label.

    A `scraper run` response is a bare array, so unlike a create envelope it
    carries no collector ID of its own — it is recovered from the config
    instead. §0.4 requires the ID on every stored row, and a number in the
    dashboard that cannot be traced back to a collector should not be there.

    Snapshots from the superseded landing-page collector are unaffected: they
    hold an outbound link and two crawler errors, none of which survive
    `extract_rows` or the title check, so none of them become listings.
    """
    for collector in doc.get("collectors") or []:
        if collector.get("source") == source and collector.get("collector_id"):
            return collector["collector_id"]
    return None


def _collector_name_for(source: str, doc: dict[str, Any]) -> str | None:
    for collector in doc.get("collectors") or []:
        if collector.get("source") == source and collector.get("collector_id"):
            return collector.get("name")
    return None


def _company_for(slug: str, doc: dict[str, Any]) -> str | None:
    for collector in doc.get("collectors") or []:
        for target in collector.get("targets") or []:
            if target.get("slug") == slug and target.get("company"):
                return target["company"]
    return None


def plan_snapshot(path: Path, doc: dict[str, Any]) -> SnapshotPlan | None:
    """Decide how to read one archived file, or decline to read it."""
    match = _NAME.match(path.name)
    if not match:
        return None
    label = match.group(1)

    # `scraper create` envelopes describe a collector, not listings.
    if label.startswith("create-") or label.startswith("create_"):
        return None

    for prefix, source, from_target in (
        ("board-company-", "board_company", False),
        ("career-", "career_page", True),
        ("fork-", "career_page", True),
    ):
        if label.startswith(prefix):
            slug = label[len(prefix):]
            # Strip the collision suffix snapshot_path adds (`_2`, `_3`).
            slug = re.sub(r"[-_]\d+$", "", slug)
            company = _company_for(slug, doc)
            if not company:
                return None
            return SnapshotPlan(path, source, slug, company, from_target,
                                _collector_id_for(source, doc),
                                _collector_name_for(source, doc))

    if label.startswith("internshala"):
        # The front-page sweep: every row names its own employer and there is
        # no target company, so the guard has nothing to compare against and
        # each row stands on its own.
        return SnapshotPlan(path, "internshala",
                            collector_id=_collector_id_for("internshala", doc),
                            collector_name=_collector_name_for("internshala", doc))
    return None


def rebuild(conn: sqlite3.Connection, quiet: bool = False) -> RebuildReport:
    """Rebuild every listing and score from the archive.

    Starts by clearing both tables. "Delete the database, run this, get the
    same numbers" is the provenance claim, and re-running against a populated
    database instead accumulated observation_count — 6 after three rebuilds —
    so the same archive produced a different database each time.

    ghost_scores goes first: it holds foreign keys into job_listings.
    """
    doc = load_collectors()
    history = collector_history()
    report = RebuildReport()

    conn.execute("DELETE FROM ghost_scores")
    conn.execute("DELETE FROM job_listings")
    conn.execute("DELETE FROM scrape_runs")
    conn.commit()

    for path, payload in iter_snapshots():
        plan = plan_snapshot(path, doc)
        if plan is None:
            report.skipped.append(path.name)
            continue

        rows = extract_rows(payload)
        errors = extract_errors(payload)

        # Every archived response is a run that happened, and a run that
        # returned nothing is still evidence — the collector panel showing
        # "no run recorded" beside a collector with 50 stored rows was an
        # artefact of only the live trigger writing this table.
        stamp = path.name[:16]
        status = "failed" if (errors and not rows) else "success" if rows else "partial"
        ran_as = _collector_at(plan.collector_name or "", stamp[:15], history) \
            or plan.collector_id or "unknown"
        record_run(
            conn, plan.source, ran_as,
            (plan.company or plan.slug or ""), status, len(rows),
            started_at=stamp, completed_at=stamp, raw_path=str(path.name),
        )
        report.runs += 1

        if not rows:
            continue

        # The envelope wins when present; otherwise the config says which
        # collector owns this source.
        collector_id = plan.collector_id
        if isinstance(payload, dict) and payload.get("collector_id"):
            collector_id = payload["collector_id"]

        result = ingest_rows(
            conn, rows, source=plan.source, collector_id=collector_id,
            company=plan.company, slug=plan.slug,
            company_from_target=plan.company_from_target,
        )
        report.ingested += result.accepted
        report.rejected += result.rejected_company
        if result.rejected_company and not quiet:
            print(f"  {path.name}: {result.summary()}", file=sys.stderr)

    report_scores = score_all(conn)
    report.scored = report_scores[0]
    report.unassessed = report_scores[1]
    return report


def career_rows_for(conn: sqlite3.Connection, company_normalized: str) -> list[Any]:
    return conn.execute(
        "SELECT * FROM job_listings WHERE source = 'career_page' "
        "AND company_name_normalized = ?",
        (company_normalized,),
    ).fetchall()


def score_all(conn: sqlite3.Connection) -> tuple[int, int]:
    """Match and score every board listing. Returns (scored, unassessed).

    `career_page_checked` is set only when we hold at least one careers-page
    row for that company. A zero-row careers result is deliberately NOT treated
    as "this company lists nothing": our extractor returns zero rows on
    SmartRecruiters and Workday without erroring, and reading that as evidence
    would mark every listing for those companies as a ghost.
    """
    conn.execute("DELETE FROM ghost_scores")
    scored = unassessed = 0

    listings = conn.execute(
        "SELECT * FROM job_listings WHERE source != 'career_page'"
    ).fetchall()

    # A repost is the same role at the same employer advertised again under a
    # fresh URL. It is weak evidence on its own — a company may genuinely open
    # the role twice — so it is a 15-point signal rather than a verdict, and it
    # only counts where the URLs actually differ.
    reposts = {
        (r["company_name_normalized"], r["job_title_normalized"])
        for r in conn.execute(
            """SELECT company_name_normalized, job_title_normalized
                 FROM job_listings
                WHERE source != 'career_page' AND job_title_normalized != ''
                GROUP BY company_name_normalized, job_title_normalized
               HAVING COUNT(DISTINCT job_url) > 1""")
    }

    for listing in listings:
        career = career_rows_for(conn, listing["company_name_normalized"])
        checked = len(career) > 0

        result = match_listing(listing["job_title"], listing["company_name"], career)
        score = score_listing(
            career_page_checked=checked,
            matched=result.matched,
            match_confidence=result.confidence if result.matched else None,
            matched_career_listing_id=(
                result.career_row["id"] if result.matched and result.career_row is not None
                and "id" in result.career_row.keys() else None
            ),
            date_posted=listing["date_posted"],
            date_posted_confidence=listing["date_posted_confidence"] or "absent",
            career_page_role_count=len(career) if checked else None,
            repost_detected=(listing["company_name_normalized"],
                             listing["job_title_normalized"]) in reposts,
            reason=result.reason,
        )

        conn.execute(
            """
            INSERT INTO ghost_scores (listing_id, ghost_score, signals,
                                      matched_career_listing_id, match_confidence,
                                      career_page_checked)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (listing["id"], score.ghost_score,
             json.dumps(score.signals), score.matched_career_listing_id,
             score.match_confidence, score.career_page_checked),
        )
        if score.career_page_checked:
            scored += 1
        else:
            unassessed += 1

    conn.commit()
    return scored, unassessed
