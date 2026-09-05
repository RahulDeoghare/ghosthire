# UI audit — the `ui` branch front end

A pass over `web/index.html` after the visual rebuild, checking behaviour
rather than looks. Every item below was reproduced in a browser against a
running `uvicorn ghosthire.api:app`, not read off the source.

Status column: **fixed** in the commit that adds this file, unless noted.

## The big one: clicks inside an open row are swallowed

`jobCard()` renders each listing as `<article role="button" tabindex="0">` with
a click handler that calls `openDetail()`. The evidence panel is then appended
*inside that same article*. So every click on anything in the panel bubbles up
to the row handler, which rebuilds the panel from scratch.

| # | Symptom | Cause | Status |
|---|---|---|---|
| 1 | "See all N roles we compared against" opens and instantly closes | `<summary>` click bubbles → panel rebuilt → `<details>` replaced by a fresh closed one | fixed |
| 2 | The ✕ close button does nothing | `closeDetail()` runs, then the bubbled click calls `openDetail()` again | fixed |
| 3 | The ‹ › step buttons do nothing | `step()` moves, then the bubbled click re-opens the row you started on | fixed |
| 4 | Space on ‹ › ✕ re-opens the row instead of pressing the button | row `keydown` handler calls `preventDefault()` on Space from any descendant | fixed |
| 5 | Evidence links re-trigger a fetch and rebuild | same bubbling; the link still worked, so this was waste rather than breakage | fixed |

One fix for all five: the row's click and keydown handlers ignore events that
originated inside the panel or on any interactive descendant.

Related, and fixed with it: `role="button"` on an element that contains
buttons, links and a `<summary>` is invalid — a button may not contain
interactive content. The row is now a plain article with an explicit
"show the evidence" control semantic that does not lie about its contents.

## Layout and navigation

| # | Symptom | Cause | Status |
|---|---|---|---|
| 6 | Nothing sticks — masthead and the search/filter bar scroll away | `body{overflow-x:hidden}` makes the body a scroll container, so `position:sticky` children have no scrollport to stick to | fixed |
| 7 | Section links land with the heading hidden behind the masthead | no `scroll-margin-top` on the anchor targets | fixed |
| 8 | Between 900px and 1024px every column is labelled twice | `.ledger-head` appears at ≥900px, the per-row `.side-label`s hide at ≥1024px (`lg:hidden`) — a 124px window where both show | fixed |
| 9 | The ledger head would slide under the two sticky bars | it is `sticky top:0 z-index:5`, beneath the masthead (z-30) and filter bar (z-20) | fixed |
| 10 | "No job matches that search" sits under two column headings | the ledger head renders regardless of whether there are rows | fixed |

## State

| # | Symptom | Cause | Status |
|---|---|---|---|
| 11 | `?listing=` silently does nothing for 66 of 70 jobs | the default filter is Checked; `openDetail()` returns early when the row is not in the DOM, after having already written the URL | fixed |
| 12 | Switching theme closes the open evidence and drops `?listing=` | `setTheme()` calls `render()`, which replaces `#jobs`; `CURRENT` is left set, so a later arrow key re-opens a panel out of nowhere | fixed |
| 13 | "Show N more" closes the open evidence | same — `render()` replaces `#jobs` | fixed |

## Theme and finish

| # | Symptom | Cause | Status |
|---|---|---|---|
| 17 | The ‹ › ✕ buttons and the roles disclosure turn invisible on hover in dark mode | Tailwind's `hover:text-black` is literally black, on a `#080b11` surface | fixed |
| 18 | The highlight on an open row is a grey rectangle that stops short of the window on both sides | it was painted on the row's own background, so it ended at the shell padding | fixed |
| 19 | The 25 compared roles are a tall stack of accent-coloured links, louder than the verdict above them | every one used `.link`, the accent colour | fixed |

18 is now a band: `.entry::before` bleeds out by exactly the shell gutter, both
driven by one `--gutter` custom property, so the highlight reaches the window
edge and reads as "this row is open" rather than as a stray box. It is
`pointer-events:none`, so it never eats a click.

19 is now a bordered panel, muted by default and accent only under the cursor,
set in up to three columns — corroborating material, not the finding.

## Second pass — affordances

| # | Symptom | Cause | Status |
|---|---|---|---|
| 20 | Clicking a row that is already expanded does not close it | the handler always called `openDetail()`, so a second click tore the panel down and rebuilt the identical one | fixed |
| 21 | The pointer cursor disappears once a row is open | the cursor came from `role="button"`, which is removed while the panel is inside the row (see the ARIA note above) | fixed |
| 22 | "Where the data comes from" looks like an inert card | `details > summary{list-style:none}` strips the disclosure triangle for every `<details>` on the page, and nothing replaced it here | fixed |
| 23 | Rows, chips, links and summaries fall back to the browser's default focus ring | only `.btn` and the form fields carried the page's own `--ring`; the default on this palette is a bright orange belonging to no token here | fixed |

20 makes the row a real toggle. Clicking a *different* row still switches
rather than closing, and clicks inside the panel are still the panel's.

21 moves `cursor:pointer` onto `.entry` itself, where it does not depend on an
attribute that comes and goes. The panel inside takes the cursor back, because
the panel is not a control.

## Dead code

| # | Item | Status |
|---|---|---|
| 14 | `avatar()` and its two tint tables — no call sites since the card layout became the ledger | removed |
| 15 | `.prose` — added during the rebuild, never applied | removed |
| 16 | `.column` — now `max-width:none`, i.e. it does nothing | removed |
| 24 | `.job`, `.job:hover`, `.job:focus-visible` — the class the card layout used; nothing carries it now | removed |
| 25 | `.v-warn` — `verdict()` only ever returns ok, bad or none | removed |
| 26 | `.sect-num` — every use became `.label` in the rebuild | removed |
| 27 | `kbd{...}` — there is no `<kbd>` on the page | removed |
| 28 | `--shadow` in both themes — its only consumer was `.job:hover` | removed |
| 29 | `META` — `/api/meta` was fetched on every page load and the result never read | removed, request and all |

## Checked and found correct — do not "fix" these

- **Focus rings are present.** An early measurement said every control had
  `outline: none`; that was an artifact of calling `.focus()` from script,
  which does not match `:focus-visible`. Real Tab presses show rings.
- **A click on a row does not scroll the page.** `focus()` on the panel's first
  button only scrolls when the panel is off screen, and it lands clear of the
  sticky bars.
- No horizontal overflow at 375px, 768px, 960px or 1440px. The two data tables
  scroll inside their own `overflow-x-auto`, which is intended.
- Escape closes the panel from inside a text input; arrow keys correctly do
  not steal from `<input>`, `<select>` and `<textarea>`.
- `safeUrl()` still refuses anything that is not http(s), and scraped text is
  still escaped before it reaches the DOM.
- Both themes are applied before first paint and survive a dead CDN.

