# Scheduling

`sweep.sh` runs every configured collector, archives each response, and rebuilds
the database from that archive. It is the third of the three places a collector
ID is wired into something real — the other two being the database (every row
carries the `c_*` that produced it) and `POST /api/scrape/trigger`.

## Running it

```bash
./sweep.sh              # every enabled target
./sweep.sh --boards     # board searches only
./sweep.sh --careers    # careers pages only
./sweep.sh --dry-run    # print the plan, spend nothing
```

`--dry-run` is worth knowing about: it prints exactly which `(source, company)`
pairs would run without making a single request, which is how to check a config
change before it costs page loads.

## Cron

Twice daily, at 07:00 and 19:00 local:

```cron
0 7,19 * * * cd /path/to/ghosthire && ./sweep.sh >> data/logs/cron.log 2>&1
```

Install it with `crontab -e`. On macOS, `cron` needs Full Disk Access to write
inside a user directory — System Settings → Privacy & Security → Full Disk
Access → add `/usr/sbin/cron`. Without it the job runs and silently fails to
write, which looks identical to not running.

Two observations twelve hours apart is the minimum for any temporal signal to
mean anything: `last_seen` only moves when a listing is seen again, and a
listing that disappears between sweeps is the strongest evidence this project
can gather. One sweep tells you what exists; two tell you what changed.

## What it does, and what it refuses to do

- **Archives before it ingests.** Every response lands in `data/snapshots/`
  first. If the script dies halfway, `db --from-snapshots` still reproduces
  every number from the files that did land.
- **Rebuilds rather than appends.** The last step is the same command a
  reviewer runs, so cron and a human converge on byte-identical numbers instead
  of drifting apart.
- **Skips careers pages it knows it cannot read.** A page marked
  `career_access: unreadable`, `js_portal` or `out_of_scope` in
  `collectors.yaml` is not worth a page load every twelve hours. The reason is
  recorded next to the target, so skipping is a decision with a stated cause
  rather than an omission.
- **Never lets one target cost the others.** Each run's exit code is captured;
  a single failure is logged and the sweep continues.
- **Keeps 30 logs** in `data/logs/`, then prunes.

## Failure modes it checks for first

The script exits before spending anything if the virtualenv is missing, `bdata`
is not on `PATH`, or the CLI is unauthenticated. A cron job that burns its slot
discovering a broken install is worse than one that says so in the first
second.
