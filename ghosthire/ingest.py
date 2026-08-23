"""Snapshot JSON -> normalized rows -> upsert.

**The company guard is the reason this module is careful.** The board is
searched per company via a full-text keyword URL, not a company filter. A
listing that merely *mentions* the target — "Postman" the API tool in a job
description — comes back indistinguishable from one that is *at* Postman. File
it under Postman, find no counterpart on Postman's careers page, and the
pipeline publishes "Postman is advertising a ghost job" with two
authoritative-looking URLs beside it.

That is a false public accusation against a named company, produced with
maximum apparent rigour, and it is not visibly broken from the inside. So rows
are attributed only when the row's own employer matches the company it is
being filed under, and everything dropped is counted rather than discarded
quietly: a target that sheds most of its rows is telling you its keyword is
ambiguous, which is exactly what "postman" is.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .fields import pick
from .normalize import (
    company_matches,
    normalize_company,
    normalize_location,
    normalize_title,
    parse_posted_date,
)


@dataclass
class IngestResult:
    """What one snapshot did to the database, including what it refused."""

    source: str
    slug: str | None = None
    inserted: int = 0
    updated: int = 0
    rejected_company: int = 0
    skipped_no_title: int = 0
    skipped_no_url: int = 0
    rejected_names: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return self.inserted + self.updated

    def summary(self) -> str:
        parts = [f"{self.accepted} accepted ({self.inserted} new, {self.updated} seen again)"]
        if self.rejected_company:
            names = ", ".join(sorted(set(self.rejected_names))[:3])
            parts.append(f"{self.rejected_company} NOT at {self.slug} ({names})")
        if self.skipped_no_title:
            parts.append(f"{self.skipped_no_title} without a title")
        if self.skipped_no_url:
            parts.append(f"{self.skipped_no_url} without a URL")
        return " · ".join(parts)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ingest_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    source: str,
    collector_id: str | None = None,
    company: str | None = None,
    slug: str | None = None,
    company_from_target: bool = False,
    seen_at: str | None = None,
) -> IngestResult:
    """Upsert collector rows.

    ``company`` is the employer this snapshot is filed under. When it is set
    and ``company_from_target`` is False — the board case — every row must name
    that same employer or it is rejected.

    ``company_from_target=True`` is the careers-page case: those rows carry no
    employer because the page itself belongs to one, so the target supplies it.
    The two are kept as separate switches on purpose. Letting the board fall
    back to the target would re-open exactly the hole this module exists to
    close.
    """
    seen_at = seen_at or _utcnow()
    result = IngestResult(source=source, slug=slug)

    for row in rows:
        raw_company = pick(row, "company")

        if company_from_target:
            # The careers page belongs to the company; rows need not repeat it.
            name = raw_company or (company or "")
        else:
            if not raw_company:
                # A board row that names no employer cannot be attributed to
                # one. Guessing here is the accusation risk.
                result.rejected_company += 1
                result.rejected_names.append("(no company named)")
                continue
            if company and not company_matches(raw_company, company):
                result.rejected_company += 1
                result.rejected_names.append(raw_company)
                continue
            # Once the guard has confirmed the row is at the target, file it
            # under the target's canonical name. The board writes the same
            # employer three ways — "NoBroker.com", "NoBroker Support", the
            # full legal name — which normalize differently and would be stored
            # as three companies, none of them matching the careers page we
            # hold for NoBroker. The raw string stays in raw_json.
            name = company if company else raw_company

        title = pick(row, "title")
        if not name or not title:
            result.skipped_no_title += 1
            continue

        url = pick(row, "url")
        if not url:
            # UNIQUE(source, job_url) is how a re-run recognises a listing it
            # has seen before. Without a URL there is no identity and no
            # evidence link for a reader to check, so the row is not stored.
            result.skipped_no_url += 1
            continue

        location = pick(row, "location")
        posted_iso, posted_confidence = parse_posted_date(pick(row, "posted"))

        existing = conn.execute(
            "SELECT id, observation_count FROM job_listings "
            "WHERE source = ? AND job_url = ?",
            (source, url),
        ).fetchone()

        if existing:
            # The newest observation is the listing's current state, so the
            # stored fields are refreshed rather than only counted.
            #
            # Not bookkeeping: 33 of 50 rows in one rebuild kept the mangled
            # company names from a snapshot taken before the collector was
            # healed, because a later clean scrape of the same job_url bumped
            # the counter and left the old text in place. A stale name here is
            # the name the product would publish an accusation against.
            conn.execute(
                """
                UPDATE job_listings
                   SET last_seen = ?, observation_count = observation_count + 1,
                       is_active = 1, raw_json = ?, collector_id = ?,
                       company_name = ?, company_name_normalized = ?,
                       job_title = ?, job_title_normalized = ?,
                       location = ?, location_normalized = ?,
                       date_posted = ?, date_posted_confidence = ?,
                       salary_range = ?
                 WHERE id = ?
                """,
                (seen_at, json.dumps(row, ensure_ascii=False), collector_id,
                 name, normalize_company(name),
                 title, normalize_title(title),
                 location, normalize_location(location),
                 posted_iso, posted_confidence, pick(row, "salary"),
                 existing["id"]),
            )
            result.updated += 1
            continue

        conn.execute(
            """
            INSERT INTO job_listings (
                source, source_company_slug, company_name, company_name_normalized,
                job_title, job_title_normalized, location, location_normalized,
                date_posted, date_posted_confidence, job_url, salary_range,
                first_seen, last_seen, observation_count, is_active,
                collector_id, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)
            """,
            (
                source, slug, name, normalize_company(name),
                title, normalize_title(title), location,
                normalize_location(location),
                posted_iso, posted_confidence, url, pick(row, "salary"),
                seen_at, seen_at, collector_id,
                json.dumps(row, ensure_ascii=False),
            ),
        )
        result.inserted += 1

    conn.commit()
    return result
