"""Subprocess wrapper around the `bdata` CLI (Bright Data Scraper Studio).

Two rules this module exists to enforce:

1. Every collector response is written to ``data/snapshots/`` before anything
   downstream touches it. That archive is the provenance trail — the database
   is rebuilt from it, so a reviewer never has to spend a credit to see the
   same numbers we saw.
2. Every invocation produces a result object, success *or* failure, so the
   caller can record a ``scrape_runs`` row either way. Failures are data.

We shell out to the CLI rather than reimplementing the REST API: the create-
and-run flow, with its visible ``c_*`` collector IDs, is the thing being
demonstrated.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import SCRAPERS_DIR, SNAPSHOT_DIR

BDATA_BIN = os.environ.get("GHOSTHIRE_BDATA_BIN", "bdata")
COLLECTORS_YAML = SCRAPERS_DIR / "collectors.yaml"

# `scraper create` caps the natural-language description at 500 chars.
DESCRIPTION_MAX_CHARS = 500

# The CLI's own sync bounds; outside these it rejects --sync-timeout outright.
SYNC_TIMEOUT_MIN = 25
SYNC_TIMEOUT_MAX = 50

# An in-flight snapshot carries this suffix until the run completes, so that
# `data/snapshots/*.json` never matches a partial, empty or abandoned file.
PART_SUFFIX = ".part"

# Collector IDs are remote values that get written into a config file, so they
# are validated against the documented shape before they touch disk.
COLLECTOR_ID_RE = re.compile(r"^c_[a-z0-9]+$")

# Keys Bright Data uses to report a per-URL crawler failure. A record carrying
# one of these is an error report, not a job listing.
ERROR_KEYS = ("error", "error_code")

# Keys a `scraper run` envelope might hide the row list under. The CLI returns
# a bare array today; this is belt-and-braces so a shape change degrades to a
# warning instead of an empty ingest.
_ROW_CONTAINER_KEYS = ("data", "results", "rows", "items", "records", "output")

_AUTH_HINT = (
    "bdata is not authenticated. Run `bdata login` (it opens a browser), "
    "then `bdata budget balance` to confirm."
)


class BdataError(RuntimeError):
    """The CLI could not be run at all, or refused the request outright."""


@dataclass
class RunResult:
    """One `bdata scraper run` invocation."""

    collector_id: str
    source: str
    target_urls: list[str]
    status: str  # 'success' | 'partial' | 'failed'
    rows: list[dict[str, Any]] = field(default_factory=list)
    crawler_errors: list[dict[str, Any]] = field(default_factory=list)
    snapshot_path: Path | None = None
    started_at: str = ""
    completed_at: str = ""
    duration_s: float = 0.0
    error: str | None = None

    @property
    def rows_returned(self) -> int:
        return len(self.rows)

    @property
    def ok(self) -> bool:
        return self.status in ("success", "partial")


@dataclass
class CreateResult:
    """One `bdata scraper create` invocation."""

    collector_id: str | None
    name: str | None
    status: str
    snapshot_path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# --------------------------------------------------------------------------
# collectors.yaml
# --------------------------------------------------------------------------

def load_collectors(path: Path | None = None) -> dict[str, Any]:
    """Parse scrapers/collectors.yaml and resolve each description_ref."""
    path = path or COLLECTORS_YAML
    if not path.exists():
        raise BdataError(f"collector config not found: {path}")
    doc = yaml.safe_load(path.read_text()) or {}
    descriptions = doc.get("descriptions") or {}

    for collector in doc.get("collectors") or []:
        ref = collector.get("description_ref")
        if ref:
            if ref not in descriptions:
                raise BdataError(
                    f"collector {collector.get('key')!r} references unknown "
                    f"description {ref!r}"
                )
            collector["description"] = descriptions[ref].strip()
    return doc


def get_collector(key: str, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = doc or load_collectors()
    for collector in doc.get("collectors") or []:
        if collector.get("key") == key:
            return collector
    known = ", ".join(c.get("key", "?") for c in doc.get("collectors") or [])
    raise BdataError(f"no collector with key {key!r}. Known keys: {known}")


def find_collectors(
    source: str | None = None,
    kind: str | None = None,
    doc: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    doc = doc or load_collectors()
    out = []
    for collector in doc.get("collectors") or []:
        if source and collector.get("source") != source:
            continue
        if kind and collector.get("kind") != kind:
            continue
        out.append(collector)
    return out


def set_collector_id(key: str, collector_id: str, path: Path | None = None) -> None:
    """Write a freshly created ``c_*`` ID back into collectors.yaml.

    Done as a targeted line edit rather than a yaml round-trip so the file's
    comments and block scalars survive — the field descriptions in it are a
    deliverable, not just config.

    The value arrives from a remote API and is about to be written into a file
    every later command parses, so it is validated first: a YAML metacharacter
    or a newline in it would corrupt the config, and that corruption would land
    immediately after a paid ten-minute generation, taking the ID with it.
    """
    if not isinstance(collector_id, str) or not COLLECTOR_ID_RE.match(collector_id):
        raise BdataError(
            f"refusing to write {collector_id!r} as a collector_id: "
            "expected the form c_<alphanumeric>"
        )

    path = path or COLLECTORS_YAML
    original = path.read_text()
    lines = original.splitlines(keepends=True)

    # `- key: foo`, tolerating quotes and a trailing comment.
    key_re = re.compile(
        rf"^\s*-\s+key:\s*[\"']?{re.escape(key)}[\"']?\s*(#.*)?$"
    )
    any_key_re = re.compile(r"^\s*-\s+key:")

    target = None
    in_block = False
    for i, line in enumerate(lines):
        if key_re.match(line):
            in_block = True
            continue
        if in_block:
            if any_key_re.match(line):
                # End of this collector's block without finding the field.
                # Keep scanning: another block may carry the same key.
                in_block = False
                if key_re.match(line):
                    in_block = True
                continue
            if re.match(r"^\s*collector_id:", line):
                target = i
                break

    if target is None:
        raise BdataError(f"could not find a collector_id line for key {key!r} in {path}")

    indent = lines[target][: len(lines[target]) - len(lines[target].lstrip())]
    lines[target] = f"{indent}collector_id: {collector_id}\n"
    updated = "".join(lines)

    # Parse before publishing, then swap atomically: a half-written config is
    # worse than no write at all.
    try:
        yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise BdataError(f"edit would leave {path} unparseable: {exc}") from exc

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(updated)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# running the CLI
# --------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reserve_snapshot(source: str, prefix: str = "") -> Path:
    """Reserve a unique snapshot name; return the path to write to.

    The returned path ends in ``.json.part``. It is a *reservation*, not a
    snapshot: call `publish_snapshot` once the CLI has put real bytes in it, or
    `discard_snapshot` when the run produced nothing.

    Two properties this buys, both load-bearing:

    1. ``data/snapshots/*.json`` only ever matches complete files. Rebuilding
       the database from that glob without spending a credit is the provenance
       promise, and a concurrent reader that hits a half-written or empty file
       breaks it.
    2. Names are unique. Timestamps are second-granular and two runs can finish
       inside one second, so a bare stamp silently overwrites the earlier run's
       evidence. Collisions get a suffix.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{stamp}_{prefix}{slug}"
    for n in range(1, 1000):
        stem = base if n == 1 else f"{base}_{n}"
        # The published name is checked too, so a reservation can never later
        # collide with a snapshot that is already on disk.
        if (SNAPSHOT_DIR / f"{stem}.json").exists():
            continue
        try:
            part = SNAPSHOT_DIR / f"{stem}.json{PART_SUFFIX}"
            part.touch(exist_ok=False)
            return part
        except FileExistsError:
            continue
    raise BdataError(f"cannot find a free snapshot name for {slug}")


def published_path(part: Path) -> Path:
    """The name a reservation takes once it holds real bytes."""
    if part.name.endswith(PART_SUFFIX):
        return part.with_name(part.name[: -len(PART_SUFFIX)])
    return part


def publish_snapshot(part: Path) -> Path | None:
    """Promote a written reservation to its final name, atomically.

    Returns ``None`` when there is nothing worth keeping. An empty file is not
    evidence, and a caller that reports its path is claiming an archive it does
    not have — which is worst on the timeout path, where the run was also the
    most expensive one of the night.
    """
    try:
        if not part.exists() or part.stat().st_size == 0:
            discard_snapshot(part)
            return None
        final = published_path(part)
        os.replace(part, final)
        return final
    except OSError:
        return None


def discard_snapshot(part: Path) -> None:
    """Drop a reservation that never received data."""
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass


def _has_bytes(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _reservation_for(out_path: Path | None, source: str, prefix: str = "") -> Path:
    """The ``.part`` path to write to for this run.

    An explicit ``out_path`` names the *published* snapshot; the CLI still
    writes beside it under ``.part`` so the same guarantee holds for callers
    that choose their own filename.
    """
    if out_path is None:
        return reserve_snapshot(source, prefix=prefix)
    part = Path(str(out_path) + PART_SUFFIX)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.touch(exist_ok=True)
    return part


def cli_available() -> bool:
    return shutil.which(BDATA_BIN) is not None


def _invoke(
    cmd: list[str], timeout: int, stream: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the CLI. With ``stream``, let its progress reach the terminal.

    A batch run polls for minutes. Capturing stdout hides the poll counter
    entirely, which leaves no way to tell a slow run from a hung one — so for
    interactive use stdout is inherited and only stderr is captured, which is
    still enough to explain a failure afterwards. The data is read from the
    ``-o`` file either way, so nothing is lost by not capturing stdout.
    """
    if not cli_available():
        raise BdataError(
            f"`{BDATA_BIN}` not found on PATH. Install it with "
            "`npm install -g @brightdata/cli`."
        )
    if not stream:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise BdataError(
                f"`{' '.join(cmd[:4])}` timed out after {timeout}s"
            ) from exc

    # The CLI writes its poll counter to STDERR (console.error), not stdout, so
    # stderr is teed — echoed live to the terminal and kept, because the auth
    # hint and crawler errors arrive on the same stream. stdout stays piped for
    # the payload fallback. A reader thread keeps the pipes from deadlocking.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", bufsize=1,
    )
    captured: list[str] = []

    def _tee() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            # Flush stdout first: piping the command makes stdout block-
            # buffered while stderr stays unbuffered, so poll output would
            # otherwise land above the line announcing the run it belongs to.
            sys.stdout.flush()
            sys.stderr.write(line)
            sys.stderr.flush()
            captured.append(line)

    pump = threading.Thread(target=_tee, daemon=True)
    pump.start()
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        pump.join(timeout=5)
        raise BdataError(
            f"`{' '.join(cmd[:4])}` timed out after {timeout}s"
        ) from exc
    pump.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout or "", "".join(captured))


def _looks_unauthenticated(text: str) -> bool:
    return "no api key found" in (text or "").lower()


def is_error_row(row: Any) -> bool:
    """True when a record is Bright Data reporting a failure, not a listing."""
    return isinstance(row, dict) and any(row.get(key) for key in ERROR_KEYS)


def _all_records(payload: Any) -> list[dict[str, Any]]:
    """Every dict record in the payload, errors included."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        # Prefer a populated container: an empty `data` alongside a populated
        # `results` would otherwise be read as zero rows.
        best: list[dict[str, Any]] | None = None
        for key in _ROW_CONTAINER_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                records = [row for row in value if isinstance(row, dict)]
                if records:
                    return records
                if best is None:
                    best = records
        if best is not None:
            return best
        if any(k in payload for k in ("job_title", "title", "company_name")):
            return [payload]
    return []


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    """The data records only.

    Error reports are excluded deliberately. They share the array with real
    listings, and counting them as rows turns a total scrape failure into
    "success, 1 row" — and can render an error string in a job-title column.
    """
    return [row for row in _all_records(payload) if not is_error_row(row)]


def extract_errors(payload: Any) -> list[dict[str, Any]]:
    """The error records only."""
    return [row for row in _all_records(payload) if is_error_row(row)]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def run_collector(
    collector_id: str,
    urls: list[str],
    source: str,
    sync: bool = False,
    timeout: int = 600,
    out_path: Path | None = None,
    stream: bool = False,
) -> RunResult:
    """Run a collector over ``urls`` and archive the raw response.

    Never raises on a failed *scrape* — the caller records the failure. Only
    raises when the CLI itself is unusable (missing binary, hard timeout).
    """
    # ---- Every argument check runs before a reservation is taken. Allocating
    # the snapshot name first left an empty *.json on disk for each rejected
    # call — a file that archives nothing while looking exactly like evidence.
    if not collector_id:
        raise BdataError(
            f"no collector_id configured for source {source!r}. "
            "Run scrapers/create_collectors.sh first."
        )
    if not urls:
        raise BdataError(f"no target URLs for source {source!r}")
    if sync and len(urls) > 1:
        # The CLI rejects --sync with --urls, but we pass a positional URL, so
        # it would never see the other targets: they would be dropped in
        # silence and the snapshot would under-report what it covers.
        raise BdataError(
            f"--sync takes a single URL; {len(urls)} configured for "
            f"{source!r}. Drop --sync to run them as a batch."
        )

    part = _reservation_for(out_path, source)
    cmd = [BDATA_BIN, "scraper", "run", collector_id]
    if sync:
        cap = min(max(timeout, SYNC_TIMEOUT_MIN), SYNC_TIMEOUT_MAX)
        # --timeout is load-bearing even here. On a realtime page-limit error
        # the CLI silently falls back to batch polling, and batch reads its cap
        # from --timeout — defaulting to 3600s when the flag is absent. Without
        # this, a "25-50s" sync run can poll for an hour.
        cmd += [urls[0], "--sync", "--sync-timeout", str(cap), "--timeout", str(cap)]
        wall = cap + 60
    else:
        cmd += ["--urls", ",".join(urls), "--timeout", str(timeout)]
        wall = timeout + 60
    cmd += ["--json", "--pretty", "-o", str(part)]

    started_at = _utcnow()
    clock = time.monotonic()

    # ---- D4: a run that times out is still a run. The module promises a
    # result object for every invocation so the caller can record it, and the
    # most expensive failure is the one that must not vanish.
    try:
        proc = _invoke(cmd, timeout=wall, stream=stream)
    except BdataError as exc:
        if "timed out" not in str(exc):
            discard_snapshot(part)
            raise
        return RunResult(
            collector_id=collector_id,
            source=source,
            target_urls=urls,
            status="failed",
            # Keep whatever the CLI managed to write; report no path at all
            # when it wrote nothing, rather than pointing at an empty file.
            snapshot_path=publish_snapshot(part),
            started_at=started_at,
            completed_at=_utcnow(),
            duration_s=round(time.monotonic() - clock, 1),
            error=str(exc),
        )
    duration = round(time.monotonic() - clock, 1)

    payload = _read_json(part) if _has_bytes(part) else None
    if payload is None and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            part.write_text(json.dumps(payload, indent=2))
        except json.JSONDecodeError:
            payload = None
    snapshot = publish_snapshot(part)

    rows = extract_rows(payload)
    crawler_errors = extract_errors(payload)
    error = None
    if proc.returncode != 0 or payload is None:
        status = "failed"
        error = (proc.stderr or proc.stdout or "").strip()[:2000] or "no output"
        if _looks_unauthenticated(error):
            error = _AUTH_HINT
    elif crawler_errors and not rows:
        # ---- D2: the collector ran, and every record it returned is an error
        # report. That is a failed scrape, not a successful one with N rows.
        status = "failed"
        error = "; ".join(
            str(e.get("error") or e.get("error_code")) for e in crawler_errors[:3]
        )[:2000]
    elif not rows:
        # Genuinely empty is not an error: a careers page with no open roles is
        # a real observation, and one the scoring engine depends on.
        status = "partial"
        error = "collector returned zero rows"
    else:
        status = "success"

    return RunResult(
        collector_id=collector_id,
        source=source,
        target_urls=urls,
        status=status,
        rows=rows,
        crawler_errors=crawler_errors,
        snapshot_path=snapshot,
        started_at=started_at,
        completed_at=_utcnow(),
        duration_s=duration,
        error=error,
    )


def create_collector(
    url: str,
    description: str,
    name: str | None = None,
    timeout: int = 600,
    max_retries: int = 4,
    out_path: Path | None = None,
) -> CreateResult:
    """Build a collector from a plain-English description.

    Generation takes 5-10 minutes server-side and is rate-limited by an
    AI-Flow concurrent-job cap, which the CLI retries through on its own.
    """
    description = description.strip()
    if len(description) > DESCRIPTION_MAX_CHARS:
        raise BdataError(
            f"description is {len(description)} chars, over the "
            f"{DESCRIPTION_MAX_CHARS}-char cap"
        )

    part = _reservation_for(out_path, name or "collector", prefix="create_")
    cmd = [BDATA_BIN, "scraper", "create", url, description]
    if name:
        cmd += ["--name", name]
    cmd += [
        "--json", "--pretty", "-o", str(part),
        "--timeout", str(timeout), "--max-retries", str(max_retries),
    ]

    # ---- D14: generation takes 5-10 minutes and the CLI reports each step on
    # stderr. Capturing that turns the longest operation in the project into a
    # silent hour, which is exactly what streaming exists to prevent.
    try:
        proc = _invoke(cmd, timeout=timeout * (max_retries + 1) + 600, stream=True)
    except BdataError:
        discard_snapshot(part)
        raise
    payload = _read_json(part) if _has_bytes(part) else None
    if payload is None and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            part.write_text(json.dumps(payload, indent=2))
        except json.JSONDecodeError:
            payload = None
    snapshot = publish_snapshot(part)

    if not isinstance(payload, dict):
        error = (proc.stderr or proc.stdout or "").strip()[:2000] or "no output"
        if _looks_unauthenticated(error):
            error = _AUTH_HINT
        return CreateResult(None, name, "failed", snapshot, {}, error)

    return CreateResult(
        collector_id=payload.get("collector_id"),
        name=payload.get("name") or name,
        status=payload.get("status") or ("created" if payload.get("collector_id") else "unknown"),
        snapshot_path=snapshot,
        raw=payload,
        error=None,
    )
