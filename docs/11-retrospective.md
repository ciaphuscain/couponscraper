# Known limitations, roadmap, scorecard, retrospective and interview bank

## Known limitations (honest list)

1. **No automatic amazon.in data.** Prices and offers arrive only from pages you view (extension),
   manual entry or imports. A watched product you never revisit gets no new prices, so
   price-drop alerts can't fire for it. This is a deliberate consequence of ADR-004.
2. **Redemption is never auto-tested.** Whether an offer works is learned only from your
   Applied/Didn't-apply clicks.
3. **Offer text parsing is regex-based.** It is accurate on the author-written development set
   (19/19), which is not held-out and not sampled from live pages. Truncated carousel text can
   miss minimum spends and exclusions; these produce warnings and lower confidence.
4. **Confidence and settlement numbers are documented heuristics** (priors), not learned
   probabilities, until your own records accumulate.
5. **Extension selectors will break** when Amazon changes markup. The panel fails safe and says so.
6. **No email ingestion, no cashback-portal or bank integration, no Creators API adapter.**
7. **Single user, single machine, loopback only.**
8. **Cold dashboard views scale linearly with watched products** (about 12 ms per product on
   realistic data, more with many sitewide offers). Warm views are cached.
9. **Not verified on real Windows hardware by hand.** CI runs the full Python suite and CLI smoke
   test on `windows-latest`. The Task Scheduler and Telegram steps are documented but were not
   executed in this environment.
10. **Multi-buy offers ("buy 2, get 5% off") are not modelled.** The parser rejects them rather
    than misreading them.

## Roadmap

| Stage | Status | Next |
|---|---|---|
| V0 prototype (DB, source, matching, notification, tests) | Done | — |
| V1 reliable discovery (multiple adapters, dedup, expiry, provenance, scheduler, health, migrations) | Done | Composite source-quality score from run history |
| V2 savings intelligence (eligibility, stacking, payment matching, effective price, confidence) | Done | Owner-confirmed stacking learned from Applied feedback on combinations |
| V3 frictionless redemption (extension, overlay, copy code, links, outcomes) | Done | Measure real clicks/time (Experiment E) |
| V4 advanced monitoring (price history, drops, cashback tracking, prioritisation, analytics) | Mostly done | Opportunity-precision report (alerts → successful redemptions) |
| V5 hardening (failure injection, security tests, backup, soak, benchmarks, docs) | Done for the scope here | Run a 30-day soak on the always-on laptop |
| V6 research | Not started | Creators API adapter *if* eligible; email import; LLM parsing only if measured unknown-rate on real captures stays high |

Experiments still to run with real data: **A** (source quality: needs real sources over weeks),
**E** (user friction: time yourself with and without the extension; protocol in
docs/08-benchmarks.md), and **G** at full length (30 days on the laptop).

## Final scorecard

| Dimension | Evidence | Current state | Weakness | Next improvement |
|---|---|---|---|---|
| Discovery | 4 source types; ingest 14k records/s; dedup replay creates 0 rows | Works with what reaches it | No automatic Amazon data | Creators API adapter if eligible; email import |
| Source legitimacy | docs/02 register; terms-gated RSS; robots.txt; no crawling | Conservative | In-browser capture is a grey area you must accept | Keep capture user-initiated |
| Offer verification | 4 independent dimensions; derived lifecycle; provenance per source | Solid model | Priors are heuristics | Calibrate from your outcomes |
| Eligibility | 3-valued rules; 20+ eligibility tests | Correct on tested cases | Rules from free text are limited to parsed fields | Rule editor in dashboard |
| Savings optimisation | Worked example; brute-force property test; 0 double counting of cashback/gift cards | Exact, bounded | Pruning can (in theory) miss exotic combos past 3 per kind | Measure real offer-set sizes |
| Redemption usability | E2E: 1 click to answer + steps; panel first render 258–298 ms locally | Good | No in-page coupon clicking (by design) | Experiment E with real purchases |
| Cashback tracking | State machine + audit trail; measured provider rates replace priors | Works | Manual status updates | Reminder alerts for overdue cashback |
| Architecture | Modular monolith, 7 ADRs, I/O-free domain | Appropriate | Single process | — |
| Coding | Stdlib only; typed; small modules | Readable | Hand-rolled HTTP routing | Revisit at ~30 endpoints |
| Database | Constraints, unique keys, WAL, migrations, SIGKILL test, backups | Solid | Plain-text at rest | Rely on BitLocker |
| Performance | docs/08 measured numbers | Within targets on realistic data | Cold multi-product views | Evaluate only changed products |
| Reliability | Backoff, auto-disable, stale-not-valid, leases, catch-up, retries | Good | Single process down = no alerts | Task Scheduler restart policy (documented) |
| Security | 18-row threat model with tests | Strong for local use | Local malware can read the token | OS-level protections |
| Testing | 120+ Python tests, 7 Node tests, E2E, CI on Windows | Broad | Not tested against live amazon.in pages | Capture real pages as fixtures (privately) |
| Observability | Health page, source_runs, job_runs, outbox, JSON logs with redaction | Adequate | No metrics time series | Add counters to status API |
| Documentation | 11 docs + ADRs, generated worked example | Matches implementation | Will drift | Keep generated tables generated |
| Operational cost | ₹0; stdlib; local | Met | — | — |
| Technical ownership | Every bug found is listed with its test (docs/07) | — | — | — |

## Engineering retrospective

**What worked.** Treating "unknown" as a value removed a whole class of over-promising.
Generating the worked-example table from the optimizer kept the docs honest. Measuring
before optimising found that the real cost was the 2ⁿ basis readings and per-view
recomputation, not SQL.

**What went wrong (and was caught).** Five real bugs: double-escaped HTML (Python `str`
concatenation returning plain `str` from a `Raw` subclass), an unsound two-reading shortcut,
scenario trimming that changed the ranked answer, deal rules that called noise a deal, and
synthetic data merging with real data. Four of the five were caught by tests or measurements
added *because* something looked off: a screenshot, a changed count, a brute-force check. The
lesson: inspect outputs, not only test results.

**What I'd redesign after six months.** Store evaluations per product when their inputs change
(event-driven recompute) instead of on read. Promote stacking rules to first-class records with
provenance, learned from your Applied feedback on *combinations*. Replace confidence priors with
calibrated per-source rates.

## Interview question bank (answers grounded in this repo)

* **Why SQLite instead of PostgreSQL?** One writer process, < 10⁶ rows, file backups, no service
  to run on Windows. WAL gives concurrent reads. The SIGKILL test proves atomicity. Move when
  there are writers on multiple machines (ADR-002).
* **Why a modular monolith?** Workload is tiny; one process is easier to debug and recover;
  domain modules are I/O-free so splitting later is mechanical (ADR-001).
* **How are duplicate offers detected?** A SHA-256 fingerprint of economics, scope, code, payment
  restriction and eligibility, deliberately excluding wording and expiry. `UNIQUE` +
  `ON CONFLICT DO NOTHING`; provenance is one row per source; expiry conflicts resolve to the
  earliest date.
* **How is source reliability measured?** Per run: fetched/created/updated/duplicates/rejected
  plus consecutive failures. Authenticity comes from evidence type. A reject ratio > 50% marks a
  run partial and blocks withdrawals.
* **How are eligibility rules represented?** JSON predicate trees with Kleene logic. A missing
  fact gives unknown, never false.
* **How do you prevent double-counting?** Instant, funding and deferred savings are separate
  fields. Deal prices are informational. Gift-card balance is a cash-cost ratio on the
  instrument, never an extra saving. Tests cover each.
* **How do you handle uncertain cashback?** Never subtracted from checkout; probability-weighted
  in risk-adjusted cost; priors replaced by measured provider rates after 3 settled records; only
  *paid* counts as realised.
* **Offer expires during processing?** Lifecycle is derived at evaluation time with a single
  `now`; alerts re-evaluate. A scenario never mixes an expired offer, and the panel re-fetches.
* **Rate limits?** `min_interval_s`, exponential backoff in `next_fetch_at`, honour `Retry-After`,
  stop on 401/403, never escalate.
* **Source HTML changes?** RSS schema fingerprint → warning; high reject ratio → partial, no
  withdrawals. Extension: fixed selectors fail safe with a warning, and manual entry still works.
* **Scheduler reliability?** DB lease with expiry (no overlap across processes, crash-safe),
  `job_runs` audit, single catch-up after downtime, failures contained per job.
* **Two workers process the same offer?** `BEGIN IMMEDIATE` serialises writers; the unique
  fingerprint makes inserts idempotent; cashback transitions are compare-and-set.
* **Misleading deal notifications?** Fresh price required; ≥ 50% success probability;
  thresholds; deal = clears max(5%, 2× volatility) below the median; dedup key; cooldown;
  downgrade suppression. Experiment D: 270 → 0 false positives on synthetic series (not
  real-world accuracy).
* **Extension security risks?** Malicious messages, DOM injection, over-broad permissions,
  credential exposure, affiliate tampering. See docs/06 T9–T15.
* **Scale to multiple users?** Per-user wallet/profile/watchlist tables, real auth (not a
  loopback token), Postgres, a worker for evaluations. The domain code is unchanged.
* **First bottleneck?** Cold per-product optimisation on page views and in `alerts.decide` (linear
  in watched products). Measured; mitigated with a version-keyed cache; next step is
  recompute-on-change.
* **What was measured / which optimisations helped?** See docs/08: optimizer 60 offers 3.5 s →
  ~0.1 s (pruning + bounding readings); warm watchlist page for 100 products ~5 ms; soak peak
  memory 14.9 MB → 1.0 MB after fixing cache retention.
* **Features deliberately rejected?** Crawling, proxies, CAPTCHA solving, auto-checkout, LLM
  arithmetic, microservices, a CP/ILP solver, a React SPA.

## Resume bullets (only from actual results in this repo)

* Built a local, dependency-free Python savings engine for Amazon India: a three-valued
  eligibility engine and an exact offer-stacking optimizer, verified against brute force over
  400 random offer sets.
* Profiled and cut worst-case optimisation from 3.5 s to ~0.1 s, and found and fixed a ranking
  regression via a deterministic benchmark (alert decisions 75 → 19 on identical data).
* Shipped a least-privilege Chrome MV3 extension (storage + loopback only), with an end-to-end
  test driving real Chromium against the real backend and offline page fixtures, in CI on Linux
  and Windows.
* Designed history-aware deal detection that removed all 270 naive "% off" false positives on a
  400-series synthetic benchmark while keeping 100% of genuine drops (synthetic data).
