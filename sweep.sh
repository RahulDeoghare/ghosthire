#!/usr/bin/env bash
#
# One sweep: run every enabled collector, ingest what comes back, re-score.
#
#   ./sweep.sh              # board + careers pages for every configured target
#   ./sweep.sh --boards     # board searches only
#   ./sweep.sh --careers    # careers pages only
#   ./sweep.sh --dry-run    # print what would run, spend nothing
#
# Written to be run by cron, so it is quiet on success, loud on failure, and
# never leaves the database half-written: every run is archived to
# data/snapshots/ first and the database is rebuilt from that archive at the
# end. If this script dies halfway, the next `db --from-snapshots` still
# reproduces every number from the files that did land.
#
# Bash 3.2 (what macOS ships) — no mapfile, no associative arrays.

set -uo pipefail

cd "$(dirname "$0")"
PY="${PY:-.venv/bin/python}"
LOG_DIR="${GHOSTHIRE_LOG_DIR:-data/logs}"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/sweep_$STAMP.log"

MODE="all"
DRY=""
# Per-target polling cap. The CLI defaults to 600s, and a target that returns
# nothing spends all of it — fourteen of those is a sweep that runs for hours
# and overlaps the next cron slot. A board search that has not answered in two
# minutes is not going to.
TIMEOUT="${GHOSTHIRE_SWEEP_TIMEOUT:-120}"
for arg in "$@"; do
  case "$arg" in
    --boards)  MODE="boards" ;;
    --careers) MODE="careers" ;;
    --dry-run) DRY="yes" ;;
    --timeout=*) TIMEOUT="${arg#*=}" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG"; }

log "sweep start (mode=$MODE${DRY:+, dry-run})"

# Fail fast and legibly rather than burning a cron slot on a broken install.
if [ ! -x "$PY" ]; then
  log "FATAL: $PY not found — create the venv first"
  exit 1
fi
if ! command -v bdata >/dev/null 2>&1; then
  log "FATAL: bdata not on PATH — npm install -g @brightdata/cli"
  exit 1
fi
if bdata budget balance 2>&1 | grep -qi "no api key"; then
  log "FATAL: bdata is not authenticated — run 'bdata login'"
  exit 1
fi

# Which (source, company) pairs to run, straight from the config so this script
# never holds its own copy of the target list.
TARGETS="$("$PY" - "$MODE" <<'PYEOF'
import sys
from ghosthire.bdata import load_collectors

mode = sys.argv[1]
for collector in load_collectors()["collectors"]:
    if not collector.get("collector_id") or not collector.get("enabled", True):
        continue
    kind = collector.get("kind")
    if mode == "boards" and kind != "board":
        continue
    if mode == "careers" and kind != "career":
        continue
    for target in collector.get("targets") or []:
        slug = target.get("slug")
        # A careers page we already know we cannot read is not worth a page
        # load every twelve hours. The reason lives in collectors.yaml.
        if kind == "career" and target.get("career_access") in (
                "out_of_scope", "unreadable", "js_portal"):
            continue
        if slug:
            print(f"{collector['source']}\t{slug}")
PYEOF
)"

if [ -z "$TARGETS" ]; then
  log "nothing to run"
  exit 0
fi

COUNT="$(printf '%s\n' "$TARGETS" | wc -l | tr -d ' ')"
log "$COUNT target(s) to sweep, ${TIMEOUT}s cap each"

if [ -n "$DRY" ]; then
  printf '%s\n' "$TARGETS" | while IFS=$'\t' read -r source slug; do
    log "  would run: --source $source --company $slug"
  done
  log "dry run — nothing spent"
  exit 0
fi

failed=0
ran=0
printf '%s\n' "$TARGETS" | while IFS=$'\t' read -r source slug; do
  log "  $source / $slug"
  # Each target is independent: one failure must not cost the rest of the
  # sweep, so the exit code is captured rather than aborting under set -e.
  if ! "$PY" -m ghosthire.cli scrape --source "$source" --company "$slug" \
        --timeout "$TIMEOUT" >>"$LOG" 2>&1; then
    log "    FAILED (see $LOG)"
  fi
done

# Rebuild from the archive rather than trusting the incremental writes above.
# This is the same command a reviewer runs, so cron and a human converge on
# byte-identical numbers.
log "rebuilding from snapshots"
if ! "$PY" -m ghosthire.cli db --from-snapshots >>"$LOG" 2>&1; then
  log "FATAL: rebuild failed — the database may be stale, see $LOG"
  exit 1
fi

SUMMARY="$("$PY" - <<'PYEOF'
from ghosthire.db import connect, DB_PATH
c = connect(DB_PATH)
q = lambda s: c.execute(s).fetchone()[0]
print(f"{q('SELECT COUNT(*) FROM job_listings')} listings · "
      f"{q('SELECT COUNT(*) FROM ghost_scores WHERE career_page_checked=1')} assessed · "
      f"{q('SELECT COUNT(*) FROM scrape_runs')} runs archived")
PYEOF
)"
log "done — $SUMMARY"

# Keep the log directory from growing without bound on a twice-daily cron.
ls -1t "$LOG_DIR"/sweep_*.log 2>/dev/null | tail -n +31 | while read -r old; do
  rm -f "$old"
done
