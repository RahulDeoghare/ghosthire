"""Ghost scoring, per plan §5.

**Nothing is scored without careers-page evidence.** A listing from a company
whose careers page we did not successfully read is `career_page_checked = 0`
and carries no score at all — not zero, not "probably fine", no score. Absence
of evidence is not evidence of absence, and skipping that distinction is the
fastest way to publish a wrong accusation about a real company.

There is a second trap underneath the first, and this project has already
walked into it once. Our careers-page extractor works on Greenhouse and
returns **zero rows** on SmartRecruiters and Workday — not an error, an empty
list. Treating "the extractor returned nothing" as "the company lists no
roles" would mark every board listing for those companies as a ghost with the
highest-weighted signal we have. So an empty result only counts as evidence
when the extractor is known to work on that platform; otherwise it is
unassessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

# §5. ghost_score = min(sum(triggered), 100)
SIGNALS = {
    "not_on_career_page": 45,   # verified today; the load-bearing signal
    "stale_90d": 20,            # from date_posted, never from first_seen
    "stale_180d": 15,           # additional, on top of stale_90d
    "career_page_empty": 10,    # careers page read successfully, zero roles
    "cross_board_mismatch": 10, # same role, different details across boards
    "repost_detected": 15,      # instrumented; expect no findings in two days
}

# Bands shown in the UI.
BANDS = ((0, 29, "likely real"), (30, 59, "questionable"), (60, 100, "likely ghost"))


def band(score: int) -> str:
    for low, high, label in BANDS:
        if low <= score <= high:
            return label
    return "unknown"


@dataclass
class ScoreResult:
    """A score, or an explicit refusal to produce one."""

    career_page_checked: int
    ghost_score: int | None = None
    signals: list[str] = field(default_factory=list)
    match_confidence: float | None = None
    matched_career_listing_id: int | None = None
    # Signals we could not evaluate, and why. Shown in the UI so a reader can
    # tell "we checked and it is fine" from "we could not check".
    abstained: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def band(self) -> str | None:
        return None if self.ghost_score is None else band(self.ghost_score)


def _days_old(posted_iso: str | None, today: date) -> int | None:
    if not posted_iso:
        return None
    try:
        return (today - date.fromisoformat(posted_iso)).days
    except ValueError:
        return None


def score_listing(
    *,
    career_page_checked: bool,
    matched: bool,
    match_confidence: float | None = None,
    matched_career_listing_id: int | None = None,
    date_posted: str | None = None,
    date_posted_confidence: str = "absent",
    career_page_role_count: int | None = None,
    cross_board_mismatch: bool = False,
    repost_detected: bool = False,
    today: date | None = None,
    reason: str = "",
) -> ScoreResult:
    """Score one board listing, or decline to.

    ``career_page_checked`` is the caller's assertion that this company's
    careers page was read successfully *and* by an extractor known to work on
    that platform. It is not inferred from a row count here, because a zero-row
    result is ambiguous between "no open roles" and "we cannot read this DOM".
    """
    today = today or datetime.now(timezone.utc).date()

    if not career_page_checked:
        return ScoreResult(
            career_page_checked=0,
            ghost_score=None,
            reason=reason or "no careers-page data for this company — not assessed",
            match_confidence=match_confidence,
        )

    signals: list[str] = []
    abstained: list[str] = []

    if matched:
        # On the careers page. This is the common case and the honest baseline;
        # the product exists to find the exceptions, not to accuse everyone.
        pass
    else:
        signals.append("not_on_career_page")

    if career_page_role_count == 0:
        signals.append("career_page_empty")

    # Staleness comes from the listing's own posted date, never from when we
    # first archived it — our archive is days old and would call everything new.
    if date_posted_confidence == "absent" or not date_posted:
        abstained.append("stale_90d")
        abstained.append("stale_180d")
    else:
        age = _days_old(date_posted, today)
        if age is None:
            abstained.extend(["stale_90d", "stale_180d"])
        else:
            if age >= 90:
                signals.append("stale_90d")
            if age >= 180:
                signals.append("stale_180d")

    if cross_board_mismatch:
        signals.append("cross_board_mismatch")
    if repost_detected:
        signals.append("repost_detected")

    total = min(sum(SIGNALS[s] for s in signals), 100)
    return ScoreResult(
        career_page_checked=1,
        ghost_score=total,
        signals=signals,
        match_confidence=match_confidence,
        matched_career_listing_id=matched_career_listing_id,
        abstained=abstained,
        reason=reason,
    )
