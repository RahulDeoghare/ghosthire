# Deployment

## Vercel — what works and what does not

The dashboard and every read endpoint run on Vercel as a Python serverless
function. Two things do not, and cannot:

| | |
|---|---|
| `POST /api/scrape/trigger` | shells out to `bdata`, a Node CLI absent from the Python runtime |
| `sweep.sh` on cron | same reason, plus a writable disk and minutes of runtime |

Both fail honestly rather than mysteriously: the trigger returns **501** with a
message naming the cause. Collecting new data is a local operation; the
deployment serves what was already collected.

That split is not a workaround, it is the architecture. The database has always
been a cache of `data/snapshots/`, so a host that can only read is a host that
can serve everything except new collection.

### How it works

`api/index.py` rebuilds the database into `/tmp` on cold start, from the
snapshot archive committed to the repo — the same `db --from-snapshots` a
reviewer runs. Locally and in production the numbers therefore come from one
source and cannot drift.

Cold start is about **0.4s** including the rebuild, for a 348 KB archive. A
warm container skips it. If the archive ever grows enough for that to hurt,
build the database at deploy time and ship it read-only; the code path is
unchanged.

Two smaller accommodations for a read-only filesystem:

- `GHOSTHIRE_DB` overrides the database path (`.env.example` documented this
  long before it worked).
- A database on an unwritable path is opened explicitly read-only, so SQLite
  does not try to create the journal it would otherwise own. WAL is used where
  the path is writable, which is the normal case and the one that matters:
  the API serves pages while a sweep ingests.

### If you ever ship a prebuilt database

Use `db.read_only_copy()`. It differs from a plain file copy in one respect and
that respect is the whole point: **a WAL database cannot be read at all from a
read-only filesystem**, because answering even a SELECT means writing its
sidecar files. Copy a WAL database onto a read-only host and the first query
returns `attempt to write a readonly database` — at request time, not at deploy
time. `read_only_copy` rewrites the journal mode to DELETE and drops the
sidecars, making the mode part of the artifact rather than a runtime surprise.

The current deployment does not need this: it rebuilds into `/tmp`, which is
writable. The helper exists for the day the archive grows enough that building
at deploy time beats building on cold start.

### Deploying

```bash
npm i -g vercel
vercel            # preview
vercel --prod     # production
```

No environment variables are required. `GHOSTHIRE_DB` defaults to
`/tmp/ghosthire.db`, and no Bright Data credentials are needed because the
deployment never calls the API.

`vercel.json` pins `includeFiles` to `{ghosthire,scrapers,web,data}/**`. Without
it the function bundles only `api/`, and the app fails at import — the package,
the collector config, the dashboard and the snapshot archive all have to travel
with it.

### Verifying a deployment

```bash
curl -s https://<your-app>.vercel.app/api/meta | jq '{assessed, not_assessed}'
curl -s -o /dev/null -w '%{http_code}\n' https://<your-app>.vercel.app/
```

Expect the same counts the local instance reports. If they differ, the archive
and the deployment are out of step — redeploy rather than reconcile by hand.

## Hosting the whole thing instead

To keep the trigger and the scheduled sweep, use a host that runs a process
with a writable disk — Railway, Render or Fly. There the app runs exactly as it
does locally:

```bash
.venv/bin/python -m ghosthire.cli db --from-snapshots
.venv/bin/uvicorn ghosthire.api:app --host 0.0.0.0 --port $PORT
```

`bdata` must be installed (`npm i -g @brightdata/cli`) and authenticated for
collection to work, and `sweep.sh` needs a cron entry — see
[`scheduling.md`](scheduling.md).
