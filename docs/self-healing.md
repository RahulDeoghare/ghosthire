# Self-healing

Three heals were run against live collectors. Two were approved and one was
rejected. All four transcripts — proposals, approval gates, approvals and the
rejection — are in [`data/heal/`](../data/heal/), unedited.

The claim worth making is not that healing worked. It is that
**`c_mt1senswibym6o5va` kept its identity through both repairs.** The ID that
timed out three times is the ID on every row it has since produced, the ID
stored on every `scrape_runs` record, and the ID `POST /api/scrape/trigger`
calls. Nothing downstream had to be repointed, because nothing was recreated.

---

## The collector that could not return a row

`c_mt1senswibym6o5va` was built on 2026-08-20 from a description ending:

> If listings are paginated or behind a "load more" control, load them all.

On Internshala that is an unbounded crawl. Three runs, three timeouts, zero
rows:

| Mode | Cap | Result |
|---|---|---|
| `--sync` | 110s | "Realtime page limit exceeded — switching to batch mode", then timeout |
| async | 660s | timeout after 61 batch polls |
| async | 1860s | timeout after 174 batch polls |

The cause was the description, not the URL: the same collector pointed at a much
narrower `/jobs/computer-science-jobs/` exceeded the realtime page limit just as
immediately, which is what rules out URL breadth.

Rebuilding was not an option — the account's trial collector slots were
exhausted. `bdata scraper heal` mutates a collector **in place** and needs no
slot, which made it the only route.

## Heal #1 — pagination

**Prompt.** *"The scraper walks every page of this job board. Every run exceeds
the realtime page limit, falls back to batch mode, and times out after 30
minutes without returning a single row. Fix it to read only the first page of
results…"*

Eleven steps — `planner`, `control_preview_runner`, `code_fixer`,
`css_selector_extractor`, `request_fulfillment_validator` — then it stopped at
`awaiting_approval` and waited. It was reviewed and approved with `--auto-save`.

**It fixed the timeout.** The command that had timed out three times returned in
41s, in realtime mode, without touching batch.

**It did not produce usable rows.** Each of the 50 rows concatenated four
separate job cards:

```json
{"company_name": "Mantra Softech India Private Limited AlmaShines Technologies
                  Private Limited MarketMyHotel.in Arcedior International…",
 "job_title":    "Product Support Trainee Customer Success Associate
                  Reservation Associate Frontdesk Executive",
 "location":     "Ahmedabad, Ahmedabad, Ahmedabad, Ahmedabad",
 "job_url":      "…at-mantra-softech-india-private-limited1784874497"}
```

Four employers and four titles in one row, one URL for all of them. The matcher
resolves a listing by company and title, so a row in this shape cannot be
matched to anything.

**The lesson, and it is the same one the landing page taught:** a passing
command is not working software. That run exited 0, printed 50 rows, named its
collector and wrote a clean snapshot — every acceptance box ticked by output the
pipeline could not use.

## Heal #2 — the row boundary

**Prompt.** *"Each returned row concatenates four separate job listings into
one… Fix the row boundary: return exactly ONE row per individual job card shown
on the page… Separately, date_posted returns the literal text 'Job' or 'Fresher
Job' for some cards."*

Reviewed at the gate and approved. Measured over the 50 rows that came back:

| Field | Coverage |
|---|---|
| `company_name`, `job_title`, `location`, `salary_range`, `job_url` | 100% |
| `date_posted` | 72% |
| **unique `job_url` values** | **50 / 50** |

The 50-of-50 unique URLs settle the row-boundary question: one row now means one
listing. Every present date parses as a relative date (`N weeks ago`), which is
the shape the date parser expects — the "Job" and "Fresher Job" junk is gone.

The remaining 28% have no date at all, and that is a real observation rather
than a defect: some cards carry a "Fresher Job" badge where the posting age
would be. Those ingest with a null date and abstain from staleness scoring.

**Run time across the three states of this collector: never → 41s → 8.4s.**

## Heal #3 — rejected

The careers-page collector `c_mt1s6jit3zcyg7dlw` reads Greenhouse and returns
zero rows on SmartRecruiters, Workday and Lever. A heal was run to teach it
Lever, aiming to unlock CRED's careers page.

It reached `awaiting_approval` with a preview containing **one row, from
Greenhouse, and none from Lever** — no evidence it had learned anything, and the
same weak signal heal #1 gave immediately before its row-boundary bug. By then a
second fact had also landed: the Lever companies had no genuine board listings
between them, so the unlock was worth nothing.

**It was rejected.** `bdata scraper approve --reject`. Greenhouse extraction was
re-tested immediately afterwards and still returned 25 roles for Razorpay.

Rejecting is the half of an approval gate that proves the gate is real. A gate
that only ever approves is a formality.

## Why the gate was never bypassed

Every heal here was run **without** `--auto-approve`, so each one stopped and
waited for a human decision — twice for yes, once for no. The alternative was
one flag shorter and would have applied heal #1's concatenation bug straight to
the live collector, where it would have poisoned every row ingested afterwards.

The gate cost about a minute per repair. It caught one bad fix out of three.
