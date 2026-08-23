"""Tests for the `bdata` CLI wrapper.

The Bright Data CLI is replaced with a small fake that writes the same shapes
the real one does. That lets the parsing, snapshot-archiving and failure
handling be tested without an API key or a credit, which matters because the
failure paths are the ones that are hard to reproduce on demand.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

from ghosthire import bdata

FAKE_ROWS = [
    {
        "company_name": "Acme Technologies",
        "job_title": "Backend Engineer",
        "location": "Bengaluru",
        "date_posted": "2 weeks ago",
        "job_url": "https://internshala.com/job/detail/backend-engineer-1",
    },
    {
        "company_name": "Foobar Labs",
        "job_title": "SDE-1, Platform",
        "location": "Remote",
        "date_posted": "3 months ago",
        "job_url": "https://internshala.com/job/detail/sde-1-platform-2",
    },
]


def _write_fake_cli(tmp_path, body: str):
    """Install a stand-in for the `bdata` binary and return its path."""
    script = tmp_path / "fake_bdata"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


SUCCESS_CLI = """
import json, sys
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
rows = json.loads({rows!r})
with open(out, "w") as fh:
    json.dump(rows, fh, indent=2)
print(json.dumps({{"status": "ok", "rows": len(rows)}}))
"""

AUTH_FAIL_CLI = """
import sys
print("Error: No API key found.", file=sys.stderr)
print("  Run 'brightdata login' or set BRIGHTDATA_API_KEY env variable.", file=sys.stderr)
sys.exit(1)
"""

EMPTY_CLI = """
import json, sys
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
with open(out, "w") as fh:
    json.dump([], fh)
"""

# Payload the real CLI returned from careers.smartrecruiters.com on 2026-08-20:
# a per-URL crawler failure delivered as a record in the data array.
ERROR_CLI = """
import json, sys
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
with open(out, "w") as fh:
    json.dump([{
        "input": {"url": "https://example.test/careers"},
        "error": "Crawler error: navigate validation error: url is required",
        "error_code": "bad_cmd_arg",
    }], fh)
"""

# Emits the payload on stdout and never writes -o, so the wrapper's own
# archiving path has to run.
STDOUT_ONLY_CLI = """
import json
print(json.dumps(json.loads({rows!r})))
"""


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    """Redirect archived snapshots into tmp so tests never touch data/."""
    target = tmp_path / "snapshots"
    target.mkdir()
    monkeypatch.setattr(bdata, "SNAPSHOT_DIR", target)
    return target


def _use(monkeypatch, tmp_path, body: str):
    script = _write_fake_cli(tmp_path, body)
    monkeypatch.setattr(bdata, "BDATA_BIN", str(script))
    monkeypatch.setattr(bdata.shutil, "which", lambda _: str(script))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return script


# --------------------------------------------------------------------------
# row extraction — the CLI returns a bare array today, but an envelope would
# silently ingest nothing if we only handled one shape.
# --------------------------------------------------------------------------

def test_extract_rows_from_bare_array():
    assert bdata.extract_rows(FAKE_ROWS) == FAKE_ROWS


@pytest.mark.parametrize("key", bdata._ROW_CONTAINER_KEYS)
def test_extract_rows_from_envelope(key):
    assert bdata.extract_rows({key: FAKE_ROWS, "status": "done"}) == FAKE_ROWS


def test_empty_container_does_not_mask_a_populated_one():
    """`data: []` beside `results: [...]` must not read as zero rows."""
    payload = {"data": [], "results": FAKE_ROWS}
    assert bdata.extract_rows(payload) == FAKE_ROWS


def test_extract_rows_from_single_record():
    assert bdata.extract_rows(FAKE_ROWS[0]) == [FAKE_ROWS[0]]


def test_extract_rows_ignores_non_dict_entries():
    assert bdata.extract_rows([FAKE_ROWS[0], "junk", None]) == [FAKE_ROWS[0]]


def test_extract_rows_on_garbage():
    assert bdata.extract_rows(None) == []
    assert bdata.extract_rows({"status": "queued"}) == []


# --------------------------------------------------------------------------
# run_collector
# --------------------------------------------------------------------------

def test_successful_run_reads_the_file_the_cli_wrote(tmp_path, monkeypatch, snapshots):
    _use(monkeypatch, tmp_path, SUCCESS_CLI.format(rows=json.dumps(FAKE_ROWS)))

    result = bdata.run_collector("c_test1234", ["https://example.test/jobs"], "internshala")

    assert result.status == "success"
    assert result.rows_returned == 2
    assert result.collector_id == "c_test1234"
    assert result.snapshot_path is not None and result.snapshot_path.exists()
    assert json.loads(result.snapshot_path.read_text()) == FAKE_ROWS
    assert result.started_at and result.completed_at


def test_run_archives_a_snapshot_the_cli_did_not_write(tmp_path, monkeypatch, snapshots):
    """The archive guarantee is ours, not the CLI's.

    When the CLI emits the payload on stdout and never writes the -o file, the
    wrapper must still land a snapshot on disk — that is the code path the
    provenance claim rests on, and a fake that writes the file itself would
    never exercise it.
    """
    _use(monkeypatch, tmp_path, STDOUT_ONLY_CLI.format(rows=json.dumps(FAKE_ROWS)))

    result = bdata.run_collector("c_test1234", ["https://example.test/jobs"], "internshala")

    assert result.status == "success"
    assert result.rows_returned == 2
    assert result.snapshot_path is not None and result.snapshot_path.exists()
    assert json.loads(result.snapshot_path.read_text()) == FAKE_ROWS


def test_reserved_snapshot_is_removed_when_nothing_is_written(tmp_path, monkeypatch, snapshots):
    """A failed run must not leave an empty file posing as evidence."""
    _use(monkeypatch, tmp_path, AUTH_FAIL_CLI)

    result = bdata.run_collector("c_test1234", ["https://example.test/jobs"], "internshala")

    assert result.status == "failed"
    assert result.snapshot_path is None
    assert list(snapshots.iterdir()) == []


def test_genuinely_empty_page_is_partial_not_failure(tmp_path, monkeypatch, snapshots):
    """A careers page with no open roles is a real observation.

    Distinct from the error case below: an empty array means the collector read
    the page and there was nothing on it, which the scoring engine treats as a
    signal in its own right.
    """
    _use(monkeypatch, tmp_path, EMPTY_CLI)

    result = bdata.run_collector("c_test1234", ["https://example.test/careers"], "career_page")

    assert result.status == "partial"
    assert result.rows_returned == 0
    assert result.crawler_errors == []
    assert result.ok is True


def test_all_error_payload_is_failed_not_success(tmp_path, monkeypatch, snapshots):
    """Error records are not job listings.

    Bright Data reports a per-URL crawler failure as a record in the same array
    as real data. Counting those as rows turned a total scrape failure into
    "success, 1 rows" and printed an empty table under it — which is exactly
    what happened on this project's first live run.
    """
    _use(monkeypatch, tmp_path, ERROR_CLI)

    result = bdata.run_collector("c_test1234", ["https://example.test/careers"], "career_page")

    assert result.status == "failed"
    assert result.ok is False
    assert result.rows_returned == 0
    assert len(result.crawler_errors) == 1
    assert "navigate validation error" in result.error


def test_auth_failure_returns_a_readable_hint(tmp_path, monkeypatch, snapshots):
    _use(monkeypatch, tmp_path, AUTH_FAIL_CLI)

    result = bdata.run_collector("c_test1234", ["https://example.test/jobs"], "internshala")

    assert result.status == "failed"
    assert result.ok is False
    assert "bdata login" in result.error


def test_missing_collector_id_is_refused_before_spending_anything():
    with pytest.raises(bdata.BdataError, match="no collector_id"):
        bdata.run_collector("", ["https://example.test"], "internshala")


def test_missing_urls_is_refused():
    with pytest.raises(bdata.BdataError, match="no target URLs"):
        bdata.run_collector("c_test1234", [], "internshala")


def test_missing_cli_binary_is_explained(monkeypatch, snapshots):
    monkeypatch.setattr(bdata.shutil, "which", lambda _: None)
    with pytest.raises(bdata.BdataError, match="npm install"):
        bdata.run_collector("c_test1234", ["https://example.test"], "internshala")


def test_snapshot_filenames_are_sortable_and_labelled(snapshots):
    part = bdata.reserve_snapshot("career_page")
    assert part.parent == snapshots
    assert part.name.endswith("_career-page.json" + bdata.PART_SUFFIX)
    assert part.name[8] == "T" and part.name[15] == "Z"

    part.write_text("[]")
    final = bdata.publish_snapshot(part)
    assert final is not None and final.name.endswith("_career-page.json")
    assert not part.exists()


def test_snapshot_names_are_unique_within_the_same_second(snapshots):
    """The property the archive actually depends on.

    Timestamps are second-granular and runs can finish inside one second. A
    name that is merely well-formatted still overwrites the previous run's
    evidence, so uniqueness is reserved at allocation time, not at write time.
    """
    paths = [bdata.reserve_snapshot("fork-razorpay") for _ in range(5)]
    assert len({p.name for p in paths}) == 5
    assert all(p.exists() for p in paths), "names must be reserved on disk"


def test_a_reservation_never_collides_with_a_published_snapshot(snapshots):
    """Publishing renames into the *.json namespace, so allocation has to check
    both — otherwise a later run in the same second reserves a name that is
    already a finished snapshot and overwrites it on publish."""
    first = bdata.reserve_snapshot("internshala")
    first.write_text(json.dumps(FAKE_ROWS))
    published = bdata.publish_snapshot(first)

    second = bdata.reserve_snapshot("internshala")
    assert bdata.published_path(second) != published
    second.write_text("[]")
    assert bdata.publish_snapshot(second) != published
    assert json.loads(published.read_text()) == FAKE_ROWS


def test_an_in_flight_run_is_invisible_to_a_json_glob(tmp_path, monkeypatch, snapshots):
    """The provenance promise is that data/snapshots/*.json rebuilds the DB.

    A reservation that lands as a 0-byte *.json breaks every concurrent reader
    of that glob, so the in-flight file must not match it.
    """
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        # Mid-run: the CLI has created the file but not finished writing it.
        seen["glob_during_run"] = sorted(p.name for p in snapshots.glob("*.json"))
        seen["out_suffix"] = out.name
        out.write_text(json.dumps(FAKE_ROWS))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bdata.shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(bdata.subprocess, "run", fake_run)

    result = bdata.run_collector("c_test1234", ["https://example.test"], "internshala")

    assert seen["glob_during_run"] == [], "a partial run must not match *.json"
    assert str(seen["out_suffix"]).endswith(bdata.PART_SUFFIX)
    assert result.snapshot_path in list(snapshots.glob("*.json"))
    assert list(snapshots.glob("*" + bdata.PART_SUFFIX)) == []


# --------------------------------------------------------------------------
# collectors.yaml
# --------------------------------------------------------------------------

def test_descriptions_resolve_and_fit_the_cli_cap():
    doc = bdata.load_collectors()
    for collector in doc["collectors"]:
        assert collector["description"], f"{collector['key']} has no description"
        assert len(collector["description"]) <= bdata.DESCRIPTION_MAX_CHARS


def test_every_career_target_is_https_and_has_a_slug():
    career = bdata.get_collector("career_generic")
    slugs = set()
    for target in career["targets"]:
        assert target["url"].startswith("https://"), target
        assert target["slug"] not in slugs, f"duplicate slug {target['slug']}"
        slugs.add(target["slug"])
    assert len(slugs) >= 8


def test_every_career_target_shares_one_description():
    """The cross-platform claim rests on the text being byte-identical.

    This checks only that: one description_ref for the whole collector, no
    per-target override, and several distinct platforms behind it. Whether the
    description WORKS on each platform is measured by the fork test against
    live pages, not asserted here.
    """
    career = bdata.get_collector("career_generic")
    assert career["description_ref"] == "career_generic"
    for target in career["targets"]:
        assert "description" not in target, (
            f"{target['slug']} overrides the shared description"
        )
    assert len({t.get("ats") for t in career["targets"]}) >= 3


def test_unknown_collector_key_lists_the_known_ones():
    with pytest.raises(bdata.BdataError, match="Known keys"):
        bdata.get_collector("nope")


def test_set_collector_id_preserves_comments_and_descriptions(tmp_path):
    source = bdata.COLLECTORS_YAML.read_text()
    scratch = tmp_path / "collectors.yaml"
    scratch.write_text(source)

    bdata.set_collector_id("career_generic", "c_abc123def456", path=scratch)

    updated = scratch.read_text()
    assert "collector_id: c_abc123def456" in updated
    assert updated.count("collector_id:") == source.count("collector_id:")
    # comments and the block-scalar descriptions survive the edit
    assert "# Bright Data Scraper Studio collectors." in updated
    assert "Extract every open job listing on this page." in updated

    # Exactly one collector changed; every other ID is byte-for-byte intact.
    # Asserted as a diff against the source rather than against a fixed list,
    # so the test does not break every time a real collector is created.
    before = {c["key"]: c.get("collector_id")
              for c in bdata.load_collectors(bdata.COLLECTORS_YAML)["collectors"]}
    after = {c["key"]: c.get("collector_id")
             for c in bdata.load_collectors(scratch)["collectors"]}

    assert after["career_generic"] == "c_abc123def456"
    assert {k: v for k, v in after.items() if k != "career_generic"} == \
           {k: v for k, v in before.items() if k != "career_generic"}


# --------------------------------------------------------------------------
# first-contact diagnostics — Scraper Studio names its own fields, and a
# renamed one must be loud rather than a quiet column of dashes.
# --------------------------------------------------------------------------

def test_unexpected_field_names_are_reported(capsys):
    from ghosthire import cli

    cli.report_unmapped_fields([
        {"job_title": "Backend Engineer", "job_url": "https://x.test/1",
         "seniority_level": "mid", "posted_epoch": 1755000000},
    ])
    out = capsys.readouterr().out
    assert "seniority_level" in out and "posted_epoch" in out
    assert "WARNING" not in out  # title and url were both readable


def test_missing_title_or_url_raises_a_warning(capsys):
    from ghosthire import cli

    cli.report_unmapped_fields([{"role_name": "Backend Engineer", "href": "https://x.test/1"}])
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "role_name" in out and "href" in out


def test_known_aliases_are_not_reported_as_unmapped(capsys):
    from ghosthire import cli

    cli.report_unmapped_fields([
        {"title": "Backend Engineer", "url": "https://x.test/1", "city": "Pune"},
    ])
    assert capsys.readouterr().out == ""


def test_career_targets_point_at_pages_that_list_roles():
    """A landing page is not a job board.

    The first collector generation was spent against https://razorpay.com/jobs/,
    a marketing page with no listings on it. The generated scraper learned to
    follow the outbound link rather than to extract roles, and failed on every
    other company.

    Asserted structurally rather than against a list of known-bad URLs: a
    target's scrape URL must differ from the page a human lands on, or be
    explicitly flagged as a site where the two genuinely are the same. That
    distinguishes "checked, and they are the same page" from "nobody looked".
    """
    for target in bdata.get_collector("career_generic")["targets"]:
        if target["url"] == target["landing_url"]:
            assert target.get("url_is_landing") is True, (
                f"{target['slug']}: scrape URL equals the landing URL and is "
                "not flagged — this is how the landing-page trap comes back"
            )


def test_collector_observations_are_backed_by_a_snapshot():
    """A roles_seen claimed from collector output must have the file to prove it.

    Checking one hand-typed YAML field against another proves nothing. Where a
    target says the collector observed N roles, the named snapshot must exist
    and actually contain at least N rows.
    """
    for target in bdata.get_collector("career_generic")["targets"]:
        if target.get("observed_by") != "collector":
            continue
        evidence = target.get("evidence")
        assert evidence, f"{target['slug']} claims collector data but names no file"
        path = bdata.SNAPSHOT_DIR.parent.parent / evidence
        assert path.exists(), f"{target['slug']}: {evidence} is missing"
        rows = bdata.extract_rows(json.loads(path.read_text()))
        assert len(rows) >= target["roles_seen"], (
            f"{target['slug']} claims {target['roles_seen']} roles, "
            f"{evidence} holds {len(rows)}"
        )


def test_board_observations_record_how_many_rows_were_actually_ours():
    """A board target's claim is checkable: the named snapshot must exist, and
    the count of rows genuinely at that company must match what is recorded.

    This is the provenance behind the ingest guard. "3 returned, 0 ours" is the
    observation that keeps Postman off the leaderboard instead of on it under
    a false accusation.
    """
    from ghosthire.normalize import company_matches

    for collector in bdata.find_collectors(kind="board"):
        for target in collector.get("targets") or []:
            if target.get("observed_by") != "collector":
                continue
            evidence = target.get("evidence")
            assert evidence, f"{target['slug']} claims collector data, names no file"
            path = bdata.SNAPSHOT_DIR.parent.parent / evidence
            assert path.exists(), f"{target['slug']}: {evidence} is missing"

            rows = bdata.extract_rows(json.loads(path.read_text()))
            assert len(rows) == target["rows_seen"]
            ours = sum(1 for r in rows
                       if company_matches(r.get("company_name"), target["company"]))
            assert ours == target["rows_at_company"], (
                f"{target['slug']}: config says {target['rows_at_company']} rows at "
                f"{target['company']}, snapshot holds {ours}"
            )


def test_http_fetch_observations_never_claim_collector_evidence():
    """A number read off a page by hand must not carry a snapshot citation."""
    for target in bdata.get_collector("career_generic")["targets"]:
        if target.get("observed_by") == "http_fetch":
            assert target.get("evidence") is None, (
                f"{target['slug']}: observed by hand but cites a snapshot"
            )


def test_fork_default_spans_three_distinct_ats_platforms():
    """The fork test is meaningless if every target runs the same ATS."""
    from ghosthire import cli

    default = cli.build_parser().parse_args(["fork"]).targets.split(",")
    targets = {t["slug"]: t for t in bdata.get_collector("career_generic")["targets"]}
    platforms = {targets[slug]["ats"] for slug in default}
    assert len(platforms) == 3, f"fork default covers only {platforms}"


# --------------------------------------------------------------------------
# Regressions. Each of these fails if the corresponding fix is reverted.
# --------------------------------------------------------------------------

def _captured_argv(monkeypatch, tmp_path, snapshots, **kwargs):
    """Run a collector against a stub and return the argv it was invoked with."""
    seen: dict[str, object] = {}

    def fake_run(cmd, **popen_kwargs):
        seen["argv"] = cmd
        seen["timeout"] = popen_kwargs.get("timeout")
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as fh:
            json.dump(FAKE_ROWS, fh)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bdata.shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(bdata.subprocess, "run", fake_run)
    bdata.run_collector("c_test1234", ["https://example.test/jobs"], "internshala", **kwargs)
    return seen


def test_sync_passes_timeout_so_the_cli_cannot_fall_back_to_hour_long_polling(
    tmp_path, monkeypatch, snapshots
):
    """The blocker: --sync alone can poll for an hour.

    On a realtime page-limit error the CLI silently switches to batch polling,
    and batch reads its cap from --timeout, defaulting to 3600s when the flag
    is absent. --sync-timeout does not bound that fallback; --timeout does.
    """
    seen = _captured_argv(monkeypatch, tmp_path, snapshots, sync=True, timeout=600)
    argv = seen["argv"]

    assert "--sync" in argv
    assert "--timeout" in argv, "without --timeout the batch fallback polls for 3600s"
    cap = int(argv[argv.index("--sync-timeout") + 1])
    assert cap == int(argv[argv.index("--timeout") + 1])
    assert bdata.SYNC_TIMEOUT_MIN <= cap <= bdata.SYNC_TIMEOUT_MAX
    # The wall clock must follow the sync cap, not the unused async timeout.
    assert seen["timeout"] <= bdata.SYNC_TIMEOUT_MAX + 60


def test_async_wall_clock_follows_the_async_timeout(tmp_path, monkeypatch, snapshots):
    seen = _captured_argv(monkeypatch, tmp_path, snapshots, sync=False, timeout=120)
    assert int(seen["argv"][seen["argv"].index("--timeout") + 1]) == 120
    assert seen["timeout"] == 180


def test_sync_refuses_multiple_urls_instead_of_dropping_them(snapshots):
    """The CLI rejects --sync with --urls, but we pass a positional URL — so
    without this guard seven of eight targets vanish without a word."""
    with pytest.raises(bdata.BdataError, match="single URL"):
        bdata.run_collector(
            "c_test1234",
            ["https://a.test", "https://b.test"],
            "career_page",
            sync=True,
        )


def test_timeout_still_produces_a_result_to_record(tmp_path, monkeypatch, snapshots):
    """The module promises a result for every invocation, and a run that burned
    the full timeout is the most expensive one to lose."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(bdata.shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(bdata.subprocess, "run", fake_run)

    result = bdata.run_collector("c_test1234", ["https://example.test"], "internshala")

    assert result.status == "failed"
    assert "timed out" in result.error
    assert result.collector_id == "c_test1234"
    assert result.started_at and result.completed_at
    # The branch that must not vanish is also the one that must not lie: with
    # nothing written, there is no snapshot to point at.
    assert result.snapshot_path is None
    assert list(snapshots.iterdir()) == []


def test_timeout_keeps_whatever_the_cli_managed_to_write(tmp_path, monkeypatch, snapshots):
    """A partial write from an expensive run is still evidence — publish it."""
    def fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(FAKE_ROWS[:1]))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(bdata.shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(bdata.subprocess, "run", fake_run)

    result = bdata.run_collector("c_test1234", ["https://example.test"], "internshala")

    assert result.status == "failed"
    assert result.snapshot_path is not None and result.snapshot_path.exists()
    assert result.snapshot_path.suffix == ".json"
    assert json.loads(result.snapshot_path.read_text()) == FAKE_ROWS[:1]


def test_output_flushed_after_exit_is_still_read(tmp_path, monkeypatch, snapshots):
    """The CLI can write its -o file after its process exits.

    A live run reported "0 rows, failed" while the snapshot it had just written
    held three valid rows, its mtime one second past the exit. Reading a moment
    too early discards real evidence and mislabels a working collector as
    broken, so the read waits briefly before concluding there is nothing.
    """
    monkeypatch.setattr(bdata, "OUTPUT_FLUSH_GRACE_S", 2.0)

    def fake_run(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])

        def write_late():
            time.sleep(0.3)
            out.write_text(json.dumps(FAKE_ROWS))

        threading.Thread(target=write_late, daemon=True).start()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bdata.shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(bdata.subprocess, "run", fake_run)

    result = bdata.run_collector("c_test1234", ["https://example.test"], "internshala")

    assert result.status == "success"
    assert result.rows_returned == 2
    assert result.snapshot_path is not None and result.snapshot_path.exists()


def test_the_grace_period_does_not_invent_a_snapshot(tmp_path, monkeypatch, snapshots):
    """A run that truly wrote nothing must still fail, not hang and then lie."""
    monkeypatch.setattr(bdata, "OUTPUT_FLUSH_GRACE_S", 0.2)
    _use(monkeypatch, tmp_path, AUTH_FAIL_CLI)

    result = bdata.run_collector("c_test1234", ["https://example.test"], "internshala")

    assert result.status == "failed"
    assert result.snapshot_path is None
    assert list(snapshots.iterdir()) == []


def test_rejected_arguments_leave_nothing_on_disk(snapshots):
    """Every argument check runs before a name is reserved. Reserving first
    left a 0-byte *.json behind for each refused call."""
    for args, kwargs in [
        ((("c_test1234"), ["https://a.test", "https://b.test"], "career_page"),
         {"sync": True}),
        (("", ["https://a.test"], "career_page"), {}),
        (("c_test1234", [], "career_page"), {}),
    ]:
        with pytest.raises(bdata.BdataError):
            bdata.run_collector(*args, **kwargs)
    assert list(snapshots.iterdir()) == []


def test_a_missing_binary_leaves_nothing_on_disk(monkeypatch, snapshots):
    monkeypatch.setattr(bdata.shutil, "which", lambda _: None)
    with pytest.raises(bdata.BdataError, match="npm install"):
        bdata.run_collector("c_test1234", ["https://example.test"], "internshala")
    assert list(snapshots.iterdir()) == []


def test_create_without_a_collector_id_explains_itself(tmp_path, monkeypatch, snapshots):
    """The message printed after a paid ten-minute generation must not be the
    word "None"."""
    body = """
import json, sys
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
with open(out, "w") as fh:
    json.dump({"name": "gh-career-generic", "status": "quota_exceeded"}, fh)
"""
    _use(monkeypatch, tmp_path, body)

    result = bdata.create_collector("https://example.test", "extract the jobs",
                                    name="gh-career-generic")

    assert result.collector_id is None
    assert result.error and "None" not in result.error
    assert "quota_exceeded" in result.error
    assert result.snapshot_path is not None and result.snapshot_path.exists()


@pytest.mark.parametrize("bad", [
    "c_x: {a: b} #comment",      # yaml metacharacters
    "c_x\ncollectors: []",       # newline injection deleting later keys
    "c_UPPER",                   # outside the documented shape
    "not_a_collector",
    "",
])
def test_set_collector_id_refuses_values_that_would_corrupt_the_config(bad, tmp_path):
    """The ID is a remote value written into a file every later command parses,
    and the write lands right after a paid ten-minute generation."""
    scratch = tmp_path / "collectors.yaml"
    scratch.write_text(bdata.COLLECTORS_YAML.read_text())

    with pytest.raises(bdata.BdataError, match="refusing to write"):
        bdata.set_collector_id("career_generic", bad, path=scratch)

    yaml.safe_load(scratch.read_text())  # still parses
    assert scratch.read_text() == bdata.COLLECTORS_YAML.read_text()


def test_set_collector_id_rejects_non_strings(tmp_path):
    scratch = tmp_path / "collectors.yaml"
    scratch.write_text(bdata.COLLECTORS_YAML.read_text())
    with pytest.raises(bdata.BdataError):
        bdata.set_collector_id("career_generic", {"id": "c_123"}, path=scratch)


def test_error_rows_are_never_counted_as_listings():
    payload = [
        {"job_title": "Backend Engineer", "job_url": "https://x.test/1"},
        {"input": {"url": "u"}, "error": "Crawler error", "error_code": "bad_cmd_arg"},
    ]
    assert len(bdata.extract_rows(payload)) == 1
    assert len(bdata.extract_errors(payload)) == 1


def test_an_error_carrying_a_title_is_not_rendered_as_a_job():
    """A rate-limit envelope with a `title` would otherwise appear in the
    job-title column as a fabricated listing."""
    payload = [{"title": "Rate limit exceeded", "error": "429"}]
    assert bdata.extract_rows(payload) == []
