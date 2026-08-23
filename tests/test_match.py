"""Matching tests.

Every row of plan §3A.4's measured table is asserted here. Those numbers were
re-measured on rapidfuzz 3.14.5 and reproduce exactly, so they are pinned: if a
dependency bump moves them, the design assumption behind the level gate has
changed and someone needs to know.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rapidfuzz import fuzz

from ghosthire.match import FUZZ_THRESHOLD, match_listing

ROOT = Path(__file__).resolve().parent.parent
CAREER = ROOT / "data/snapshots/20260821T183126Z_career-razorpay.json"


def career_rows():
    return [{"job_title": r["job_title"], "company_name": "Razorpay"}
            for r in json.loads(CAREER.read_text()) if r.get("job_title")]


# --------------------------------------------------------------------------
# plan §3A.4, measured. The two marked DIFF at 100.0 are why token_set_ratio
# is banned outright.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,tset,tsort,ratio,wratio,same", [
    ("front end engineer", "front end engineer", 100.0, 100.0, 100.0, 100.0, True),
    ("senior back end engineer", "back end engineer", 100.0, 82.9, 82.9, 95.0, False),
    ("software engineer intern", "software engineer", 100.0, 82.9, 82.9, 95.0, False),
    ("software development engineer 1", "software development engineer 2",
     96.8, 96.8, 96.8, 96.8, False),
    ("product designer", "product manager", 71.0, 71.0, 71.0, 71.0, False),
    ("data scientist", "data engineer", 59.3, 59.3, 59.3, 59.3, False),
])
def test_scorer_table_reproduces(a, b, tset, tsort, ratio, wratio, same):
    assert round(fuzz.token_set_ratio(a, b), 1) == tset
    assert round(fuzz.token_sort_ratio(a, b), 1) == tsort
    assert round(fuzz.ratio(a, b), 1) == ratio
    assert round(fuzz.WRatio(a, b), 1) == wratio
    if not same:
        # The point of the table: token_set_ratio calls three of these five
        # different pairs a perfect or near-perfect match.
        assert tset >= tsort


# --------------------------------------------------------------------------
# the level gate — runs BEFORE the fuzz, because no scorer can do this
# --------------------------------------------------------------------------

@pytest.mark.parametrize("board,career", [
    ("SDE-1", "SDE-2"),
    ("Software Development Engineer 1", "Software Development Engineer 2"),
    ("Senior Backend Engineer", "Junior Backend Engineer"),
    ("Engineer II", "Engineer III"),
])
def test_a_level_mismatch_is_never_a_match(board, career):
    """96.8 on every scorer. If the gate ran after the fuzz, these would all
    be reported as the same job."""
    result = match_listing(board, "Razorpay", [{"job_title": career,
                                                "company_name": "Razorpay"}])
    assert not result.matched
    assert "level" in result.reason


def test_the_same_level_still_matches():
    result = match_listing("SDE-2 Backend", "Razorpay",
                           [{"job_title": "Software Development Engineer 2, Back End",
                             "company_name": "Razorpay"}])
    assert result.matched
    assert result.score == 100.0


def test_a_level_stated_on_one_side_only_matches_with_less_confidence():
    """A careers page routinely omits a level the board spells out. That is
    ambiguity, not a conflict — match, but say we are less sure."""
    result = match_listing("Backend Engineer II", "Razorpay",
                           [{"job_title": "Backend Engineer", "company_name": "Razorpay"}])
    assert result.matched and result.ambiguous_level
    assert result.confidence < result.score / 100.0


# --------------------------------------------------------------------------
# never fuzz the company
# --------------------------------------------------------------------------

def test_a_role_at_another_company_is_not_a_match():
    result = match_listing("Backend Engineer", "Razorpay",
                           [{"job_title": "Backend Engineer", "company_name": "Postman"}])
    assert not result.matched


def test_no_career_data_is_reported_as_unassessed_not_as_a_ghost():
    """Absence of evidence is not evidence of absence. The caller must be able
    to tell these apart, because one is a finding and the other is silence."""
    result = match_listing("Backend Engineer", "Razorpay", [])
    assert not result.matched
    assert "no careers-page data" in result.reason


# --------------------------------------------------------------------------
# against the real Razorpay pair
# --------------------------------------------------------------------------

def test_the_live_true_negative():
    """'Associate, Startup Accounts' is on the board AND the careers page. The
    common case, and the product has to say so rather than only find ghosts."""
    result = match_listing("Associate, Startup Accounts", "Razorpay", career_rows())
    assert result.matched and result.score == 100.0


@pytest.mark.parametrize("title", [
    "Associate Technical Program Manager",
    "Associate Manager, Key Accounts Management",
])
def test_the_live_ghost_candidates(title):
    result = match_listing(title, "Razorpay", career_rows())
    assert not result.matched
    assert result.score < FUZZ_THRESHOLD


def test_the_near_miss_that_token_set_ratio_would_have_passed():
    """The strongest single argument for the gate-then-fuzz design, and it is
    from our own live data rather than a textbook."""
    board = "Associate Manager, Key Accounts Management"
    career = "Associate Manager, Startup Accounts"

    assert round(fuzz.token_set_ratio(board.lower(), career.lower()), 1) == 87.1
    assert not match_listing(board, "Razorpay", career_rows()).matched
