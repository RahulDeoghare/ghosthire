"""GhostHire command line.

    python -m ghosthire.cli scrape --source internshala --limit 20
    python -m ghosthire.cli scrape --source career --company razorpay
    python -m ghosthire.cli collectors
    python -m ghosthire.cli show data/snapshots/<file>.json

Every `scrape` writes the raw collector response to data/snapshots/ and prints
the collector ID that produced it, so what is on screen and what is on disk
can always be tied back to one entry in scrapers/collectors.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

from . import ROOT
from .bdata import (
    BdataError,
    RunResult,
    create_collector,
    extract_errors,
    extract_rows,
    find_collectors,
    get_collector,
    load_collectors,
    run_collector,
    set_collector_id,
)

DOT = "·"

# Field aliases and the value scrubber live in fields.py so the terminal and
# the ingest path read a row identically. `_clean` and `_scalar` are re-exported
# because the create path and the snapshot viewer scrub remote text too.
from .fields import (  # noqa: E402
    FIELD_ALIASES,
    _clean,
    _scalar,
    pick,
)

__all__ = ["FIELD_ALIASES", "pick", "main", "build_parser"]


def _warn(message: str) -> None:
    """Write to stderr without letting it overtake stdout.

    Piping the command to `tee` or a log file makes stdout block-buffered
    while stderr stays unbuffered, so an error can appear *above* the run that
    produced it. On a recorded demo that reads as the tool contradicting
    itself.
    """
    sys.stdout.flush()
    print(message, file=sys.stderr)
    sys.stderr.flush()


def _display_width(text: str) -> int:
    """Terminal cells, not codepoints — CJK and fullwidth glyphs take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def report_unmapped_fields(rows: list[dict[str, Any]]) -> None:
    """Say so, loudly, when the collector returns fields we do not read.

    Scraper Studio generates its own schema from the plain-English
    description. It usually honours the field names asked for, but it is not
    contractually bound to — and a renamed field would otherwise show up as a
    quiet column of dashes rather than an error. This turns the first run
    against a new collector into a diagnostic instead of a guess.
    """
    if not rows:
        return

    # Bright Data attaches these to every record. Reporting them on every
    # successful run trains the reader to ignore the channel that also carries
    # the real "your field names changed" warning.
    expected_noise = {"input", "product_page_url", "timestamp", "discovery_input"}
    known = {alias for aliases in FIELD_ALIASES.values() for alias in aliases}
    known |= expected_noise
    seen: set[str] = set()
    for row in rows:
        seen.update(row.keys())

    unmapped = sorted(k for k in seen - known if not k.startswith("_"))
    if unmapped:
        print(
            f"  note: {len(unmapped)} field(s) returned but not read: "
            f"{', '.join(unmapped)}"
        )
        print("        add them to FIELD_ALIASES in ghosthire/cli.py if useful.")

    for field in ("title", "url"):
        if not any(pick(row, field) for row in rows):
            print(
                f"  WARNING: no row has a usable {field!r}. The collector's "
                f"field names may differ from the description."
            )
            print(f"           seen instead: {', '.join(sorted(seen))}")


def _truncate(text: str, width: int) -> str:
    """Truncate to a display width, keeping grapheme clusters intact."""
    text = " ".join(text.split())
    if _display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        # Never cut inside a combining or zero-width-joiner sequence.
        if unicodedata.combining(ch) or ch == "\u200d":
            out.append(ch)
            continue
        cost = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cost > width - 1:
            break
        out.append(ch)
        used += cost
    return "".join(out) + "…"


def _pad(text: str, width: int) -> str:
    """Left-align to a display width (str.ljust counts codepoints)."""
    return text + " " * max(0, width - _display_width(text))


def print_table(rows: list[dict[str, Any]], limit: int | None = None) -> None:
    """Print rows as COMPANY / TITLE / LOCATION / POSTED.

    Career-page rows carry no company — the company *is* the target — so for
    those the first column shows the department instead of a column of dashes.
    """
    # `if limit` would treat --limit 0 as "no limit"; None is the only
    # no-limit value, and a negative limit is rejected at the parser.
    shown = rows if limit is None else rows[:limit]
    if not shown:
        print("  (no rows)")
        return

    lead = "company" if any(pick(row, "company") for row in shown) else "department"
    widths = {"lead": 22, "title": 34, "location": 18, "posted": 14}
    print(
        f"{_pad(lead.upper(), widths['lead'])}  {_pad('TITLE', widths['title'])}  "
        f"{_pad('LOCATION', widths['location'])}  {_pad('POSTED', widths['posted'])}"
    )
    for row in shown:
        cells = [
            _truncate(pick(row, lead) or "—", widths["lead"]),
            _truncate(pick(row, "title") or "—", widths["title"]),
            _truncate(pick(row, "location") or "—", widths["location"]),
            _truncate(pick(row, "posted") or "—", widths["posted"]),
        ]
        keys = ["lead", "title", "location", "posted"]
        print("  ".join(_pad(c, widths[k]) for c, k in zip(cells, keys)))

    hidden = len(rows) - len(shown)
    if hidden > 0:
        print(f"  ... {hidden} more rows in the snapshot")


def print_run_header(result: RunResult) -> None:
    print(
        f"collector {result.collector_id} {DOT} {result.source} {DOT} "
        f"{result.rows_returned} rows {DOT} {result.duration_s}s"
    )


def _rel(path: Path | None) -> str:
    if path is None:
        return "(none)"
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_scrape(args: argparse.Namespace) -> int:
    doc = load_collectors()

    # `career` is an alias for the one generic career collector; every other
    # source is looked up by its `source:` field.
    if args.source == "career":
        matches = [get_collector("career_generic", doc)]
    else:
        matches = [
            c for c in find_collectors(source=args.source, doc=doc)
            if c.get("enabled", True)
        ]
        if not matches:
            _warn(f"no enabled collector for source {args.source!r}")
            return 2

    # ---- --company filters slugged targets for ANY source, not just career.
    # The board is scraped per company (one filtered URL each) because the
    # front-page sweep and the career targets are disjoint populations, and a
    # matcher needs the same company on both sides to have anything to do.
    collectors = []
    for collector in matches:
        targets = collector.get("targets") or []
        if args.company:
            picked = [t for t in targets if t.get("slug") == args.company]
            if not picked:
                slugs = ", ".join(
                    t["slug"] for t in targets if t.get("slug")
                ) or "(this collector's targets have no slugs)"
                _warn(
                    f"no target with slug {args.company!r} in "
                    f"{collector['key']}. Known: {slugs}"
                )
                return 2
            targets = picked
        collectors.append((collector, targets))

    # ---- A collector that has no c_* ID yet was never created; it did not
    # fail. Counting it as a failure made the P1 acceptance command exit 1 on a
    # fully successful scrape, which breaks `set -e`, cron and sweep.sh.
    pending = [c for c, _ in collectors if not c.get("collector_id")]
    collectors = [(c, t) for c, t in collectors if c.get("collector_id")]
    for collector in pending:
        print(
            f"skipping {collector['key']} {DOT} not created yet "
            f"{DOT} run scrapers/create_collectors.sh {collector['key']}"
        )
    if not collectors:
        _warn(
            f"no created collector for source {args.source!r}. "
            "Run scrapers/create_collectors.sh first."
        )
        return 2

    failures = 0
    for collector, targets in collectors:
        urls = [t["url"] for t in targets if t.get("url")]
        if not urls:
            _warn(f"{collector['key']}: no target URLs configured")
            failures += 1
            continue

        source = collector["source"]
        if args.company:
            source = f"{source}_{args.company}"

        try:
            print(
                f"running {collector['key']} {DOT} {collector.get('collector_id')} "
                f"{DOT} {len(urls)} url(s) {DOT} "
                f"{'sync' if args.sync else f'async, up to {args.timeout}s'}"
            )
            result = run_one(
                collector, urls, source, sync=args.sync, timeout=args.timeout
            )
        except BdataError as exc:
            # One misconfigured collector should not cost us the others.
            _warn(f"{collector['key']}: {exc}")
            failures += 1
            continue

        print_run_header(result)
        if result.status == "failed":
            _warn(f"  status: FAILED {DOT} {result.error}")
            failures += 1
        else:
            print_table(result.rows, limit=args.limit)
            report_unmapped_fields(result.rows)
        if result.snapshot_path:
            print(f"→ {_rel(result.snapshot_path)}")
        print()

    return 1 if failures else 0


def run_one(
    collector: dict[str, Any],
    urls: list[str],
    source: str,
    sync: bool = False,
    timeout: int = 600,
) -> RunResult:
    return run_collector(
        collector_id=collector.get("collector_id"),
        urls=urls,
        source=source,
        sync=sync,
        timeout=timeout,
        stream=True,  # interactive: let the poll counter reach the terminal
    )


def cmd_collectors(args: argparse.Namespace) -> int:
    doc = load_collectors()
    rows = doc.get("collectors") or []
    print(f"{'KEY':26}  {'KIND':7}  {'COLLECTOR ID':22}  TARGETS")
    for collector in rows:
        cid = collector.get("collector_id") or "— not created"
        enabled = "" if collector.get("enabled", True) else "  (disabled)"
        print(
            f"{collector['key']:26}  {collector['kind']:7}  {cid:22}  "
            f"{len(collector.get('targets') or [])}{enabled}"
        )
    pending = [c for c in rows if not c.get("collector_id") and c.get("enabled", True)]
    if pending:
        print(
            f"\n{len(pending)} collector(s) not yet created. "
            "Run scrapers/create_collectors.sh"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Render a snapshot already on disk. Costs nothing, proves the archive."""
    path = Path(args.path)
    if not path.exists():
        _warn(f"no such snapshot: {path}")
        return 2
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        _warn(f"{path} is not valid JSON: {exc}")
        return 2
    except UnicodeDecodeError:
        _warn(f"{path} is not text — snapshots are UTF-8 JSON")
        return 2
    except OSError as exc:
        _warn(f"cannot read {path}: {exc.strerror}")
        return 2

    rows = extract_rows(payload)
    errors = extract_errors(payload)
    fixture = isinstance(payload, dict) and payload.get("_fixture") is True
    label = "FIXTURE (hand-written, not collector output)" if fixture else "snapshot"
    tail = f" {DOT} {len(errors)} crawler error(s)" if errors else ""
    print(f"{label} {DOT} {_rel(path)} {DOT} {len(rows)} rows{tail}")
    if isinstance(payload, dict) and payload.get("collector_id"):
        print(f"collector {payload['collector_id']}")
    print()
    print_table(rows, limit=args.limit)
    print_crawler_errors(errors, rows)
    return 0


def print_crawler_errors(
    errors: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> None:
    """Report the records that are Bright Data failures rather than listings.

    Without this, a careers page with no open roles and a scrape that never
    read the page both render as `(no rows)`. Downstream those two must not be
    the same value: an empty page is a ghost signal that drives the score up,
    while a failed scrape has to land as ``career_page_checked = 0``.
    """
    if not errors:
        return
    print()
    print(f"  {len(errors)} record(s) are crawler errors, not listings:")
    for err in errors[:5]:
        source = err.get("input")
        url = source.get("url") if isinstance(source, dict) else None
        message = _clean(_scalar(err.get("error") or err.get("error_code")))
        print(f"    {_clean(_scalar(url)) or '(no url)'}: {message[:120]}")
    if len(errors) > 5:
        print(f"    ... {len(errors) - 5} more")
    if not rows:
        print(
            "  NOT an empty careers page — the collector never read it. "
            "Do not score this as a missing listing."
        )


def cmd_description(args: argparse.Namespace) -> int:
    """Print a field description verbatim. Used by create_collectors.sh."""
    doc = load_collectors()
    descriptions = doc.get("descriptions") or {}
    if args.key not in descriptions:
        _warn(f"no description {args.key!r}. Known: {', '.join(descriptions)}")
        return 2
    sys.stdout.write(descriptions[args.key].strip())
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    doc = load_collectors()
    collector = get_collector(args.key, doc)
    if collector.get("collector_id") and not args.force:
        print(
            f"{args.key} already has collector_id "
            f"{collector['collector_id']} — pass --force to create another"
        )
        return 0

    targets = collector.get("targets") or []
    url = args.url or (targets[0]["url"] if targets else None)
    if not url:
        _warn(f"{args.key} has no target URL to build against")
        return 2

    print(
        f"creating collector {args.key} against {url}\n"
        "generation takes 5-10 minutes server-side; the CLI polls and retries "
        "through the concurrent-job cap."
    )
    result = create_collector(
        url=url,
        description=collector["description"],
        name=collector.get("name"),
    )
    if not result.collector_id:
        _warn(f"create failed: {_clean(_scalar(result.error)) or 'no detail'}")
        if result.snapshot_path:
            _warn(f"  response archived at {_rel(result.snapshot_path)}")
        return 1

    # `collector_id` is validated by set_collector_id, but `name` and `status`
    # are free-form remote strings that reach the terminal unchecked; pick()
    # scrubs row values for exactly this reason and the create path must too.
    print(
        f"collector {result.collector_id} {DOT} "
        f"{_clean(_scalar(result.name)) or '(unnamed)'} {DOT} "
        f"{_clean(_scalar(result.status)) or 'unknown'}"
    )
    print(f"→ {_rel(result.snapshot_path)}")
    set_collector_id(args.key, result.collector_id)
    print(f"wrote collector_id into scrapers/collectors.yaml under {args.key}")
    return 0


# Fields the generic career description asks Scraper Studio for. The first two
# are load-bearing: without a title and a URL a row cannot be matched or
# verified by a human.
CAREER_REQUIRED_FIELDS = ("title", "url")
# Wide enough for the longest ATS name we carry ("smartrecruiters", 15). At 11
# that row shifted every column right of it — on the table this project exists
# to show.
ATS_W = 16
CAREER_OPTIONAL_FIELDS = ("location", "department")


def _ats(target: dict[str, Any]) -> str:
    return target.get("ats") or "unknown"


def _coverage(rows: list[dict[str, Any]], field: str) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if pick(row, field)) / len(rows)


def cmd_fork(args: argparse.Namespace) -> int:
    """The §0.3 fork test: does ONE plain-English description generalize?

    Runs the single generic career collector against several unrelated ATS
    platforms. If the same instruction returns clean rows from a Lever page, a
    Greenhouse page and a hand-rolled page, one collector replaces eight — and
    that result is the strongest thing this project can show. If it does not,
    we find out in ten minutes instead of after eight generations.
    """
    doc = load_collectors()
    collector = get_collector("career_generic", doc)
    all_targets = collector.get("targets") or []
    # Deduplicate, preserving order: running the same target three times bills
    # three times and would report "3/3 targets" from one platform, which makes
    # the verdict meaningless.
    wanted: list[str] = []
    for slug in (s.strip() for s in args.targets.split(",")):
        if slug and slug not in wanted:
            wanted.append(slug)

    if not wanted:
        _warn(
            "no targets given. The fork test compares one description across "
            "several ATS platforms, so it needs at least two."
        )
        return 2

    targets = []
    for slug in wanted:
        match = next((t for t in all_targets if t.get("slug") == slug), None)
        if match is None:
            known = ", ".join(t.get("slug", "?") for t in all_targets)
            _warn(f"unknown target {slug!r}. Known: {known}")
            return 2
        targets.append(match)

    # `or "unknown"` rather than a dict default: a target with `ats:` left
    # blank yields None, and sorted({None, "greenhouse"}) is a TypeError.
    platforms = sorted({t.get("ats") or "unknown" for t in targets})
    if len(platforms) < 2:
        _warn(
            f"all {len(targets)} target(s) run {platforms[0]!r}. The question "
            "is whether one description survives DIFFERENT platforms, so pick "
            "targets that do not share an ATS."
        )
        return 2
    print(
        f"fork test {DOT} collector {collector.get('collector_id') or '— not created'} "
        f"{DOT} one description, {len(targets)} targets, "
        f"{len(platforms)} platforms ({', '.join(platforms)})"
    )
    print(f"description: {collector['description_ref']} "
          f"({len(collector['description'])} chars, byte-identical for every target)")
    print()

    results = []
    for target in targets:
        try:
            result = run_collector(
                collector_id=collector.get("collector_id"),
                urls=[target["url"]],
                source=f"fork_{target['slug']}",
                sync=args.sync,
                stream=True,
            )
        except BdataError as exc:
            _warn(f"error: {exc}")
            return 1
        results.append((target, result))
        print(
            f"  {target['slug']:<14} {_ats(target):<{ATS_W}} "
            f"{result.status:<8} {result.rows_returned:>4} rows  "
            f"{result.duration_s}s  → {_rel(result.snapshot_path)}"
        )

    print()
    header = (
        f"{'TARGET':<14} {_pad('ATS', ATS_W)} {'STATUS':<8} {'ROWS':>5}  "
    )
    header += "  ".join(f"{f.upper():>10}" for f in
                        CAREER_REQUIRED_FIELDS + CAREER_OPTIONAL_FIELDS)
    print(header)
    print("-" * len(header))

    # ---- A run that failed never tested the description: the CLI errored, or
    # the crawler never read the page. Those tell us nothing about whether one
    # description generalizes, so they are excluded from the verdict rather
    # than counted as evidence against it.
    tested = [(t, r) for t, r in results if r.status != "failed"]
    failed = [(t, r) for t, r in results if r.status == "failed"]

    generalized = 0
    for target, result in results:
        cells = []
        for fld in CAREER_REQUIRED_FIELDS + CAREER_OPTIONAL_FIELDS:
            cells.append(
                "        —" if result.status == "failed"
                else f"{_coverage(result.rows, fld) * 100:9.0f}%"
            )
        print(
            f"{target['slug']:<14} {_pad(_ats(target), ATS_W)} "
            f"{result.status:<8} {result.rows_returned:>5}  " + "  ".join(cells)
        )
        if result.status != "failed" and result.rows and all(
            _coverage(result.rows, fld) >= 0.9 for fld in CAREER_REQUIRED_FIELDS
        ):
            generalized += 1

    print()
    total = len(results)
    if not total:
        print("VERDICT: NONE — no target produced a result, so nothing is proven.")
        return 1

    if failed:
        print(f"{len(failed)}/{total} run(s) failed before the description was "
              "tested, and are excluded from the verdict:")
        for target, result in failed:
            print(f"  {target['slug']:<14} {(result.error or 'no detail')[:90]}")
        print()

    if not tested:
        # The mirror image of the no-targets guard: with every run failed, a
        # confident "DOES NOT GENERALIZE" would report a refutation that no API
        # call was ever made to earn.
        print(f"VERDICT: INCONCLUSIVE — 0/{total} runs reached the page, so "
              "nothing was learned about the description.")
        print("→ fix the runs (auth, collector ID, target URLs) and re-run. "
              "This is not evidence either way.")
        return 1

    tried = len(tested)
    scope = f"{tried}/{total} run(s) that reached the page"
    if generalized == tried:
        print(f"VERDICT: GENERALIZES — {generalized}/{tried} targets returned rows "
              f"with title and URL on ≥90% of them, across "
              f"{len({t.get('ats') or 'unknown' for t, _ in tested})} platforms "
              f"({scope}).")
        print("→ one career collector covers every company. Skip the "
              "per-company fallback.")
        return 0
    if generalized:
        print(f"VERDICT: PARTIAL — {generalized}/{tried} targets clean ({scope}).")
        print("→ keep the generic collector for the platforms that work; build "
              "a separate collector per platform for the rest.")
        return 0
    print(f"VERDICT: DOES NOT GENERALIZE — 0/{tried} targets clean ({scope}).")
    print("→ fall back to per-platform collectors, chosen for ATS diversity.")
    return 1


def cmd_db(args: argparse.Namespace) -> int:
    """Create the database, optionally rebuilding it from the archive.

    `--from-snapshots` is the claim that matters: a reviewer can delete
    data/ghosthire.db, run this, and reproduce every number in the dashboard
    without spending a credit or trusting us.
    """
    from .db import DB_PATH, connect, init
    from .pipeline import rebuild

    conn = connect(args.path)
    init(conn)
    print(f"schema applied to {_rel(Path(args.path or DB_PATH))}")

    if not args.from_snapshots:
        return 0

    report = rebuild(conn)
    print(
        f"ingested {report.ingested} listing(s) {DOT} "
        f"{report.rejected} rejected as not-at-company {DOT} "
        f"{report.scored} assessed {DOT} {report.unassessed} not assessed"
    )
    if report.skipped:
        print(f"  {len(report.skipped)} snapshot(s) not ingested "
              "(collector-create envelopes, or an unrecognised name)")
    if report.unassessed:
        print("  listings with no careers-page data carry NO score, by design — "
              "absence of evidence is not evidence of absence")
    return 0


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or greater, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ghosthire.cli",
        description="Scrape job boards and company career pages via Bright Data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="run a collector and print the rows")
    p_scrape.add_argument(
        "--source",
        required=True,
        help="internshala | board_company | career "
             "(board_company and career read --company)",
    )
    p_scrape.add_argument(
        "--company", help="target slug for this source, e.g. razorpay"
    )
    p_scrape.add_argument(
        "--limit", type=_non_negative_int, default=20, help="rows to print"
    )
    p_scrape.add_argument(
        "--sync",
        action="store_true",
        help="synchronous single-URL run (25-50s server cap); good for a live demo",
    )
    p_scrape.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="async polling cap in seconds (default 600)",
    )
    p_scrape.set_defaults(func=cmd_scrape)

    p_collectors = sub.add_parser("collectors", help="list configured collectors")
    p_collectors.set_defaults(func=cmd_collectors)

    p_show = sub.add_parser("show", help="render a snapshot already on disk")
    p_show.add_argument("path")
    p_show.add_argument("--limit", type=_non_negative_int, default=20)
    p_show.set_defaults(func=cmd_show)

    p_desc = sub.add_parser("description", help="print a field description verbatim")
    p_desc.add_argument("key")
    p_desc.set_defaults(func=cmd_description)

    p_fork = sub.add_parser(
        "fork",
        help="run ONE career description against several ATS platforms (§0.3)",
    )
    p_fork.add_argument(
        "--targets",
        default="razorpay,freshworks,browserstack",
        help="comma-separated career slugs, one per ATS platform "
             "(default spans Greenhouse, SmartRecruiters and Workday)",
    )
    p_fork.add_argument("--sync", action="store_true")
    p_fork.set_defaults(func=cmd_fork)

    p_db = sub.add_parser("db", help="create or rebuild the database")
    p_db.add_argument(
        "--from-snapshots",
        action="store_true",
        help="rebuild every listing and score from data/snapshots/ (no network)",
    )
    p_db.add_argument("--path", help="database file (default data/ghosthire.db)")
    p_db.set_defaults(func=cmd_db)

    p_create = sub.add_parser("create", help="create one collector via Scraper Studio")
    p_create.add_argument("key")
    p_create.add_argument("--url", help="override the build URL")
    p_create.add_argument("--force", action="store_true")
    p_create.set_defaults(func=cmd_create)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BdataError as exc:
        _warn(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
