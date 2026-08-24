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


def test_score_bands_survive_a_dead_cdn():
    """Acceptance item 1 is colour-coded bands. If those came from the CDN,
    an offline dashboard would lose the encoding entirely."""
    style = _html().split("</style>")[0]
    for band in ("band-real", "band-quest", "band-ghost"):
        assert f".{band}" in style


def test_the_detail_view_marks_derived_dates_the_way_the_list_does():
    """A relative date is accurate to about a week. The leaderboard says so
    with a tilde; the detail view rendered the same value as a bare date — and
    the detail view is exactly where someone goes to scrutinise a claim."""
    html = _html()
    detail = html[html.index("async function openDetail"):]
    assert "dateCell(d)" in detail, "detail must reuse the list's date renderer"
    assert "accurate to about a week" in detail


def test_the_page_opens_on_findings_not_on_everything():
    """Opening with all 70 listings led with the 66 that could not be checked,
    which reads as a dump rather than a finding. The default is the listings
    that carry a verdict; the rest stay one click away."""
    html = _html()
    assert 'let FILTER = "checked"' in html
    assert '["checked",' in html, "a 'checked' filter must exist to default to"


def test_long_lists_are_capped_until_asked():
    html = _html()
    assert "const PAGE = 25" in html
    assert "Show ${hidden} more" in html


def test_the_modal_is_announced_and_takes_focus():
    html = _html()
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert "lastFocus" in html, "focus must return to where it came from"
