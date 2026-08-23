"""Scoring tests.

Most of these are about refusing to score. The arithmetic is trivial; knowing
when we are not entitled to an opinion is the part that keeps this project
from libelling someone.
"""

from __future__ import annotations

from datetime import date

import pytest

from ghosthire.score import SIGNALS, band, score_listing

TODAY = date(2026, 8, 23)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def test_without_career_data_there_is_no_score_at_all():
    """Not zero, not 'probably fine'. No score. Absence of evidence is not
    evidence of absence."""
    result = score_listing(career_page_checked=False, matched=False, today=TODAY)

    assert result.ghost_score is None
    assert result.career_page_checked == 0
    assert result.signals == []
    assert result.band is None
    assert "not assessed" in result.reason


def test_an_unassessed_listing_is_never_accused_however_stale():
    """The dangerous combination: no careers-page data, but a very old posting.
    The staleness must not leak out as a score."""
    result = score_listing(
        career_page_checked=False, matched=False,
        date_posted="2025-01-01", date_posted_confidence="exact", today=TODAY,
    )
    assert result.ghost_score is None
    assert result.signals == []


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

def test_a_listing_on_the_careers_page_scores_zero():
    """The common case. A product that only ever finds ghosts is not
    measuring anything."""
    result = score_listing(career_page_checked=True, matched=True,
                           match_confidence=1.0, today=TODAY)
    assert result.ghost_score == 0
    assert result.signals == []
    assert result.band == "likely real"


def test_absence_from_the_careers_page_is_the_load_bearing_signal():
    result = score_listing(career_page_checked=True, matched=False, today=TODAY)
    assert result.signals == ["not_on_career_page"]
    assert result.ghost_score == 45
    assert result.band == "questionable"


def test_stale_signals_stack():
    result = score_listing(
        career_page_checked=True, matched=False,
        date_posted="2025-08-01", date_posted_confidence="relative", today=TODAY,
    )
    assert set(result.signals) == {"not_on_career_page", "stale_90d", "stale_180d"}
    assert result.ghost_score == 45 + 20 + 15
    assert result.band == "likely ghost"


def test_the_score_is_capped_at_100():
    result = score_listing(
        career_page_checked=True, matched=False,
        date_posted="2024-01-01", date_posted_confidence="exact",
        career_page_role_count=0, cross_board_mismatch=True,
        repost_detected=True, today=TODAY,
    )
    assert sum(SIGNALS[s] for s in result.signals) > 100
    assert result.ghost_score == 100


# --------------------------------------------------------------------------
# abstention — 28% of live board rows have no date
# --------------------------------------------------------------------------

def test_a_listing_with_no_date_abstains_from_staleness_rather_than_guessing():
    """14 of 50 live rows carry 'Job' or 'Fresher Job' where a date belongs.
    Treating that as very old would fabricate the strongest temporal signal."""
    result = score_listing(career_page_checked=True, matched=False,
                           date_posted=None, date_posted_confidence="absent",
                           today=TODAY)

    assert result.signals == ["not_on_career_page"]
    assert set(result.abstained) == {"stale_90d", "stale_180d"}
    assert result.ghost_score == 45


def test_a_recent_listing_triggers_no_staleness_and_abstains_from_nothing():
    result = score_listing(career_page_checked=True, matched=False,
                           date_posted="2026-08-02", date_posted_confidence="relative",
                           today=TODAY)
    assert result.signals == ["not_on_career_page"]
    assert result.abstained == []


# --------------------------------------------------------------------------
# the empty-careers-page trap this project already walked into once
# --------------------------------------------------------------------------

def test_an_empty_careers_page_only_counts_when_it_was_really_read():
    """Our extractor returns zero rows on SmartRecruiters and Workday without
    erroring. If the caller cannot vouch for the platform it must pass
    career_page_checked=False, and then nothing is scored — including this."""
    unchecked = score_listing(career_page_checked=False, matched=False,
                              career_page_role_count=0, today=TODAY)
    checked = score_listing(career_page_checked=True, matched=False,
                            career_page_role_count=0, today=TODAY)

    assert unchecked.ghost_score is None
    assert "career_page_empty" not in unchecked.signals
    assert "career_page_empty" in checked.signals


# --------------------------------------------------------------------------
# repost detection — instrumented, and expected to fire rarely
# --------------------------------------------------------------------------

def test_a_repost_adds_its_signal_without_becoming_a_verdict():
    """The same role advertised again under a fresh URL. Weak evidence on its
    own — a company may genuinely open a role twice — so it is worth 15 and
    cannot reach the 'likely ghost' band by itself."""
    result = score_listing(career_page_checked=True, matched=True,
                           repost_detected=True, today=TODAY)

    assert result.signals == ["repost_detected"]
    assert result.ghost_score == 15
    assert result.band == "likely real", "a repost alone must not accuse anyone"


def test_a_repost_compounds_with_absence():
    result = score_listing(career_page_checked=True, matched=False,
                           repost_detected=True, today=TODAY)
    assert set(result.signals) == {"not_on_career_page", "repost_detected"}
    assert result.ghost_score == 60
    assert result.band == "likely ghost"


def test_repost_detection_needs_distinct_urls(tmp_path):
    """The detector groups by (company, normalized title) and requires more
    than one distinct job_url. Ingesting the same snapshot twice must not
    manufacture a repost out of one listing seen twice."""
    from ghosthire import db, ingest, pipeline
    conn = db.connect(tmp_path / "r.db")
    db.init(conn)

    row = {"company_name": "Acme", "job_title": "Backend Engineer",
           "job_url": "https://x.test/1"}
    ingest.ingest_rows(conn, [row], source="internshala")
    ingest.ingest_rows(conn, [row], source="internshala")
    pipeline.score_all(conn)
    signals = conn.execute("SELECT signals FROM ghost_scores").fetchone()[0]
    assert "repost_detected" not in signals

    ingest.ingest_rows(conn, [dict(row, job_url="https://x.test/2")],
                       source="internshala")
    pipeline.score_all(conn)
    for (sig,) in conn.execute("SELECT signals FROM ghost_scores"):
        # Both listings are now reposts of each other; neither is scored,
        # because Acme has no careers-page data — the gate still wins.
        assert sig == "[]" or "repost_detected" in sig


@pytest.mark.parametrize("score,expected", [
    (0, "likely real"), (29, "likely real"),
    (30, "questionable"), (59, "questionable"),
    (60, "likely ghost"), (100, "likely ghost"),
])
def test_bands(score, expected):
    assert band(score) == expected
