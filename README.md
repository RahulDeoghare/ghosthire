# GhostHire

Detects **ghost jobs** — roles advertised on a job board that the hiring company
does not list on its own careers page.

Every claim it makes cites two URLs and the collector that produced them, so any
finding can be checked, or refuted, in about ten seconds.

```
collector c_mt1senswibym6o5va · board_company_razorpay · 3 rows · 12.9s
COMPANY      TITLE                                       LOCATION    POSTED
Razorpay     Associate, Startup Accounts                 Bangalore   —
Razorpay     Associate Technical Program Manager         Bangalore   —
Razorpay     Associate Manager, Key Accounts Management  Remote      1 week ago
→ data/snapshots/20260823T070349Z_board-company-razorpay.json
```

Of those three, one appears on Razorpay's own Greenhouse board and two do not.

---

## The problem

A ghost job is a listing that stays up after hiring stopped — to collect
résumés, to look like growth, or because nobody took it down. Job seekers spend
real effort on them. They are hard to spot from one side, because a single
listing looks identical whether or not the role exists.

They are much easier to spot from two sides. If a company advertises a role on a
board but does not list it on the careers page it controls, that gap is worth
explaining. GhostHire reads both sides and reports the difference.

**It reports a discrepancy, not a verdict.** A gap has innocent explanations —
the careers page lags, the role was filled yesterday, the board listing is a
recruiter's. The dashboard shows both source URLs beside every score precisely
so a reader can reach their own conclusion.

## Quick start

Requires Python 3.14, Node (for the Bright Data CLI), and about two minutes.

```bash
git clone https://github.com/RahulDeoghare/ghosthire.git
cd ghosthire
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Rebuild the database from the archived collector responses.
# No network, no API key, no credits — this is the point.
.venv/bin/python -m ghosthire.cli db --from-snapshots

.venv/bin/uvicorn ghosthire.api:app --reload
```

Open <http://localhost:8000/>. The API is browsable at `/docs`.

**You do not need Bright Data credentials to evaluate this project.** Every
collector response ever received is committed under `data/snapshots/`, and the
database is a cache of that archive. `db --from-snapshots` reproduces every
number in the dashboard from those files. Credentials are only needed to collect
*new* data:

```bash
npm install -g @brightdata/cli
bdata login
.venv/bin/python -m ghosthire.cli scrape --source career --company razorpay
```

Run the tests with `.venv/bin/python -m pytest -q` (209 tests).

## How it works

```
board search ─┐
              ├─→ archive ─→ ingest ─→ match ─→ score ─→ API ─→ dashboard
careers page ─┘   (JSON)     (guard)   (gate)   (gated)
```

1. **Collect.** A Bright Data Scraper Studio collector reads a job board,
   filtered to one company. Another reads that company's careers page.
2. **Archive.** Every response is written to `data/snapshots/` before anything
   parses it, successes and failures alike.
3. **Ingest.** Rows are normalized and stored — but only if the row's employer
   matches the company it is filed under. See *the guard* below.
4. **Match.** Board listing to careers-page role: exact on normalized company,
   then a hard level gate, then `token_sort_ratio ≥ 85`.
5. **Score.** Only where the careers page was actually read.

### Matching: gate, then fuzz

Fuzzy matching alone is unsafe here, and the numbers say so (rapidfuzz 3.14.5):

| Pair | `token_set` | `token_sort` | Same job? |
|---|---|---|---|
| `senior back end engineer` / `back end engineer` | **100.0** | 82.9 | no |
| `software development engineer 1` / `… 2` | 96.8 | **96.8** | no |
| `SDE-2 Backend` / `Software Development Engineer 2, Back End` | — | 33.3 raw → **100.0 normalized** | yes |

Three consequences, all enforced in code:

- **`token_set_ratio` is never used.** It scores a subset as a perfect match.
  Live data from this project has a case at **87.1** — two genuinely different
  Razorpay roles that any threshold of 85 would have accepted.
- **No scorer separates SDE-1 from SDE-2** — 96.8 on every metric, because one
  character carries the meaning. Levels are compared *before* any fuzzing, and a
  conflict skips the fuzz entirely.
- **Normalization does the heavy lifting.** Without the title map, the matcher
  would report real listings as missing and manufacture ghosts wholesale.

### The guard, and why it exists

The board is searched per company through a full-text URL, not a company filter.
Searching for `cred` returned **29 listings, none of them CRED** — it matched
*credit*, *credentials*, *Accredian*, *AMOLAKSHAYA TRADE AND CREDIT*. Ingested
as CRED listings, they would have found no counterpart on CRED's careers page
and been published as high-confidence ghost jobs at a named real company, each
with two authoritative-looking URLs beside it.

So a row is stored only when its own employer matches the company it is filed
under, and every rejection is counted and named. A target that sheds most of its
rows is reporting an ambiguous keyword, and that belongs on screen.

### What it refuses to claim

A listing is scored **only** when that employer's careers page was read
successfully. Otherwise it carries no score at all — not zero, which would read
as *we checked and it is fine*. The database enforces this rather than trusting
anyone to remember:

```sql
CHECK ((career_page_checked = 0 AND ghost_score IS NULL)
    OR (career_page_checked = 1 AND ghost_score IS NOT NULL))
```

There is a second trap underneath. This project's careers-page extractor returns
**zero rows** on SmartRecruiters, Workday and Lever without erroring. Reading
that as "this company lists no roles" would mark every listing for those
companies as a ghost with the highest-weighted signal available. So an empty
result counts as evidence only where the extractor is known to work.

## Scraper Studio

Collectors are built by handing Scraper Studio a plain-English description of
the wanted fields. The descriptions are in
[`scrapers/collectors.yaml`](scrapers/collectors.yaml) and are a deliverable in
their own right — one of them is run unchanged against several unrelated
applicant-tracking systems.

```
Extract every open job listing on this page. For each listing return:
- job_title: the role title as written
- location: city, or "Remote" if remote
- department: team or function if shown, otherwise null
- job_url: absolute URL to the full job description
If listings are paginated or behind a "show more" control, load them all.
```

Three collector IDs are in use. Every stored row carries the `c_*` that produced
it, and the ID is wired into all three of the places it can be:

| Wiring | Where |
|---|---|
| Database | `collector_id` on every row of `job_listings` and `scrape_runs` |
| API trigger | `POST /api/scrape/trigger` |
| Schedule | [`sweep.sh`](sweep.sh) on cron — see [`docs/scheduling.md`](docs/scheduling.md) |

### Self-healing

`c_mt1senswibym6o5va` was repaired **twice without ever being recreated**. The
ID that timed out three times is the ID on every row it has since produced, and
the ID the API trigger calls — a repair history attached to a stable identifier
is the difference between a scraper and an endpoint.

| | |
|---|---|
| Before | 3 runs, 0 rows, timed out at 110s, 660s and 1860s |
| Heal #1 | fixed the pagination — 50 rows in 41s, but each row concatenated four listings |
| Heal #2 | fixed the row boundary — 50 rows in 8.4s, 50/50 unique URLs |

A third heal, on the careers-page collector, was **rejected at the gate**: its
preview showed no improvement on the platform it targeted, and the platform
turned out to unlock only companies with no genuine listings. Rejecting is the
half of an approval gate that proves the gate is real. Full transcripts —
proposals, gates, approvals and the rejection — are in
[`data/heal/`](data/heal/) and written up in
[`docs/self-healing.md`](docs/self-healing.md).

## Findings

See [`findings.md`](findings.md) for the current results with clickable
evidence.

## Limitations

Stated plainly, because the honest version is more useful than the flattering
one.

- **Verified coverage is narrow: 4 of 70 listings.** Verifying a listing means
  reading that employer's careers page. A survey of 26 Indian technology
  companies found three with a public Greenhouse board this collector can read.
  The rest use an unreadable ATS, a bespoke client-side portal, or — in
  NoBroker's case — LinkedIn, which is login-walled and therefore out of scope.
  The employers who advertise on this board and the employers with
  machine-readable careers pages are largely different populations. That is a
  property of the market, not a bug, and the dashboard names the reason per
  company.
- **This is a selected sample, not a survey.** Companies were chosen because
  both sides are machine-readable. Nothing here supports a claim about Indian
  employers in general.
- **`department` is empty on every platform tested.** The description asks for
  it; Scraper Studio does not return it. Nothing scores on it.
- **28% of board listings carry no date.** Those abstain from staleness scoring
  rather than defaulting. Dates derived from "3 weeks ago" are accurate to about
  a week and are marked with a `~`.
- **The board is capped at one page**, by design — the unbounded version timed
  out every time.
- **Scoring weights are a judgement, not ground truth.**

## Only public data

Every target is a public page reachable without an account. Nothing behind a
login, a paywall or a region block is scraped. Where a company's job portal
turned out to be login-walled, it was dropped rather than worked around — and
recorded as dropped.

## Layout

```
ghosthire/          normalize · match · score · ingest · pipeline · api
scrapers/           collectors.yaml — the plain-English field descriptions
web/                the dashboard (no build step)
data/snapshots/     every collector response, unedited
data/heal/          self-healing transcripts, including the approval gates
docs/               sources, scheduling, self-healing
sweep.sh            the scheduled sweep
```
