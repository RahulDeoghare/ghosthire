#!/usr/bin/env bash
#
# The create flow, in the open.
#
# Builds every collector listed in scrapers/collectors.yaml that does not yet
# have a c_* ID, by handing Bright Data Scraper Studio a plain-English
# description of the fields we want. The returned collector ID is written back
# into collectors.yaml, and from then on every scraped row carries it.
#
#   ./scrapers/create_collectors.sh                 # every pending collector
#   ./scrapers/create_collectors.sh career_generic  # just one
#
# Generation takes 5-10 minutes per collector server-side. The CLI polls and
# retries through Bright Data's AI-Flow concurrent-job cap on its own, so this
# script is meant to be left running.
#
# Written for bash 3.2 (what macOS ships) — no mapfile, no associative arrays.

set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
BDATA="${BDATA:-bdata}"

if ! command -v "$BDATA" >/dev/null 2>&1; then
  echo "error: '$BDATA' not on PATH. npm install -g @brightdata/cli" >&2
  exit 1
fi

# Capture first, then match. Piping straight into `grep -q` lets grep exit on
# the first match, which SIGPIPEs bdata — and under `pipefail` that turns the
# whole condition false, so the guard would never fire.
BALANCE_BEFORE="$("$BDATA" budget balance 2>&1 || true)"
if printf '%s' "$BALANCE_BEFORE" | grep -qi "no api key"; then
  echo "error: bdata is not authenticated. Run 'bdata login', then retry." >&2
  exit 1
fi

echo "== budget before =="
printf '%s\n' "$BALANCE_BEFORE"
echo

# Which collectors still need building: the ones named on the command line, or
# every enabled collector without a c_* ID.
if [ $# -gt 0 ]; then
  KEYS="$*"
else
  KEYS="$("$PY" - <<'PYEOF'
from ghosthire.bdata import load_collectors
for c in load_collectors()["collectors"]:
    if c.get("enabled", True) and not c.get("collector_id"):
        print(c["key"])
PYEOF
)"
fi

if [ -z "$KEYS" ]; then
  echo "every enabled collector already has a c_* ID. Nothing to create."
  exit 0
fi

echo "creating:" $KEYS
echo
failed=""

for key in $KEYS; do
  # key, build URL, description ref and template name in one lookup.
  meta="$("$PY" -c "
import sys
from ghosthire.bdata import get_collector
c = get_collector(sys.argv[1])
targets = c.get('targets') or []
print('\t'.join([
    targets[0]['url'] if targets else '',
    c.get('description_ref') or '',
    c.get('name') or sys.argv[1],
]))" "$key")"

  url="$(printf '%s' "$meta" | cut -f1)"
  ref="$(printf '%s' "$meta" | cut -f2)"
  name="$(printf '%s' "$meta" | cut -f3)"

  if [ -z "$url" ]; then
    echo "-- $key: no target URL configured, skipping" >&2
    continue
  fi

  description="$("$PY" -m ghosthire.cli description "$ref")"

  # Timestamped, never a fixed name: a re-created collector must not erase the
  # record of the one before it. Failed generations are evidence too.
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  out="data/snapshots/${stamp}_create_${key}.json"

  echo "-- $key"
  echo "   url:  $url"
  echo "   desc: $ref (${#description} chars)"
  echo "   out:  $out"
  echo

  # The line that matters. Everything around it is bookkeeping.
  #
  # `set -e` would abort the whole script here on a failed generation, so the
  # failure is caught explicitly: with several collectors queued, one that hits
  # the trial cap or a 429 must not cost us the rest of the batch. A failed
  # generation is recorded and the loop moves on.
  if ! "$BDATA" scraper create "$url" "$description" \
    --name "$name" \
    --json --pretty \
    -o "$out"
  then
    echo "   !! generation failed for $key — see $out if it was written" >&2
    failed="$failed $key"
    continue
  fi

  # Tolerate a missing or malformed file: without this the json.load raises and
  # aborts before the guard below can report anything useful.
  cid="$("$PY" -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    data = {}
print((data.get('collector_id') or '') if isinstance(data, dict) else '')" "$out" 2>/dev/null || true)"

  if [ -z "$cid" ]; then
    echo "   !! no collector_id came back — see $out" >&2
    failed="$failed $key"
    continue
  fi

  echo "   collector_id: $cid"
  "$PY" -c "
import sys
from ghosthire.bdata import set_collector_id
set_collector_id(sys.argv[1], sys.argv[2])" "$key" "$cid"
  echo "   written into scrapers/collectors.yaml"
  echo
done

echo "== budget after =="
"$BDATA" budget balance || true
echo
"$PY" -m ghosthire.cli collectors

if [ -n "$failed" ]; then
  echo
  echo "failed to create:$failed" >&2
  exit 1
fi
