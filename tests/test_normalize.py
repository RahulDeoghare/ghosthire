"""Normalization tests.

Two properties carry the project:

- the same job written two ways must compare equal, or the matcher invents
  ghosts wholesale;
- a row must never be attributed to a company it is not at, or the product
  publishes a false accusation about a real employer.
"""

from __future__ import annotations

from datetime import date

import pytest
from rapidfuzz import fuzz

from ghosthire.normalize import (
    company_matches,
    extract_level,
    normalize_company,
    normalize_location,
    normalize_title,
    parse_posted_date,
    split_locations,
)


# --------------------------------------------------------------------------
# the case plan §3A.4(c) rests on
# --------------------------------------------------------------------------

def test_normalization_turns_33_into_100():
    """'SDE-2 Backend' and 'Software Development Engineer 2, Back End' are the
    same job. Raw they score 33.3, which would be reported as a ghost."""
    a, b = "SDE-2 Backend", "Software Development Engineer 2, Back End"

    assert round(fuzz.token_sort_ratio(a, b), 1) == 33.3
    assert normalize_title(a) == normalize_title(b)
    assert fuzz.token_sort_ratio(normalize_title(a), normalize_title(b)) == 100.0


def test_token_set_ratio_is_unsafe_on_our_own_live_data():
    """§3A.4(a) is not a style preference, and this pair is from our own board
    snapshot: two genuinely different Razorpay roles that token_set_ratio rates
    above an 85 gate and token_sort_ratio correctly rejects."""
    board = "Associate Manager, Key Accounts Management"
    career = "Associate Manager, Startup Accounts"

    assert round(fuzz.token_set_ratio(board.lower(), career.lower()), 1) == 87.1
    assert round(fuzz.token_sort_ratio(board.lower(), career.lower()), 1) == 72.7


# --------------------------------------------------------------------------
# companies
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "Razorpay",
    "Razorpay Software Private Limited",
    "RAZORPAY PVT LTD",
    "Razorpay Technologies",
])
def test_company_suffixes_collapse_to_one_form(raw):
    assert normalize_company(raw) == "razorpay"


def test_parenthesised_asides_are_dropped():
    """From the live board: the same employer, with a head office bolted on."""
    assert normalize_company("TELUS International AI Inc. (Las Vegas, United States)") \
        == normalize_company("TELUS International AI")


def test_a_company_made_only_of_noise_words_keeps_its_name():
    """Stripping every generic word would normalize 'Global Systems' to the
    empty string, and every such company would then collide with every other."""
    assert normalize_company("Global Systems") != ""
    assert normalize_company("Global Systems") != normalize_company("India Solutions")


# --------------------------------------------------------------------------
# the guard — the failure mode that does real-world harm
# --------------------------------------------------------------------------

def test_guard_accepts_the_same_company_written_differently():
    assert company_matches("Razorpay Software Private Limited", "Razorpay")


@pytest.mark.parametrize("row_company", ["Stitch", "Stayin Bangalore", "The ProEducator"])
def test_guard_rejects_full_text_search_false_positives(row_company):
    """Live case: /jobs/keywords-postman is a full-text search, not a company
    filter. These three came back for "postman" because their descriptions
    mention the API tool. Filing them under Postman and finding no counterpart
    on Postman's careers page would publish a fake accusation."""
    assert not company_matches(row_company, "Postman")


@pytest.mark.parametrize("row", ["NoBroker.com", "NoBroker Support", "NoBroker India"])
def test_a_brandless_trailing_token_is_still_the_same_company(row):
    """Rejecting these lost genuine NoBroker listings from a live board run."""
    assert company_matches(row, "NoBroker")


@pytest.mark.parametrize("row", ["CRED Avenue", "CredAvenue", "Accredian",
                                 "AMOLAKSHAYA TRADE AND CREDIT Private Limited"])
def test_a_trailing_token_that_carries_a_brand_is_a_different_company(row):
    """CredAvenue is not CRED. This is the asymmetry the exception turns on:
    a trailing token with no brand of its own is tolerated, one that names a
    different business is not. All four of these came back from a live search
    for the keyword 'cred'."""
    assert not company_matches(row, "CRED")


def test_a_distinct_product_line_is_not_folded_into_the_parent():
    """'Payment Gateway' names a business, not a decoration, so it stays out
    rather than being attributed to Paytm on our say-so."""
    assert not company_matches("Paytm Payment Gateway", "Paytm")


@pytest.mark.parametrize("a,b", [(None, "Postman"), ("Stitch", None), ("", "Postman")])
def test_guard_refuses_to_match_on_missing_information(a, b):
    assert not company_matches(a, b)


# --------------------------------------------------------------------------
# levels — the gate no scorer can replace
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("SDE-1", (None, 1)),
    ("SDE-2", (None, 2)),
    ("Software Development Engineer 2", (None, 2)),
    ("Engineer II", (None, 2)),
    ("L3 Engineer", (None, 3)),
    ("Senior Backend Engineer", ("senior", None)),
    ("Junior Accountant", ("junior", None)),
    ("Backend Engineer", (None, None)),
])
def test_level_extraction(title, expected):
    assert extract_level(title) == expected


def test_sde_1_and_sde_2_differ_only_in_the_level():
    """96.8 on every scorer — one character carries all the meaning, so the
    gate has to catch it before any fuzzing happens."""
    assert round(fuzz.token_sort_ratio("software development engineer 1",
                                       "software development engineer 2"), 1) == 96.8
    assert extract_level("SDE-1")[1] != extract_level("SDE-2")[1]


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

def test_multi_valued_locations_are_split():
    """The board writes one role's cities as 'Delhi , Ghaziabad , Noida',
    space before each comma."""
    assert split_locations("Delhi , Ghaziabad , Noida") == ["delhi", "ghaziabad", "noida"]


def test_repeated_cities_collapse():
    assert split_locations("Bangalore, Bangalore, Bangalore") == ["bangalore"]


@pytest.mark.parametrize("raw", ["Work from home", "Remote", "WFH", "Bangalore (Hybrid)"])
def test_remote_forms(raw):
    out = normalize_location(raw)
    assert out == "remote" or out == "bangalore"


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def test_relative_dates_parse_with_their_confidence():
    today = date(2026, 8, 23)
    assert parse_posted_date("3 weeks ago", today) == ("2026-08-02", "relative")
    assert parse_posted_date("Posted 1 week ago", today) == ("2026-08-16", "relative")


def test_absolute_dates_are_marked_exact():
    assert parse_posted_date("2026-08-01")[1] == "exact"


@pytest.mark.parametrize("raw", ["Job", "Fresher Job", "", None, "   "])
def test_non_dates_are_absent_not_zero(raw):
    """28% of live board rows carry 'Job' or 'Fresher Job' in the date field.
    Reading those as a date, or as the epoch, would fabricate staleness."""
    assert parse_posted_date(raw) == (None, "absent")
