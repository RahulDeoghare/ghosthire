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
    extract_rows,
    find_collectors,
    get_collector,
    load_collectors,
    run_collector,
    set_collector_id,
)

DOT = "·"

# Collector output field names vary by target; accept the obvious synonyms
# rather than losing a row to a naming difference.
FIELD_ALIASES = {
    "company": ("company_name", "company", "employer", "organisation", "organization"),
    "title": ("job_title", "title", "role", "position"),
    "location": ("location", "job_location", "city", "place"),
    "posted": ("date_posted", "posted", "posted_date", "posted_on", "date"),
    "url": ("job_url", "url", "link", "job_link", "apply_url"),
    "department": ("department", "team", "function", "category"),
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
        text = _scalar(value).translate(_CONTROL_CHARS)
        text = " ".join(text.split())
        if text:
            return text
    return ""


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

    if args.source == "career":
        collector = get_collector("career_generic", doc)
        targets = collector.get("targets") or []
        if args.company:
            targets = [t for t in targets if t.get("slug") == args.company]
            if not targets:
                slugs = ", ".join(
                    t.get("slug", "?") for t in collector.get("targets") or []
                )
                print(
                    f"no career target with slug {args.company!r}. Known: {slugs}",
                    file=sys.stderr,
                )
                return 2
        collectors = [(collector, targets)]
    else:
        matches = [
            c for c in find_collectors(source=args.source, doc=doc)
            if c.get("enabled", True)
        ]
        if not matches:
            print(
                f"no enabled collector for source {args.source!r}", file=sys.stderr
            )
            return 2
        collectors = [(c, c.get("targets") or []) for c in matches]

    failures = 0
    for collector, targets in collectors:
        urls = [t["url"] for t in targets if t.get("url")]
        if not urls:
            print(f"{collector['key']}: no target URLs configured", file=sys.stderr)
            failures += 1
            continue

        source = collector["source"]
        if args.source == "career" and args.company:
            source = f"career_{args.company}"

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
            print(f"{collector['key']}: {exc}", file=sys.stderr)
            failures += 1
            continue

        print_run_header(result)
        if result.status == "failed":
            print(f"  status: FAILED {DOT} {result.error}", file=sys.stderr)
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
        print(f"no such snapshot: {path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except UnicodeDecodeError:
        print(f"{path} is not text — snapshots are UTF-8 JSON", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"cannot read {path}: {exc.strerror}", file=sys.stderr)
        return 2

    rows = extract_rows(payload)
    fixture = isinstance(payload, dict) and payload.get("_fixture") is True
    label = "FIXTURE (hand-written, not collector output)" if fixture else "snapshot"
    print(f"{label} {DOT} {_rel(path)} {DOT} {len(rows)} rows")
    if isinstance(payload, dict) and payload.get("collector_id"):
        print(f"collector {payload['collector_id']}")
    print()
    print_table(rows, limit=args.limit)
    return 0


def cmd_description(args: argparse.Namespace) -> int:
    """Print a field description verbatim. Used by create_collectors.sh."""
    doc = load_collectors()
    descriptions = doc.get("descriptions") or {}
    if args.key not in descriptions:
        print(
            f"no description {args.key!r}. Known: {', '.join(descriptions)}",
            file=sys.stderr,
        )
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
        print(f"{args.key} has no target URL to build against", file=sys.stderr)
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
        print(f"create failed: {result.error}", file=sys.stderr)
        return 1

    print(f"collector {result.collector_id} {DOT} {result.name} {DOT} {result.status}")
    print(f"→ {_rel(result.snapshot_path)}")
    set_collector_id(args.key, result.collector_id)
    print(f"wrote collector_id into scrapers/collectors.yaml under {args.key}")
    return 0


# Fields the generic career description asks Scraper Studio for. The first two
# are load-bearing: without a title and a URL a row cannot be matched or
# verified by a human.
CAREER_REQUIRED_FIELDS = ("title", "url")
CAREER_OPTIONAL_FIELDS = ("location", "department")


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
        print(
            "no targets given. The fork test compares one description across "
            "several ATS platforms, so it needs at least two.",
            file=sys.stderr,
        )
        return 2

    targets = []
    for slug in wanted:
        match = next((t for t in all_targets if t.get("slug") == slug), None)
        if match is None:
            known = ", ".join(t.get("slug", "?") for t in all_targets)
            print(f"unknown target {slug!r}. Known: {known}", file=sys.stderr)
            return 2
        targets.append(match)

    platforms = sorted({t.get("ats", "unknown") for t in targets})
    if len(platforms) < 2:
        print(
            f"all {len(targets)} target(s) run {platforms[0]!r}. The question "
            "is whether one description survives DIFFERENT platforms, so pick "
            "targets that do not share an ATS.",
            file=sys.stderr,
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
            print(f"error: {exc}", file=sys.stderr)
            return 1
        results.append((target, result))
        print(
            f"  {target['slug']:<14} {target.get('ats', '?'):<11} "
            f"{result.status:<8} {result.rows_returned:>4} rows  "
            f"{result.duration_s}s  → {_rel(result.snapshot_path)}"
        )

    print()
    header = f"{'TARGET':<14} {'ATS':<11} {'ROWS':>5}  "
    header += "  ".join(f"{f.upper():>10}" for f in
                        CAREER_REQUIRED_FIELDS + CAREER_OPTIONAL_FIELDS)
    print(header)
    print("-" * len(header))

    generalized = 0
    for target, result in results:
        cells = []
        for fld in CAREER_REQUIRED_FIELDS + CAREER_OPTIONAL_FIELDS:
            cells.append(f"{_coverage(result.rows, fld) * 100:9.0f}%")
        print(
            f"{target['slug']:<14} {target.get('ats', '?'):<11} "
            f"{result.rows_returned:>5}  " + "  ".join(cells)
        )
        if result.rows and all(
            _coverage(result.rows, fld) >= 0.9 for fld in CAREER_REQUIRED_FIELDS
        ):
            generalized += 1

    print()
    total = len(results)
    if not total:
        print("VERDICT: NONE — no target produced a result, so nothing is proven.")
        return 1
    if generalized == total:
        print(f"VERDICT: GENERALIZES — {generalized}/{total} targets returned rows "
              f"with title and URL on ≥90% of them, across "
              f"{len(platforms)} platforms.")
        print("→ one career collector covers every company. Skip the "
              "per-company fallback.")
        return 0
    if generalized:
        print(f"VERDICT: PARTIAL — {generalized}/{total} targets clean "
              f"across {len(platforms)} platforms.")
        print("→ keep the generic collector for the platforms that work; build "
              "a separate collector per platform for the rest.")
        return 0
    print(f"VERDICT: DOES NOT GENERALIZE — 0/{total} targets clean.")
    print("→ fall back to per-platform collectors, chosen for ATS diversity.")
    return 1


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
        help="internshala | secondary | career (career reads --company)",
    )
    p_scrape.add_argument("--company", help="career target slug, e.g. razorpay")
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
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
