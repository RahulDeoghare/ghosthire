"""Reading values out of collector output.

Scraper Studio names its own output fields from the plain-English description,
and the names vary by target. This module is the single place that knows which
key means what, so the terminal and the ingest path can never disagree about
what a row said — a divergence there would mean the table on screen and the
row in the database describe different things.
"""

from __future__ import annotations

from typing import Any

# Collector output field names vary by target; accept the obvious synonyms
# rather than losing a row to a naming difference.
FIELD_ALIASES = {
    "company": ("company_name", "company", "employer", "organisation", "organization"),
    "title": ("job_title", "title", "role", "position"),
    "location": ("location", "job_location", "city", "place"),
    "posted": ("date_posted", "posted", "posted_date", "posted_on", "date"),
    "url": ("job_url", "url", "link", "job_link", "apply_url"),
    "department": ("department", "team", "function", "category"),
    "salary": ("salary_range", "salary", "stipend", "compensation", "ctc"),
}

# Scraped values reach the operator's terminal directly, so control bytes are
# stripped: an escape sequence in a job title would otherwise repaint or clear
# the screen of whoever runs this.
_CONTROL_CHARS: dict[int, str | None] = {
    **{c: None for c in range(0x20)},
    **{c: None for c in range(0x7F, 0xA0)},
    # Whitespace controls separate words; deleting them would weld the words
    # either side together ("Backend\nEngineer" -> "BackendEngineer").
    0x09: " ", 0x0A: " ", 0x0B: " ", 0x0C: " ", 0x0D: " ",
}


def _clean(text: str) -> str:
    """Strip control bytes and flatten whitespace in remote text."""
    return " ".join(text.translate(_CONTROL_CHARS).split())


def _scalar(value: Any) -> str:
    """Render a collector value as flat text, or nothing.

    Collector output is remote JSON and a field can come back as a list or an
    object. Passing those through ``str()`` would print a Python repr into a
    job table, which reads like data but is not.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_scalar(v) for v in value) if part)
    return ""


def pick(row: dict[str, Any], field: str) -> str:
    for key in FIELD_ALIASES[field]:
        value = row.get(key)
        if value in (None, "", [], {}):
            continue
        text = _clean(_scalar(value))
        if text:
            return text
    return ""
