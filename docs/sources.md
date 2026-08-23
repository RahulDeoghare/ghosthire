# Sources

Every target this project scrapes, and what we actually know about it.

Provenance labels are used consistently across the repo:

- **VERIFIED** — observed directly, with a URL and a timestamp
- **DERIVED** — computed from verified data
- **UNVERIFIED** — believed but not yet confirmed

## Job boards

| Board | URL | Public without login | Pre-built Bright Data scraper | Status |
|---|---|---|---|---|
| Internshala — jobs | https://internshala.com/jobs/ | UNVERIFIED | **No** (VERIFIED) | primary — collector times out, see below |
| Internshala — internships | https://internshala.com/internships/ | UNVERIFIED | **No** (VERIFIED) | primary |
| Secondary board | — | — | — | undecided |

### Pre-built coverage — VERIFIED 2026-08-20

`bdata pipelines list` returns **43** pre-built scrapers. Filtering it for every job
board we considered:

```
$ bdata pipelines list | grep -iE "indeed|naukri|internshala|foundit|glassdoor|linkedin|timesjobs"
linkedin_company_profile
linkedin_job_listings
linkedin_people_search
linkedin_person_profile
linkedin_posts
```

LinkedIn is the only job source Bright Data already ships, and we do not target it.
**Internshala, Naukri, Foundit, TimesJobs and Glassdoor are all absent** — every board
in our plan is uncovered territory, which is exactly where a purpose-built collector
earns its place.

Indeed does not appear in `pipelines list` either, but it stays excluded on separate
grounds: Bright Data sells a dedicated Indeed scraper and an Indeed job-posting dataset
as products, so building one here would still be re-implementing something they ship —
and it carries the highest anti-bot risk of any candidate.

Account state at the same timestamp: zones `cli_unlocker` (unblocker) and `cli_browser`
(browser_api) both active.

**Indeed is deliberately excluded.** Bright Data already ships a dedicated Indeed
scraper and an Indeed job-posting dataset, so building one here would re-implement an
existing product rather than demonstrate anything. It is also the highest anti-bot risk
of any candidate.

The secondary board is chosen on the day, from Naukri → Foundit → TimesJobs, taking the
first that is (a) absent from `bdata pipelines list` and (b) renders listings without a
login wall:

```bash
bdata pipelines list | grep -iE "indeed|naukri|internshala|foundit|glassdoor|linkedin"
```

If all three are gated, we ship Internshala alone. The career pages are the interesting
half regardless.

### The board collector times out — VERIFIED 2026-08-21

`c_mt1senswibym6o5va` (built from the `board_internshala` description) has never
returned a row. Three runs, three timeouts:

| Mode | Cap | Result |
|---|---|---|
| `--sync` | 110s | "Realtime page limit exceeded — switching to batch mode", then timeout |
| async | 660s | timeout after 61 batch polls, 0 rows |
| async | 1860s | timeout after 174 batch polls, 0 rows |

**The cause is the description, not the URL.** The last line of `board_internshala`
reads *"If listings are paginated or behind a 'load more' control, load them all."* On
Internshala that is an unbounded crawl. The same collector pointed at a much narrower
`https://internshala.com/jobs/computer-science-jobs/` exceeded the realtime page limit
just as immediately, which is what rules out URL breadth as the explanation.

§7 of the build plan budgets **~10 pages per board sweep**, not the whole site — so a
bounded first-page read is what the plan assumed from the start. The bounded rewrite is
kept in `descriptions.board_internshala_bounded`.

**Blocked on account limits.** Creating a second collector from the bounded description
returns:

```
Failed to create scraper template: Error: You've run out of custom collectors
on your trial          Status: 400
```

Three collector slots exist and all three are spent, one of them on
`c_mt1ruaer190xqv1oee`, the dead landing-page collector above. `bdata scraper` has no
delete subcommand, so a slot cannot be reclaimed from the CLI.

`bdata scraper heal` mutates an existing collector **in place** and therefore needs no
slot, which makes it the only route to a bounded board collector on the free tier.

### Heal #1 — pagination — VERIFIED 2026-08-22

Prompt: read only the first page, do not follow pagination or "load more". The heal ran
eleven steps (`planner` → `code_fixer` → `css_selector_extractor` →
`request_fulfillment_validator` → …), stopped at `awaiting_approval` for review, and was
approved with `--auto-save`. Both transcripts are in `data/heal/`.

**It fixed the timeout.** The same command that had timed out three times returned in
41s, in realtime mode, without touching batch:

```
collector c_mt1senswibym6o5va · internshala · 50 rows · 41.0s
→ data/snapshots/20260822T025428Z_internshala.json
```

**It did not produce usable rows.** Each of the 50 rows concatenates four separate job
cards:

```json
{"company_name": "Mantra Softech India Private Limited AlmaShines Technologies
                  Private Limited MarketMyHotel.in Arcedior International...",
 "job_title":    "Product Support Trainee Customer Success Associate
                  Reservation Associate Frontdesk Executive",
 "location":     "Ahmedabad, Ahmedabad, Ahmedabad, Ahmedabad",
 "job_url":      ".../at-mantra-softech-india-private-limited1784874497"}
```

Four companies and four titles in one row, one URL for all of them. The matching engine
resolves a board listing to a careers-page role by company and title, so a row in this
shape cannot be matched to anything — the row count is real but the rows are not data.
`date_posted` came back partly repaired: 32 of 50 carry a real relative date
("Posted 3 weeks ago"), 14 still carry the literal `"Job"` or `"Fresher Job"`.

**The lesson, and it is the same one as the landing page:** a passing command is not
working software. That run exits 0, prints 50 rows, names its collector and writes a
clean snapshot — and every acceptance box P1 defines was ticked by output P2 could not
have used.

### Heal #2 — row boundary and dates — VERIFIED 2026-08-22

Prompt: return exactly one row per job card, and make `date_posted` the posting age
rather than the literal "Job". Reviewed at the gate and approved with `--auto-save`.

```
collector c_mt1senswibym6o5va · internshala · 50 rows · 8.4s
→ data/snapshots/20260822T025851Z_internshala.json
```

Measured over those 50 rows:

| Field | Coverage |
|---|---|
| `company_name`, `job_title`, `location`, `salary_range`, `job_url` | 100% |
| `date_posted` | 72% |
| **unique `job_url` values** | **50/50** |

The 50-out-of-50 unique URLs are what settles the row-boundary bug: one row now means
one listing. Every one of the 36 present dates parses as a relative date
(`N weeks ago`), which is the exact shape the §1.8 date parser expects — the "Job" and
"Fresher Job" junk is gone.

**The remaining 28% have no date, and that is a real observation, not a defect.** Some
Internshala cards carry a "Fresher Job" badge where the posting age would otherwise be.
Those rows must ingest with a null date rather than an invented one, and any temporal
signal has to abstain on them — the same rule `career_page_checked = 0` follows for
career pages.

Run time across the three states of this collector: **never → 41s → 8.4s.**

## Company career pages

Eight companies, chosen to span as many distinct applicant-tracking systems as
possible — that spread is the entire point, because one plain-English field description
is run against all of them.

| # | Company | Page that actually lists roles | ATS | Roles seen | Verified |
|---|---|---|---|---|---|
| 1 | Razorpay | https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited | Greenhouse | 24 | **2026-08-20** |
| 2 | Postman | https://job-boards.greenhouse.io/postman | Greenhouse | 111 | **2026-08-20** |
| 3 | Freshworks | https://careers.smartrecruiters.com/Freshworks | SmartRecruiters | 54+ | **2026-08-20** |
| 4 | BrowserStack | https://browserstack.wd3.myworkdayjobs.com/External | Workday | — | JS-rendered |
| 5 | Zerodha | https://careers.zerodha.com/ | Custom | — | JS-rendered |
| 6 | CRED | https://careers.cred.club/ | Custom | — | JS-rendered |
| 7 | Swiggy | https://careers.swiggy.com/ | Custom | — | JS-rendered |
| 8 | Meesho | https://jobs.lever.co/meesho | Lever (UNVERIFIED) | — | 403 on plain fetch |

Five distinct ATS platforms — Greenhouse, SmartRecruiters, Workday, Lever and
hand-rolled — which is a wider spread than the plan assumed, and makes the
one-description-many-platforms question worth asking properly.

### The landing-page trap — VERIFIED 2026-08-20, the hard way

**None of the eight company career pages list any jobs.** Every one is a marketing
page whose only job-related content is a "See all jobs" button pointing at an external
ATS. The URLs originally in the plan were all landing pages.

This cost a collector generation to discover. `c_mt1ruaer190xqv1oee` was built against
`https://razorpay.com/jobs/`; with no listings on that page to learn from, the
generated scraper learned the only job-shaped thing present — *the outbound link* — and
returned:

```json
[{"job_url": "https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited",
  "input": {"url": "https://razorpay.com/jobs/"}}]
```

Run against Freshworks and Zerodha, the same collector failed with
`Crawler error: navigate validation error: url is required` — it went looking for an
outbound link that those pages do not have, and navigated to nothing.

**The scraper was not wrong. The URL was.** A generated collector can only learn from
what is on the page it is built against, so the build URL has to be a page that
actually lists roles. The two snapshots are kept in `data/snapshots/` as evidence, and
`tests/test_bdata.py::test_career_targets_point_at_pages_that_list_roles` fails if a
landing page ever creeps back into the config.

### The fork test result — VERIFIED 2026-08-20

§0.3 asks one question: does a single plain-English description generalize across
unrelated ATS platforms? One collector, `c_mt1s6jit3zcyg7dlw`, built from the
`career_generic` description, was run against three targets on three platforms. The
description text was byte-identical for all three.

| Target | ATS | Rows | title | url | location | department | Snapshot |
|---|---|---|---|---|---|---|---|
| Razorpay | Greenhouse | 24 | 100% | 100% | 100% | 0% | `20260820T172031Z_fork-razorpay.json` |
| Freshworks | SmartRecruiters | 0 | — | — | — | — | `20260820T172036Z_fork-freshworks.json` |
| BrowserStack | Workday | 0 | — | — | — | — | `20260820T172040Z_fork-browserstack.json` |

**VERDICT: PARTIAL — 1/3 targets clean.** The description generalizes *within*
Greenhouse and returns nothing on SmartRecruiters or Workday. All three runs reached
their page and returned a well-formed empty array rather than a crawler error, so this
is a real answer about the description, not a failed run.

Consequences, both of which the rest of the build assumes:

1. **One collector does not cover eight companies.** The per-platform fallback in §0.3
   is the path we are on: keep the generic collector for Greenhouse, and build one
   collector per remaining ATS rather than one per company. Five platforms, not eight
   companies, is the number that matters.
2. **`department` is never populated, on any platform.** The description asks for it and
   Scraper Studio does not return it. Nothing downstream may treat a missing department
   as a signal — it is missing everywhere, by construction.

The difference between the 24 rows recorded here and the 25 returned on 2026-08-21
is Razorpay's board changing between runs, which is the effect this project
exists to measure.

### Rendering

Plain HTTP fetches were used for the checks above. Three boards returned real titles
that way; four more (Workday, Zerodha, CRED, Swiggy) returned an empty shell because
they render client-side, and Meesho returned 403.

**That does not mean those five are unscrapeable** — it means a naive fetch cannot read
them. Scraper Studio renders in a real browser and the account has an unblocker zone,
which is precisely the gap those five sit in. Their status stays UNVERIFIED until a
collector run says otherwise, and no claim is made about them before that.

### The check each URL still needs

Open it in a browser and answer three questions. It takes fifteen seconds per page and
saves fifteen minutes of collector generation each time the answer is no.

1. Are job listings visible without clicking anything?
2. Is there a login, region gate, or cookie wall in front of them?
3. Which ATS is it really? (`lever.co`, `greenhouse.io` or `boards.greenhouse.io` in the
   job links; otherwise custom.)

Record the answer in the table above, replace anything that fails from the reserve
list, and update `scrapers/collectors.yaml` to match. The `ats` values there are
recorded guesses — written down *so they can be checked*, not because they are known.

## Pairing the board with the career pages — VERIFIED 2026-08-23

The front page of the board and the eight career targets are **disjoint populations**:

```
board employers (normalized)          46
career targets configured              8
overlap                                0
```

Internshala's fresher-jobs page returns Hey Buddy, Harold Electricals, Bajaj Finance,
Atlas Copco, Conbox Logistics. The career targets are consumer-tech companies. Not one
appears on both sides, and re-picking career targets from the board list does not fix it
either — the extraction path that works is Greenhouse, and mid-market Indian employers
mostly do not run Greenhouse.

Left alone this is fatal to the product rather than to the pipeline: ingest, normalize
and match would all run correctly, find nothing, and every listing would score
`career_page_checked = 0`. The dashboard would render 50 rows of "not assessed" and no
ghost score would be computable for any of them.

**The fix is to scrape the board per company rather than as a firehose.** Internshala
exposes a company filter at `/jobs/keywords-<name>`, so the two sides are made to
overlap by construction instead of by luck. It needs no new collector — the same
`c_mt1senswibym6o5va` reads the filtered URL — which matters because the account's trial
collector slots are exhausted.

First run, `board_company --company razorpay`, 3 rows in 12.9s
(`20260823T070349Z_board-company-razorpay.json`), against the 25 roles on Razorpay's
Greenhouse board (`20260821T183126Z_career-razorpay.json`):

| Board listing | Best careers-page candidate | `token_sort_ratio` | Verdict |
|---|---|---|---|
| Associate, Startup Accounts | Associate, Startup Accounts | **100.0** | on both — not a ghost |
| Associate Technical Program Manager | Technical Account Manager | 70.0 | **candidate ghost** |
| Associate Manager, Key Accounts Management | Associate Manager, Startup Accounts | 72.7 | **candidate ghost** |

Two things worth noting. The first row is a true negative and the product must say so —
a listing that *is* on the careers page is the common case and the honest baseline. And
the third row is exactly the near-miss the §3A.4 threshold exists for: 72.7 is close
enough to look like a match to a human skimming, and the ≥85 gate correctly refuses it.

**What this costs in scope.** The project no longer assesses "a job board"; it assesses
a named list of companies, on both sides. That is a smaller claim and a more defensible
one — every score traces to two URLs a reader can open. The front-page sweep is kept for
the coverage number, not for scoring.

## Why coverage is narrow — VERIFIED 2026-08-23

Verifying a listing requires reading that employer's own careers page, and that is the
binding constraint. A survey of 26 Indian technology companies found **three** with a
public Greenhouse board this collector can read: Razorpay, Groww and Postman.

The others fail for three distinct reasons, and the dashboard names which:

| Reason | Example | Detail |
|---|---|---|
| Unreadable ATS | Freshworks, BrowserStack, CRED | SmartRecruiters, Workday and Lever all return a well-formed **empty array** — the fourth platform tested after Greenhouse |
| Bespoke portal | Swiggy, Zerodha | roles are fetched client-side; the served page carries none |
| **Out of scope by rule** | NoBroker | its "See all opportunities" points at `linkedin.com/jobs/search` |

**NoBroker is the interesting case.** Its careers page carries no listings at all — the
job portal *is* LinkedIn. That fails the public-data-only rule, and Bright Data already
ships a pre-built LinkedIn scraper, so building one would re-implement a product rather
than demonstrate anything. NoBroker is unassessable **by rule, not by capability**, and
that distinction is stated on the dashboard rather than hidden behind a blank cell.

The structural finding underneath: **the employers who advertise on this board and the
employers with machine-readable careers pages are largely different populations.** Mid-
market Indian companies use bespoke portals or LinkedIn; the ones on Greenhouse barely
appear on the board. That is why 4 of 70 listings are assessed, and it is a property of
the market rather than of the pipeline.

## Only public data

Every target here is a public page reachable without an account. Nothing behind a
login, a paywall, or a region block is scraped. If a page turns out to require any of
those, it is dropped rather than worked around.
