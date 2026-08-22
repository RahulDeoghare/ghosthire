"""Tests for the terminal frontend.

The table printer, the snapshot viewer and the fork guards were entirely
untested until a QA pass pointed out that the strongest claim this project
makes — the fork verdict — could be printed after zero API calls.
"""

from __future__ import annotations

import json

import pytest

from ghosthire import cli

ROW = {
    "company_name": "Acme Technologies",
    "job_title": "Backend Engineer",
    "location": "Bengaluru",
    "date_posted": "2 weeks ago",
    "job_url": "https://example.test/1",
}


# --------------------------------------------------------------------------
# value rendering — collector output is remote JSON and reaches a terminal
# --------------------------------------------------------------------------

def test_control_characters_from_scraped_data_are_stripped():
    """A job title carrying an escape sequence must not repaint the screen."""
    row = {"job_title": "\x1b[31mRED\x1b[0m", "location": "Pune\x07"}
    assert cli.pick(row, "title") == "[31mRED[0m"
    assert "\x1b" not in cli.pick(row, "title")
    assert "\x07" not in cli.pick(row, "location")


def test_structured_values_do_not_leak_python_reprs():
    assert cli.pick({"job_title": {"en": "Engineer"}}, "title") == ""
    assert cli.pick({"company_name": ["Acme", "Beta"]}, "company") == "Acme, Beta"
    assert cli.pick({"location": False}, "location") == ""
    assert cli.pick({"location": 0}, "location") == "0"


def test_missing_fields_render_as_empty_not_none():
    assert cli.pick({}, "title") == ""
    assert cli.pick({"job_title": None}, "title") == ""


def test_whitespace_inside_values_is_flattened():
    assert cli.pick({"job_title": "Backend\n\tEngineer  II"}, "title") == "Backend Engineer II"


# --------------------------------------------------------------------------
# width — a terminal counts cells, not codepoints
# --------------------------------------------------------------------------

def test_cjk_characters_are_measured_as_two_cells():
    assert cli._display_width("東京") == 4
    assert cli._display_width("Tokyo") == 5


def test_truncation_respects_display_width():
    truncated = cli._truncate("東京都千代田区丸の内一丁目", 10)
    assert cli._display_width(truncated) <= 10


def test_padding_aligns_wide_characters():
    """Columns must not drift after a CJK cell."""
    assert cli._display_width(cli._pad("東京", 10)) == 10
    assert cli._display_width(cli._pad("Tokyo", 10)) == 10


def test_table_columns_stay_aligned_across_scripts(capsys):
    """Latin and CJK rows must end at the same column.

    Emoji are deliberately out of scope: a ZWJ sequence like 👩‍💻 renders as one
    cell pair but measures as its component codepoints, and fixing that needs a
    grapheme-segmentation library. Job titles are overwhelmingly text, so the
    cost is not worth a dependency — but the limit is real and stated here
    rather than papered over.
    """
    cli.print_table([
        ROW,
        {"company_name": "株式会社日本電信電話", "job_title": "ソフトウェア", "location": "東京"},
        {"company_name": "Beta Systems", "job_title": "Platform Engineer", "location": "Pune"},
    ])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    # Full padded width, not rstripped: stripping would remove the final
    # column's padding and make every row look like a different length.
    widths = {cli._display_width(ln) for ln in lines}
    assert len(widths) == 1, f"columns drift: {sorted(widths)}"


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------

def test_limit_zero_shows_nothing_rather_than_everything(capsys):
    """`if limit:` treated 0 as "no limit" and printed the whole snapshot."""
    cli.print_table([ROW] * 10, limit=0)
    out = capsys.readouterr().out
    assert "(no rows)" in out
    assert "Acme Technologies" not in out


def test_limit_none_shows_everything(capsys):
    cli.print_table([ROW] * 3, limit=None)
    assert capsys.readouterr().out.count("Acme Technologies") == 3


def test_hidden_row_count_is_never_fabricated(capsys):
    cli.print_table([ROW] * 10, limit=4)
    out = capsys.readouterr().out
    assert "... 6 more rows" in out


def test_negative_limit_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["show", "x.json", "--limit", "-5"])


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------

def test_show_handles_a_directory_without_a_traceback(tmp_path, capsys):
    assert cli.main(["show", str(tmp_path)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_show_handles_a_binary_file(tmp_path, capsys):
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xc8\xff\x00\xfe")
    assert cli.main(["show", str(binary)]) == 2
    assert "not text" in capsys.readouterr().err


def test_show_handles_malformed_json(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cli.main(["show", str(bad)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_show_handles_a_missing_file(tmp_path, capsys):
    assert cli.main(["show", str(tmp_path / "nope.json")]) == 2
    assert "no such snapshot" in capsys.readouterr().err


def test_show_reports_error_records_rather_than_listing_them(tmp_path, capsys):
    """An error record is never a listing, and its absence is never an empty
    careers page. Both halves have to be on screen."""
    snap = tmp_path / "errors.json"
    snap.write_text(json.dumps([
        {"input": {"url": "https://careers.example.test"},
         "error": "Crawler error: navigate validation error: url is required",
         "error_code": "bad_cmd_arg"}
    ]))
    assert cli.main(["show", str(snap)]) == 0
    out = capsys.readouterr().out

    assert "0 rows" in out
    assert "1 crawler error(s)" in out
    assert "(no rows)" in out          # not rendered as a job
    assert "https://careers.example.test" in out
    assert "navigate validation error" in out
    assert "NOT an empty careers page" in out


def test_show_does_not_cry_error_on_a_genuinely_empty_page(tmp_path, capsys):
    """The other half of the same distinction: zero roles is a real
    observation, and P2 scores it as one."""
    snap = tmp_path / "empty.json"
    snap.write_text("[]")
    assert cli.main(["show", str(snap)]) == 0
    out = capsys.readouterr().out

    assert "0 rows" in out
    assert "crawler error" not in out
    assert "NOT an empty careers page" not in out


# --------------------------------------------------------------------------
# fork — this command prints the project's headline claim
# --------------------------------------------------------------------------

def test_fork_refuses_to_run_with_no_targets(capsys):
    """0/0 satisfied `generalized == total`, so GENERALIZES printed after zero
    API calls — the strongest claim in the project, from no data at all."""
    assert cli.main(["fork", "--targets", ""]) == 2
    assert "GENERALIZES" not in capsys.readouterr().out


def test_fork_refuses_targets_that_share_one_platform(capsys):
    """Three runs against one ATS bills three times and proves nothing about
    generalization."""
    assert cli.main(["fork", "--targets", "razorpay,razorpay,postman"]) == 2
    assert "greenhouse" in capsys.readouterr().err


def _stub_runs(monkeypatch, results):
    """Feed cmd_fork/cmd_scrape canned RunResults, one per call, no network."""
    from ghosthire.bdata import RunResult

    queue = list(results)

    def fake_run(collector_id, urls, source, **kwargs):
        spec = queue.pop(0) if queue else {}
        return RunResult(
            collector_id=collector_id or "c_stub",
            source=source,
            target_urls=urls,
            status=spec.get("status", "success"),
            rows=spec.get("rows", []),
            error=spec.get("error"),
            duration_s=1.0,
        )

    monkeypatch.setattr(cli, "run_collector", fake_run)


CAREER_ROW = {"job_title": "Backend Engineer", "job_url": "https://x.test/1"}


def test_fork_says_inconclusive_when_every_run_failed(monkeypatch, capsys):
    """The mirror of the no-targets guard, and the one that actually fired.

    With bdata unauthenticated all three runs fail, zero API calls succeed —
    and the command used to print DOES NOT GENERALIZE, reporting a refutation
    of the project's headline claim that nothing had tested.
    """
    _stub_runs(monkeypatch, [{"status": "failed", "error": "No API key found"}] * 3)

    code = cli.main(["fork", "--targets", "razorpay,freshworks,browserstack"])
    out = capsys.readouterr().out

    assert code == 1
    assert "INCONCLUSIVE" in out
    assert "DOES NOT GENERALIZE" not in out
    assert "GENERALIZES" not in out.replace("DOES NOT GENERALIZE", "")
    assert "No API key found" in out


def test_fork_excludes_failed_runs_from_the_verdict(monkeypatch, capsys):
    """A run that never reached the page is not evidence against the
    description — but it must still be visible, not quietly dropped."""
    _stub_runs(monkeypatch, [
        {"status": "success", "rows": [CAREER_ROW]},
        {"status": "failed", "error": "Crawler error: url is required"},
    ])

    code = cli.main(["fork", "--targets", "razorpay,freshworks"])
    out = capsys.readouterr().out

    assert code == 0
    assert "GENERALIZES — 1/1" in out
    assert "1/2 run(s) failed" in out
    assert "freshworks" in out


def test_fork_counts_a_zero_row_run_against_the_description(monkeypatch, capsys):
    """Distinct from a failure: the collector read the page and extracted
    nothing, which is a real answer about the description."""
    _stub_runs(monkeypatch, [
        {"status": "success", "rows": [CAREER_ROW]},
        {"status": "partial", "rows": [], "error": "collector returned zero rows"},
    ])

    code = cli.main(["fork", "--targets", "razorpay,freshworks"])
    out = capsys.readouterr().out

    assert code == 0
    assert "PARTIAL — 1/2" in out
    assert "run(s) failed" not in out


def test_smartrecruiters_does_not_shift_the_fork_table(monkeypatch, capsys):
    """The money-shot table: a 15-char ATS name must not push its row right."""
    _stub_runs(monkeypatch, [{"status": "success", "rows": [CAREER_ROW]}] * 2)

    cli.main(["fork", "--targets", "razorpay,freshworks"])
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.startswith(("razorpay ", "freshworks ", "TARGET "))]

    assert len(lines) == 3
    starts = {ln.index("%") for ln in lines if "%" in ln}
    assert len(starts) == 1, "coverage columns must line up across rows"


def test_fork_default_spans_three_distinct_platforms():
    from ghosthire import bdata

    default = cli.build_parser().parse_args(["fork"]).targets.split(",")
    targets = {t["slug"]: t for t in bdata.get_collector("career_generic")["targets"]}
    assert len({targets[s]["ats"] for s in default}) == 3


# --------------------------------------------------------------------------
# scrape — the P1 acceptance command
# --------------------------------------------------------------------------

def _two_collector_doc():
    """One created collector and one that was never built, same source."""
    return {
        "descriptions": {"board": "extract the jobs"},
        "collectors": [
            {"key": "board_made", "kind": "board", "source": "internshala",
             "collector_id": "c_made1234", "description": "extract the jobs",
             "targets": [{"url": "https://example.test/jobs"}]},
            {"key": "board_unmade", "kind": "board", "source": "internshala",
             "collector_id": None, "description": "extract the jobs",
             "targets": [{"url": "https://example.test/internships"}]},
        ],
    }


def test_an_uncreated_collector_does_not_fail_a_successful_scrape(monkeypatch, capsys):
    """`--source internshala` fans out to both Internshala collectors, and one
    has no c_* ID yet. Counting that as a failure made the P1 acceptance
    command exit 1 after printing real rows — which breaks `set -e`, cron and
    Thursday's sweep.sh."""
    monkeypatch.setattr(cli, "load_collectors", lambda *a, **k: _two_collector_doc())
    _stub_runs(monkeypatch, [{"status": "success", "rows": [ROW]}])

    code = cli.main(["scrape", "--source", "internshala"])
    captured = capsys.readouterr()

    assert code == 0, "a collector that was never created is not a failed run"
    assert "skipping board_unmade" in captured.out
    assert "create_collectors.sh" in captured.out
    assert "Backend Engineer" in captured.out
    # It must not announce a run it cannot make, least of all with ID "None".
    assert "· None ·" not in captured.out


def test_scrape_still_fails_when_nothing_can_run(monkeypatch, capsys):
    doc = _two_collector_doc()
    doc["collectors"][0]["collector_id"] = None
    monkeypatch.setattr(cli, "load_collectors", lambda *a, **k: doc)

    assert cli.main(["scrape", "--source", "internshala"]) == 2
    assert "no created collector" in capsys.readouterr().err


def test_a_failed_run_still_fails_the_command(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_collectors", lambda *a, **k: _two_collector_doc())
    _stub_runs(monkeypatch, [{"status": "failed", "error": "No API key found"}])

    assert cli.main(["scrape", "--source", "internshala"]) == 1


# --------------------------------------------------------------------------
# field-name diagnostics
# --------------------------------------------------------------------------

def test_fields_bright_data_always_sends_are_not_reported_as_surprises(capsys):
    """`input` and `product_page_url` ride along on every response. Reporting
    them on every successful run trains the reader to ignore the channel that
    also carries the real warning."""
    cli.report_unmapped_fields([
        {"job_title": "Backend Engineer", "job_url": "u",
         "input": {"url": "x"}, "product_page_url": "y"},
    ])
    assert capsys.readouterr().out == ""


def test_a_genuinely_unknown_field_is_still_reported(capsys):
    cli.report_unmapped_fields([
        {"job_title": "Backend Engineer", "job_url": "u", "seniority_level": "mid"},
    ])
    assert "seniority_level" in capsys.readouterr().out
