"""Dashboard tests.

The page is plain HTML with no build step, so its two safety helpers are
extracted from the shipped file and exercised directly with node. That keeps
these tests honest: they run the code that is actually served, not a Python
restatement of it.

They skip rather than fail where node is unavailable, since node is the
Bright Data CLI's runtime and not a declared dependency of this project.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web/index.html"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _html() -> str:
    return WEB.read_text()


def _run_with_helpers(script: str) -> dict:
    """Run `script` with safeUrl/esc lifted out of the shipped page."""
    html = _html()
    safe_url = re.search(r"function safeUrl\(u\) \{[\s\S]*?\n\}", html).group(0)
    esc = re.search(r"const esc = s => String[\s\S]*?\}\[c\]\)\);", html).group(0)
    out = subprocess.run(
        ["node", "-e", f"{esc}\n{safe_url}\n{script}"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# the blocker: a scraped URL becomes a clickable link in this origin
# --------------------------------------------------------------------------

@node
@pytest.mark.parametrize("url", [
    "javascript:alert(document.domain)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
])
def test_dangerous_schemes_are_never_linked(url):
    """Escaping makes a URL safe to sit *in* an attribute; it says nothing
    about what the URL does when clicked. Every URL on this page is scraped
    remote text and the product invites the viewer to click it."""
    result = _run_with_helpers(
        f"console.log(JSON.stringify({{safe: safeUrl({url!r})}}))")
    assert result["safe"] is None


@node
@pytest.mark.parametrize("url", [
    "//evil.example/x",     # protocol-relative
    "/api/listings",        # relative path
    "",
])
def test_non_absolute_urls_are_never_linked(url):
    """Resolving these against the page origin produced a link back to the
    dashboard labelled "open the board listing" — evidence going nowhere."""
    result = _run_with_helpers(
        f"console.log(JSON.stringify({{safe: safeUrl({url!r})}}))")
    assert result["safe"] is None


@node
@pytest.mark.parametrize("url", [
    "https://internshala.com/job/detail/x-1234",
    "http://example.test/a",
])
def test_real_evidence_links_still_work(url):
    """The guard must not achieve safety by breaking the links the product
    exists to show."""
    result = _run_with_helpers(
        f"console.log(JSON.stringify({{safe: safeUrl({url!r})}}))")
    assert result["safe"] is not None


@node
def test_markup_in_scraped_text_is_inert():
    payload = "<img src=x onerror=alert(1)> Associate, Startup Accounts"
    result = _run_with_helpers(
        f"console.log(JSON.stringify({{out: esc({payload!r})}}))")
    assert "<img" not in result["out"]
    assert "&lt;img" in result["out"]


# --------------------------------------------------------------------------
# things that would mislead a viewer
# --------------------------------------------------------------------------

def test_hidden_is_defined_locally_not_borrowed_from_the_cdn():
    """`hidden` is a Tailwind utility and closeDetail() only toggles that
    class. With the CDN unreachable the evidence modal could be opened and
    never dismissed — Escape and backdrop click both dead — leaving it over
    the page permanently."""
    style = _html().split("</style>")[0]
    assert re.search(r"\.hidden\s*\{[^}]*display:\s*none", style)


def test_verdict_colours_survive_a_dead_cdn():
    """The verdict is the one thing colour carries. If those classes came from
    the CDN, an offline page would lose the encoding entirely — every job would
    look alike. Class names may change with a redesign; owning them locally
    must not."""
    style = _html().split("</style>")[0]
    positive, negative, unknown = [], [], []
    for line in style.splitlines():
        name = line.strip().split("{")[0].strip()
        if not name.startswith("."):
            continue
        if "ok" in name or "real" in name:
            positive.append(name)
        elif "bad" in name or "ghost" in name:
            negative.append(name)
        elif "none" in name or "unknown" in name:
            unknown.append(name)
    assert positive, "no locally-defined class for the corroborated verdict"
    assert negative, "no locally-defined class for the not-found verdict"
    assert unknown, "no locally-defined class for the unverified verdict"


def test_a_date_is_shown_in_the_form_the_source_used():
    """51 of 70 listings gave their age in words — "3 weeks ago" — and we
    stored an ISO date. Rendering that back as a calendar date presents an
    estimate as a reading; rendering it as a phrase keeps the precision
    visible in the shape of the value, without needing a symbol to explain it.

    So: a relative source renders relative, an exact source renders a date.
    """
    html = _html()
    body = html[html.index("function posted("):]
    body = body[:body.index("\n}")]

    assert '"relative"' in body, "the renderer must branch on how the date was obtained"
    rel = body[body.index('"relative"'):]
    assert "ago(" in rel, "a relative source must render as a relative phrase"

    # And the exact branch must render an actual date rather than a phrase.
    assert "MONTHS[" in body, "an exact source must render as a calendar date"
    assert "title=" in body, "the imprecision must still be explained on hover"


def test_no_view_opens_with_dozens_of_rows():
    """The original page rendered all 70 listings at once, which reads as a
    dump. Whatever the default filter is, the page must cap what it draws and
    offer the rest, rather than rendering everything."""
    html = _html()
    import re

    cap = re.search(r"const PAGE\s*=\s*(\d+)", html)
    assert cap, "no page cap constant"
    assert 5 <= int(cap.group(1)) <= 30, \
        f"cap of {cap.group(1)} is not a cap worth having"
    assert "more" in html.lower(), "capped rows must be reachable"


def test_the_reader_can_filter_to_each_verdict():
    """Browsing is the point, so each verdict has to be selectable — including
    the unverified ones, which must be reachable rather than hidden."""
    html = _html()
    assert "unchecked" in html and "confirmed" in html and "ghost" in html


# --------------------------------------------------------------------------
# theming
# --------------------------------------------------------------------------

def test_both_themes_are_defined_locally():
    """A theme built out of CDN utility classes disappears with the CDN. Both
    token sets live in the page's own stylesheet."""
    style = _html().split("</style>")[0]
    assert ":root{" in style.replace(" ", "")
    assert '[data-theme="dark"]' in style


def test_the_theme_is_applied_before_the_page_paints():
    """Reading the preference after render flashes the wrong theme. The
    applier has to run in <head>, before <body> exists."""
    html = _html()
    head = html[:html.index("</head>")]
    assert "data-theme" in head, "no pre-paint theme applier in <head>"
    assert "prefers-color-scheme" in head, "the OS preference must be the default"


def test_stored_theme_access_is_guarded():
    """localStorage throws outright in some privacy modes. A theme preference
    is not worth a blank page."""
    html = _html()
    for hit in ("localStorage.getItem", "localStorage.setItem"):
        i = html.index(hit)
        assert "try" in html[max(0, i - 200):i], f"{hit} is not inside a try"


def test_the_chart_carries_a_palette_for_each_surface():
    """A dark palette is stepped for its own background, not flipped from the
    light one — each was validated separately against its surface."""
    html = _html()
    assert "color_dark" in html, "the chart must choose per surface"
    assert "isDark()" in html


def test_a_wide_window_is_filled_rather_than_margined():
    """Capping the reading column stopped titles and verdicts drifting apart,
    but left large empty margins. The open job fills them instead, and one
    media query owns the breakpoint so CSS and JS cannot disagree about it."""
    html = _html()
    assert ".panes{" in html.replace(" ", ""), "no two-pane layout"
    assert "grid-template-columns" in html
    assert "WIDE" in html, "the breakpoint must be readable from script"
    # The same breakpoint value in both places, not two that drift.
    import re
    widths = set(re.findall(r"min-width:\s*(\d+)px", html))
    assert len(widths) == 1, f"breakpoint declared at differing widths: {widths}"


def test_a_narrow_window_still_gets_the_dialog():
    """Below the breakpoint there is no room for a pane, so the job has to open
    as a dialog rather than render into something hidden."""
    html = _html()
    assert 'id="detail"' in html and 'role="dialog"' in html
    assert "paneEmpty" in html, "the pane needs an empty state, not a blank column"


def test_the_modal_is_announced_and_takes_focus():
    html = _html()
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert "lastFocus" in html, "focus must return to where it came from"
