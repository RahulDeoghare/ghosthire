"""Turning scraped text into comparable values.

Everything the matcher does depends on this module getting two things right:

1. **The same job, written two ways, must normalize to the same string.**
   ``'SDE-2 Backend'`` and ``'Software Development Engineer 2, Back End'`` score
   33.3 raw and 100.0 normalized. Without that, the matcher reports real
   listings as absent from the careers page and manufactures ghosts wholesale.

2. **A row must belong to the company it is filed under.** The board is
   searched per company, and the board's search is full text, not a company
   filter — so a listing that merely *mentions* the company comes back looking
   exactly like one that is *at* the company. Filing it under that company and
   finding no careers-page counterpart publishes a fake accusation against a
   real employer. `company_matches` is the guard, and it runs before anything
   reaches the matcher.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

# --------------------------------------------------------------------------
# companies
# --------------------------------------------------------------------------

# Legal and filler suffixes. Stripped so "Razorpay Software Private Limited"
# and "Razorpay" are the same company, which §3A.4 step 1 requires to be an
# exact match rather than a fuzzy one.
COMPANY_SUFFIXES = (
    "private limited", "pvt ltd", "pvt. ltd.", "pvt limited", "private ltd",
    "limited", "ltd", "llp", "inc", "incorporated", "corp", "corporation",
    "company", "co", "plc", "gmbh", "sa", "bv", "nv", "ag", "pte",
)

# Generic descriptors that appear inconsistently between a board and a careers
# page. "Corteva Agriscience" / "Corteva Agri Science" is a real pair from our
# own board snapshot.
COMPANY_NOISE = (
    "technologies", "technology", "solutions", "software", "systems",
    "services", "labs", "global", "india", "group", "holdings", "ventures",
    "enterprises", "industries", "international",
)


def normalize_company(name: str | None) -> str:
    """A company's comparable form: lowercase, no suffixes, no punctuation."""
    if not name:
        return ""
    text = name.lower()
    # Drop parenthesised asides — "TELUS International AI Inc. (Las Vegas,
    # United States)" is the same employer as "TELUS International AI".
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = text.split()

    # Suffixes are multi-word, so strip from the end repeatedly.
    changed = True
    while changed and words:
        changed = False
        for suffix in COMPANY_SUFFIXES:
            parts = suffix.split()
            if len(words) > len(parts) and words[-len(parts):] == parts:
                words = words[: -len(parts)]
                changed = True
                break

    # Never let noise removal empty the name: "Global Systems" would otherwise
    # normalize to "", and every such company would collide with every other.
    kept = [w for w in words if w not in COMPANY_NOISE]
    return " ".join(kept or words)


# Tokens that may trail a company name without making it a different company.
# Deliberately short and boring: every addition here widens what counts as the
# same employer, and the cost of getting that wrong is an accusation aimed at
# the wrong company.
BRAND_SUFFIXES = frozenset({
    "com", "in", "io", "support", "careers", "jobs", "hiring", "recruitment",
    "official", "hq", "team", "india", "bharat",
})


def company_matches(row_company: str | None, target_company: str | None) -> bool:
    """Is this row actually *at* the target company?

    Never fuzzy — §3A.4 forbids fuzzing the company, and the cost of a loose
    match here is a public accusation against whichever company the row really
    belongs to. A miss drops one row; a false accept invents an employer.

    Exact on the normalized form, with one narrow exception: a trailing token
    that carries no brand of its own. "NoBroker.com" and "NoBroker Support" are
    NoBroker; dropping them lost real listings. "CRED Avenue" is *not* CRED —
    it is a different company — so a trailing token that is itself brand-like
    keeps the row rejected. That asymmetry is the whole rule.
    """
    if not row_company or not target_company:
        return False

    row = normalize_company(row_company).split()
    target = normalize_company(target_company).split()
    if not row or not target:
        return False
    if row == target:
        return True
    if len(row) <= len(target) or row[: len(target)] != target:
        return False
    return all(token in BRAND_SUFFIXES for token in row[len(target):])


# --------------------------------------------------------------------------
# titles
# --------------------------------------------------------------------------

# Expanded before comparison. Order matters: longer keys first so "sde" does
# not fire inside "sde-2".
TITLE_MAP = (
    (r"\bsde\s*[-\s]?\s*(\d)\b", r"software development engineer \1"),
    (r"\bsde\b", "software development engineer"),
    (r"\bswe\s*[-\s]?\s*(\d)\b", r"software engineer \1"),
    (r"\bswe\b", "software engineer"),
    (r"\bmts\b", "member of technical staff"),
    (r"\bsr\.?\b", "senior"),
    (r"\bjr\.?\b", "junior"),
    (r"\bmgr\.?\b", "manager"),
    (r"\bengg?\.?\b", "engineer"),
    (r"\bdev\b", "developer"),
    (r"\bback[\s-]?end\b", "back end"),
    (r"\bfront[\s-]?end\b", "front end"),
    (r"\bfull[\s-]?stack\b", "full stack"),
    (r"\bui\s*/\s*ux\b", "ui ux"),
    (r"\bqa\b", "quality assurance"),
    (r"\bml\b", "machine learning"),
    (r"\bai\b", "artificial intelligence"),
    (r"\bpm\b", "product manager"),
)

# Roman numerals used as levels, and the words for the same thing.
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4}

SENIORITY = ("intern", "junior", "mid", "senior", "staff", "principal", "lead")


def normalize_title(title: str | None) -> str:
    """A title's comparable form.

    This is where `'SDE-2 Backend'` and
    `'Software Development Engineer 2, Back End'` become the same string.
    """
    if not title:
        return ""
    text = title.lower()
    text = re.sub(r"[^a-z0-9\s/\-]", " ", text)
    for pattern, replacement in TITLE_MAP:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[\-/]", " ", text)
    return " ".join(text.split())


def extract_level(title: str | None) -> tuple[str | None, int | None]:
    """The (seniority, numeric) level carried by a title.

    Returned separately from the title because §3A.4's gate runs on these
    before any fuzzing: no scorer can tell SDE-1 from SDE-2, which score 96.8
    on every metric because one character carries all the meaning.
    """
    text = normalize_title(title)
    if not text:
        return None, None

    seniority = next((s for s in SENIORITY if re.search(rf"\b{s}\b", text)), None)

    numeric: int | None = None
    # `engineer 2`, `level 2`, `l2`, `engineer ii`
    match = re.search(r"\b(?:level|l)\s*(\d)\b", text) or \
        re.search(r"\b(\d)\b", text)
    if match:
        numeric = int(match.group(1))
    else:
        for token in text.split():
            if token in _ROMAN:
                numeric = _ROMAN[token]
                break
            if token in _WORD_NUM:
                numeric = _WORD_NUM[token]
                break
    return seniority, numeric


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

REMOTE_TERMS = ("work from home", "wfh", "remote", "anywhere")


def split_locations(location: str | None) -> list[str]:
    """One listing can name several cities.

    The board returns "Delhi , Ghaziabad , Noida" — note the space before each
    comma — for a single role. Comparing that as one opaque string against
    "Delhi" fails, so it is split before it reaches anything that compares.
    """
    if not location:
        return []
    parts = re.split(r"\s*[,/|]\s*|\s+and\s+", location)
    out: list[str] = []
    for part in parts:
        norm = normalize_location(part)
        if norm and norm not in out:
            out.append(norm)
    return out


def normalize_location(location: str | None) -> str:
    if not location:
        return ""
    text = location.lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = " ".join(text.split())
    if any(term in text for term in REMOTE_TERMS):
        return "remote"
    return text


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

_REL = re.compile(
    r"(?:posted\s+)?(\d+)\s*\+?\s*(day|week|month|year)s?\s*ago", re.I
)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

# Formats the career pages and boards actually emit.
_ABSOLUTE_FORMATS = ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y")


def parse_posted_date(
    raw: str | None, today: date | None = None
) -> tuple[str | None, str]:
    """``("2026-08-01", "relative")`` — the date, and how well we know it.

    The confidence half is not decoration. A relative date is only accurate to
    its own unit: "3 weeks ago" is ±7 days, so a 90-day staleness threshold
    computed from it is a claim about a range, not a day. Callers that publish
    a staleness signal have to say which they are standing on.
    """
    if not raw or not str(raw).strip():
        return None, "absent"

    text = str(raw).strip()
    today = today or datetime.now(timezone.utc).date()

    match = _REL.search(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        posted = today - timedelta(days=amount * _UNIT_DAYS[unit])
        return posted.isoformat(), "relative"

    if re.search(r"\b(today|just posted)\b", text, re.I):
        return today.isoformat(), "relative"
    if re.search(r"\byesterday\b", text, re.I):
        return (today - timedelta(days=1)).isoformat(), "relative"

    for fmt in _ABSOLUTE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat(), "exact"
        except ValueError:
            continue

    # Text that is not a date at all — the board emits "Job" and "Fresher Job"
    # in this field for roughly a quarter of its rows. Absent, not zero.
    return None, "absent"
