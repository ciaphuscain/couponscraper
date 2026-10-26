# Part 3 — Requirements

Status key: **Done** = implemented and covered by tests · **Partial** · **Not built**.

## Functional requirements

### P0 — Core
| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F1 | Add an amazon.in product URL; extract and validate the ASIN | Done | `amazon.extract_asin`; `tests/test_parsing.py::AmazonUrlTests` (look-alike hosts, credentials, ports, short links rejected) |
| F2 | Watchlist with optional target price | Done | `repo.watch`, dashboard, CLI `add`, extension "Track" |
| F3 | Discover offers from approved sources | Done | File import, RSS (terms-gated), extension capture, manual entry |
| F4 | Normalise, validate, deduplicate | Done | `normalize.py`, fingerprint dedup, `tests/test_ingest.py` |
| F5 | Track expiry and evidence freshness | Done | Derived lifecycle (ACTIVE/STALE/EXPIRED/WITHDRAWN/NOT_STARTED) |
| F6 | Determine eligibility | Done | Three-valued rules + profile + wallet matching |
| F7 | Estimate net cost; compare scenarios; rank | Done | `optimizer.optimize`, worked-example tests |
| F8 | Notify | Done | Telegram / file / console channels, alert policy |
| F9 | Direct redemption path | Done | Steps, copy code, allowlisted official link, canonical product URL |
| F10 | Record redemption outcome | Done | API + dashboard + extension buttons, idempotent |
| F11 | Savings history | Done | Purchases + cashback ledger; realised vs pending separated |

### P1 — High leverage
| ID | Requirement | Status |
|---|---|---|
| F12 | Price history + deal quality | Done (only from observations that reach the system) |
| F13 | Browser extension: recognise product pages, show offers, save, feedback | Done (Chrome MV3, unpacked) |
| F14 | Coupon application assistance | Partial: copy code, "tick the coupon" instruction; no automated clicking (by design) |
| F15 | Cashback-provider comparison | Partial: multiple portal offers compete in the optimizer; no portal data feed |
| F16 | Payment-method preferences | Done (wallet; hypothetical cards optional) |
| F17 | Alert thresholds, cooldown, quiet hours, grouping | Done |
| F18 | Offer confidence | Done (authenticity prior + Beta-style update from user outcomes) |
| F19 | Stacking rules | Done (declared per offer, owner defaults, explicit exclusions) |

### P2/P3
| Requirement | Status |
|---|---|
| Cashback settlement tracking | Done (state machine with audit trail, measured provider rates feed the optimizer, overdue reminders every 14 days) |
| Promotional email import | Done (`.eml`/`.mbox`/folder; no credentials) |
| Opportunity-precision report | Done (History page) |
| Buy/wait advice, card insights, cross-product offer inference | Done |
| Mobile companion (Telegram bot), opt-in passive price observation | Done |
| Source quality scoring | Partial: health, failure counts and reject counts per run; no composite score |
| Cross-source duplicate detection | Done |
| Personalised recommendations, product equivalence, expiry prediction, LLM parsing, learning-to-rank | Not built (no measured need yet) |

## Non-functional requirements

| ID | Requirement | Target | Measured / verified |
|---|---|---|---|
| N1 | Local offer lookup | p95 < 300 ms | see docs/08-benchmarks.md |
| N2 | Cached dashboard response | p95 < 500 ms | see docs/08-benchmarks.md |
| N3 | No duplicate notifications for identical states | 0 | unique dedup key + soak test (0 duplicate keys) |
| N4 | Bounded memory over long operation | no unbounded growth | soak test samples |
| N5 | Recover after crash/restart | consistent DB, jobs resume | SIGKILL test; lease expiry; single catch-up |
| N6 | ₹0 recurring cost | ₹0 | stdlib only; local SQLite; Telegram free |
| N7 | No shopping credentials collected | none | wallet validator rejects card-number-like strings; extension reads no cookies/forms |
| N8 | Loopback only | refuses other bind addresses | `config.load` |
| N9 | Runs on Windows | Python ≥ 3.11 | CI matrix includes windows-latest |

## Assumption register

| # | Assumption | Impact if wrong | How to check |
|---|---|---|---|
| A1 | Reading the page the user is viewing, on their click, for personal use is acceptable | Extension capture would need disabling; manual entry remains | Read amazon.in Conditions of Use; decide (docs/06 §Residual risks) |
| A2 | User has no Creators API eligibility | If eligible, an API adapter could add scheduled price checks | Associates console |
| A3 | Amazon applies instant discounts in order coupon → code → bank | Basis UNKNOWN handling already shows both readings | Observe at checkout; set `basis` |
| A4 | Settlement priors (bank 85%, Amazon Pay 90%, portal 65%) | Risk-adjusted ranking shifts | Replaced automatically by the measured provider rate after ≥3 settled records |
| A5 | Authenticity priors (official 95%, listed 75%, unverified 50%) | Confidence numbers are heuristics | Track Applied/Failed outcomes; recalibrate |
| A6 | Offers re-confirmed within 48 h (24 h for extension captures) are fresh | Too long: stale offers shown; too short: good offers hidden | Per-source `stale_after_h` |
| A7 | IST (+05:30) for date-only expiries and quiet hours | Off-by-hours expiry | Fixed offset; India has no DST |
| A8 | Cashback without a stated date is due within 90 days of purchase | Reminders too early or late | Enter the provider's stated date when recording |
| A9 | A bank offer shown on several product pages may apply to other products | "Possibly" answers that don't hold | Shown as unknown, never confirmed or alerted |

## Success metrics (how the owner will judge it)

| Objective | Metric | Source in the system |
|---|---|---|
| Realised savings | Σ(reference − cash paid) + cashback **paid** | History page |
| Redemption success rate | applied / (applied + failed + not_shown) | `redemption_attempts` |
| Redemption effort | Clicks from product page to redemption path | E2E: 1 click (Track/Save) to see the answer; steps listed |
| Opportunity precision | Alerts followed within 7 days by a purchase or applied offer / deal alerts sent | History page (`repo.alert_precision`) |
| Operational efficiency | Source runs, failures, rejects; CPU/time per evaluation | Health page, benchmarks |
