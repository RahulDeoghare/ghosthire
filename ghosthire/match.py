"""Resolving a board listing to a role on the company's own careers page.

Gate, then fuzz — never fuzz alone. Plan §3A.4 measured this rather than
assuming it, and two results drive the design:

- ``token_set_ratio`` scores a subset as a perfect match, so "Backend Engineer"
  on a careers page matches "Senior Backend Engineer" on a board at 100. It is
  never used here. Our own live data has a case at 87.1 — above any sane
  threshold — for two genuinely different Razorpay roles.
- No scorer can separate SDE-1 from SDE-2: they score 96.8 on every metric
  because one character carries all the meaning. Fuzzy matching structurally
  cannot solve it, so levels are compared *before* any fuzzing happens.

**Why this is written so defensively.** A false non-match publicly accuses a
real company of advertising a job that does not exist. That is the one failure
mode of this project that does real-world harm, so every uncertainty here
resolves toward "we are not sure" rather than toward "it is a ghost".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from rapidfuzz import fuzz

from .normalize import extract_level, normalize_company, normalize_title

# §3A.4 step 4. Below this, two titles are different roles.
FUZZ_THRESHOLD = 85.0

# A title carrying a level matched against one carrying none is ambiguous:
# "Backend Engineer" on a careers page may or may not be the "Backend Engineer
# II" on the board. We match, but say we are less sure, and the UI shows it.
AMBIGUITY_PENALTY = 0.25

SENIORITY_RANK = {
    "intern": 0, "junior": 1, "mid": 2,
    "senior": 3, "staff": 4, "principal": 5, "lead": 5,
}


@dataclass
class MatchResult:
    """Why a listing did or did not resolve to a careers-page role."""

    matched: bool
    career_row: Any | None = None
    score: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    ambiguous_level: bool = False


def _levels_conflict(a: str, b: str) -> tuple[bool, bool]:
    """``(conflict, ambiguous)`` for two titles.

    A conflict is a hard stop. Ambiguity — one side states a level, the other
    is silent — is not a conflict, because a careers page routinely omits the
    level a board spells out.
    """
    sen_a, num_a = extract_level(a)
    sen_b, num_b = extract_level(b)

    if num_a is not None and num_b is not None and num_a != num_b:
        return True, False
    if sen_a and sen_b and SENIORITY_RANK.get(sen_a) != SENIORITY_RANK.get(sen_b):
        return True, False

    ambiguous = (num_a is None) != (num_b is None) or bool(sen_a) != bool(sen_b)
    return False, ambiguous


def match_listing(
    board_title: str,
    board_company: str,
    career_rows: Sequence[Any],
    threshold: float = FUZZ_THRESHOLD,
) -> MatchResult:
    """Find ``board_title`` among a company's careers-page roles.

    ``career_rows`` must already be that company's rows; the company is
    re-checked anyway, because matching a title against the wrong company's
    board is how a listing gets called a ghost for the wrong reason.
    """
    if not career_rows:
        # Nothing to compare against is not evidence of absence. The caller
        # must treat this as unassessed, never as a ghost.
        return MatchResult(False, reason="no careers-page data for this company")

    board_norm = normalize_title(board_title)
    if not board_norm:
        return MatchResult(False, reason="board listing has no usable title")

    company_norm = normalize_company(board_company)
    best: tuple[float, Any, bool] | None = None
    gated = 0

    for row in career_rows:
        career_title = row["job_title"] if not isinstance(row, str) else row
        row_company = (
            row["company_name"] if not isinstance(row, str) and "company_name" in row.keys()
            else board_company
        )
        if normalize_company(row_company) != company_norm:
            continue

        conflict, ambiguous = _levels_conflict(board_title, career_title)
        if conflict:
            # §3A.4 step 3: skip the fuzz entirely. Scoring these would return
            # 96.8 and call two different jobs the same one.
            gated += 1
            continue

        # token_sort_ratio, never token_set_ratio. See the module docstring.
        score = fuzz.token_sort_ratio(board_norm, normalize_title(career_title))
        if best is None or score > best[0]:
            best = (score, row, ambiguous)

    if best is None:
        return MatchResult(
            False,
            reason=(f"all {gated} same-company roles differ in level"
                    if gated else "no roles for this company on the careers page"),
        )

    score, row, ambiguous = best
    if score < threshold:
        return MatchResult(
            False, career_row=row, score=score,
            reason=f"closest careers-page role scored {score:.1f}, below {threshold:.0f}",
        )

    confidence = score / 100.0
    if ambiguous:
        confidence *= 1 - AMBIGUITY_PENALTY
    return MatchResult(
        True, career_row=row, score=score, confidence=round(confidence, 3),
        reason=f"matched at {score:.1f}" + (" (level stated on one side only)"
                                            if ambiguous else ""),
        ambiguous_level=ambiguous,
    )
