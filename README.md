# GhostHire

Detects **ghost jobs** — roles advertised on job boards that the hiring company does
not list on its own careers page.

## The problem

Job seekers spend hours applying to roles nobody is hiring for. Companies leave
listings up to collect résumés for future openings, to satisfy internal "always be
hiring" policies, to look like they're growing — or simply because someone forgot to
close a filled role.

Nobody tracks this, because catching it requires two sources cross-referenced: the job
board, and the company's own careers page. GhostHire scrapes both and compares them.

## How it works

1. Scrape live listings from Indian job boards.
2. Separately scrape the careers page of each company appearing in those listings.
3. Normalize company names and job titles, then fuzzy-match board listings against
   careers-page listings.
4. Score each board listing on how likely it is to be a ghost, and show the evidence
   side by side.

## What a score means

> A high ghost score means a listing is advertised on a job board and absent from the
> company's own careers page. It is **evidence worth checking, not proof that a job is
> fake.** The scoring weights are our judgement, not ground truth.

Every result links to both source URLs so you can verify it yourself.

## Status

Early development. Setup instructions, findings and screenshots land as the pieces do.

## License

MIT
