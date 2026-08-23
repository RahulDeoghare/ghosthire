# Findings

As of 2026-08-23. **4 of 70** collected listings were checked against the employer's own careers page, comparing against **80** careers-page roles. Everything else is listed as unverifiable, with the reason, in the dashboard's
coverage table and in [`docs/sources.md`](docs/sources.md).

Each row below can be checked by opening the two links. That is the point — a finding you cannot check is an accusation.

## Checked listings

| Score | Company | Role | Signals | Board listing |
|---|---|---|---|---|
| **45** | Groww | Accounts Payable Specialist | `not_on_career_page` | [open](https://internshala.com/job/detail/accounts-payable-specialist-job-in-bangalore-at-groww1786175005) |
| **45** | Razorpay | Associate Technical Program Manager | `not_on_career_page` | [open](https://internshala.com/job/detail/associate-technical-program-manager-job-in-bangalore-at-razorpay1787185857) |
| **45** | Razorpay | Associate Manager, Key Accounts Management | `not_on_career_page` | [open](https://internshala.com/job/detail/fresher-remote-associate-manager-key-accounts-management-job-at-razorpay1786667557) |
| **0** | Razorpay | Associate, Startup Accounts | `none` | [open](https://internshala.com/job/detail/associate-startup-accounts-job-in-bangalore-at-razorpay1787099871) |

## What each result means

### Accounts Payable Specialist — Groww  ·  score 45

not found on the careers page. Collected by `c_mt1senswibym6o5va`.

- Board listing: <https://internshala.com/job/detail/accounts-payable-specialist-job-in-bangalore-at-groww1786175005>
- No match among the 5 open roles on the careers page we read

### Associate Technical Program Manager — Razorpay  ·  score 45

not found on the careers page. Collected by `c_mt1senswibym6o5va`.

- Board listing: <https://internshala.com/job/detail/associate-technical-program-manager-job-in-bangalore-at-razorpay1787185857>
- No match among the 25 open roles on the careers page we read

### Associate Manager, Key Accounts Management — Razorpay  ·  score 45

not found on the careers page. Collected by `c_mt1senswibym6o5va`.

- Board listing: <https://internshala.com/job/detail/fresher-remote-associate-manager-key-accounts-management-job-at-razorpay1786667557>
- No match among the 25 open roles on the careers page we read

### Associate, Startup Accounts — Razorpay  ·  score 0

appears on the careers page — **not** a ghost. Collected by `c_mt1senswibym6o5va`.

- Board listing: <https://internshala.com/job/detail/associate-startup-accounts-job-in-bangalore-at-razorpay1787099871>
- Matched careers-page role (100% confidence): [Associate, Startup Accounts](https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited/jobs/4724183005)

## How to read these

A gap between a board listing and a careers page is a **discrepancy, not a
verdict**. Innocent explanations exist: the careers page lags, the role was
filled yesterday, the listing is a recruiter's rather than the employer's. The
score says how many signals fired and which — nothing more.

The one row scoring 0 matters as much as the ones scoring 45. It is a listing
that *does* appear on the employer's careers page, and reporting it is what
makes the other numbers mean anything. A tool that only ever finds ghosts is not
measuring.

## Sample size

Four checked listings is small, and the reason is documented rather than
glossed: verifying a listing requires reading that employer's careers page, and
a survey of 26 Indian technology companies found three with a public Greenhouse
board this collector can read. The employers who advertise on this board and the
employers with machine-readable careers pages are largely different populations.

These companies were selected because both sides are machine-readable. Nothing
here supports a claim about Indian employers in general.
