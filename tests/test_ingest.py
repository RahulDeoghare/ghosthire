"""Ingest tests.

The guard is the subject. Everything else here exists to prove the guard does
not achieve its safety by dropping good rows too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghosthire import db, ingest

ROOT = Path(__file__).resolve().parent.parent
RAZORPAY_BOARD = ROOT / "data/snapshots/20260823T070349Z_board-company-razorpay.json"
RAZORPAY_CAREER = ROOT / "data/snapshots/20260821T183126Z_career-razorpay.json"
POSTMAN_FIXTURE = ROOT / "data/fixtures/board_postman_false_positives.json"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init(c)
    return c


def _rows(path):
    payload = json.loads(path.read_text())
    return payload["data"] if isinstance(payload, dict) else payload


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------

def test_rows_not_at_the_target_company_never_reach_the_database(conn):
    """The failure mode that does real-world harm.

    /jobs/keywords-postman is a full-text search. All three rows mention
    Postman and none are at Postman. Ingested under Postman they would match
    nothing on Postman's careers page and be published as high-confidence
    ghost jobs at a named real company.
    """
    result = ingest.ingest_rows(
        conn, _rows(POSTMAN_FIXTURE), source="board_company",
        company="Postman", slug="postman", collector_id="c_test",
    )

    assert result.accepted == 0
    assert result.rejected_company == 3
    assert conn.execute("SELECT COUNT(*) FROM job_listings").fetchone()[0] == 0
    # The names are kept so an operator can see WHY a target sheds its rows.
    assert set(result.rejected_names) == {"Stayin Bangalore", "Stitch", "The ProEducator"}


def test_the_drop_count_is_reported_not_swallowed(conn):
    """A target that discards most of its rows is telling you the keyword is
    ambiguous. Silently returning zero rows looks identical to a company that
    simply is not hiring."""
    result = ingest.ingest_rows(
        conn, _rows(POSTMAN_FIXTURE), source="board_company",
        company="Postman", slug="postman",
    )
    assert "NOT at postman" in result.summary()
    assert "Stitch" in result.summary()


def test_a_board_row_naming_no_employer_is_rejected_not_guessed(conn):
    result = ingest.ingest_rows(
        conn, [{"job_title": "Backend Engineer", "job_url": "https://x.test/1"}],
        source="board_company", company="Razorpay", slug="razorpay",
    )
    assert result.accepted == 0
    assert result.rejected_company == 1


def test_the_guard_does_not_reject_genuine_rows(conn):
    """The live Razorpay board snapshot: all three rows are really Razorpay."""
    result = ingest.ingest_rows(
        conn, _rows(RAZORPAY_BOARD), source="board_company",
        company="Razorpay", slug="razorpay", collector_id="c_mt1senswibym6o5va",
    )
    assert result.rejected_company == 0
    assert result.accepted == 3


# --------------------------------------------------------------------------
# career pages supply the company; the board never may
# --------------------------------------------------------------------------

def test_career_rows_take_their_company_from_the_page_they_came_from(conn):
    """A careers page belongs to one company, so its rows need not repeat it.
    That fallback is a separate switch from the board path on purpose."""
    result = ingest.ingest_rows(
        conn, _rows(RAZORPAY_CAREER), source="career_page",
        company="Razorpay", slug="razorpay", company_from_target=True,
        collector_id="c_mt1s6jit3zcyg7dlw",
    )
    assert result.accepted == 25
    names = {r[0] for r in conn.execute(
        "SELECT DISTINCT company_name_normalized FROM job_listings")}
    assert names == {"razorpay"}


def test_the_career_fallback_is_not_available_to_board_rows(conn):
    """If the board could fall back to the target company, the guard would be
    bypassed by any row that simply omitted its employer."""
    rows = [{"job_title": "Ghost Role", "job_url": "https://x.test/9"}]
    board = ingest.ingest_rows(conn, rows, source="board_company",
                               company="Postman", slug="postman")
    career = ingest.ingest_rows(conn, rows, source="career_page",
                                company="Postman", slug="postman",
                                company_from_target=True)
    assert board.accepted == 0 and board.rejected_company == 1
    assert career.accepted == 1


# --------------------------------------------------------------------------
# upsert behaviour
# --------------------------------------------------------------------------

def test_reingesting_the_same_snapshot_observes_rather_than_duplicates(conn):
    kw = dict(source="board_company", company="Razorpay", slug="razorpay")
    first = ingest.ingest_rows(conn, _rows(RAZORPAY_BOARD), **kw)
    second = ingest.ingest_rows(conn, _rows(RAZORPAY_BOARD), **kw)

    assert first.inserted == 3 and second.inserted == 0
    assert second.updated == 3
    assert conn.execute("SELECT COUNT(*) FROM job_listings").fetchone()[0] == 3
    counts = {r[0] for r in conn.execute(
        "SELECT observation_count FROM job_listings")}
    assert counts == {2}


def test_a_row_without_a_url_is_not_stored(conn):
    """UNIQUE(source, job_url) is the listing's identity, and the URL is also
    the evidence link a reader clicks to check us."""
    result = ingest.ingest_rows(
        conn, [{"company_name": "Razorpay", "job_title": "Backend Engineer"}],
        source="board_company", company="Razorpay", slug="razorpay",
    )
    assert result.skipped_no_url == 1
    assert result.accepted == 0


def test_dates_are_stored_with_their_confidence(conn):
    ingest.ingest_rows(conn, _rows(RAZORPAY_BOARD), source="board_company",
                       company="Razorpay", slug="razorpay")
    rows = conn.execute(
        "SELECT date_posted, date_posted_confidence FROM job_listings").fetchall()
    for date_posted, confidence in rows:
        assert confidence in ("exact", "relative", "absent")
        # 'absent' must mean NULL, never a fabricated date.
        assert (date_posted is None) == (confidence == "absent")


def test_every_stored_row_carries_its_collector_id(conn):
    """Provenance: any number in the dashboard traces to the collector that
    produced it."""
    ingest.ingest_rows(conn, _rows(RAZORPAY_BOARD), source="board_company",
                       company="Razorpay", slug="razorpay",
                       collector_id="c_mt1senswibym6o5va")
    ids = {r[0] for r in conn.execute("SELECT collector_id FROM job_listings")}
    assert ids == {"c_mt1senswibym6o5va"}
