# Part 4 — Architecture

## Candidates compared

| Criterion | A: script + SQLite + Telegram | **B: modular monolith + local dashboard + extension** | C: event-driven workers + queue |
|---|---|---|---|
| Development complexity | Lowest | Moderate | High (queue semantics, retries across services) |
| Runtime resources | ~20 MB | ~30–60 MB, one process | Several processes + broker |
| Operational burden on Windows | Task Scheduler entry | One process started at logon | Broker service + supervisors |
| Debuggability | Good | Good (one process, structured logs, job_runs table) | Distributed traces needed |
| Failure recovery | Re-run | Transactions + leases + catch-up | Broker durability, idempotent consumers |
| Data volume (≤10⁴ offers, ≤10⁶ price rows) | Fine | Fine | Massive overkill |
| Extension support | None (no API) | Local JSON API | Yes |
| Scaling path | Rewrite | Swap SQLite → Postgres, split jobs into a worker if ever needed | Already "scaled" |

**Decision: B.** I challenged the hypothesis by asking whether A would do.
A cannot serve the extension, and the extension is the core
low-effort workflow (the product-page answer). C solves throughput and
isolation problems this workload does not have: one user, a handful of
sources, a few hundred evaluations a day. Inside B, every module is a plain
Python package with one-way dependencies, so a later split would not mean a
rewrite.

## Component and trust-boundary diagram

```
                         UNTRUSTED                                   │  TRUSTED (your PC, loopback only)
                                                                     │
 ┌──────────────────────┐  user clicks Save   ┌────────────────────┐ │  ┌──────────────────────────────────────────────┐
 │ amazon.in product    │ ──(DOM text only)──▶│ Extension content  │─┼─▶│ Extension service worker                     │
 │ page (user's browser)│                     │ script (closed     │ │  │  · checks sender + ASIN · loopback URL only  │
 └──────────────────────┘                     │ shadow DOM panel)  │◀┼──│  · validates responses against v1 schema    │
                                              └────────────────────┘ │  └───────────────┬──────────────────────────────┘
 ┌──────────────────────┐                                            │                  │ HTTP 127.0.0.1 + X-CouponHunter-Token
 │ RSS feeds (terms-    │──https, host-pinned, robots.txt ──────────┼──┐               ▼
 │ reviewed only)       │                                            │  │  ┌──────────────────────────────────────────┐
 └──────────────────────┘                                            │  │  │ web.app  (Host check · token · session  │
 ┌──────────────────────┐                                            │  │  │  cookie · CSRF · CSP · body cap)         │
 │ Files you import,    │──local read ──────────────────────────────┼──┤  └──────┬───────────────────────────┬───────┘
 │ text you paste       │                                            │  │         │                           │
 └──────────────────────┘                                            │  ▼         ▼                           ▼
                                                                     │ Source adapters ─▶ Normalisation ─▶ Ingest (dedup,
                                                                     │ (fetch/shape only)  (validate,        provenance,
                                                                     │                      allowlist URLs,   withdrawal)
                                                                     │                      parse text)          │
                                                                     │                                           ▼
                                                                     │                              SQLite (WAL) offers · sources ·
                                                                     │                              prices · alerts · history
                                                                     │                                           │
                                                                     │       Verification (lifecycle, confidence) ◀┘
                                                                     │                    │
                                                                     │       Eligibility (3-valued rules, wallet)
                                                                     │                    │
                                                                     │       Savings optimizer (exact enumeration)
                                                                     │          │                │              │
                                                                     │   Dashboard (HTML)   JSON API      Alert policy ─▶ Notifier
                                                                     │                                              │
 ┌──────────────────────┐                                            │                                              │
 │ Telegram Bot API     │◀── plain text, token from env var ─────────┼──────────────────────────────────────────────┘
 └──────────────────────┘                                            │
```

**Data boundaries.** Raw source text is stored only as evidence
(`offer_sources.raw_text`, `offers.source_text`) and is always rendered
escaped. Structured fields enter the domain model only through
`normalize_record`. Card details never cross any boundary: the wallet holds
categories (bank, type, network). Secrets (API token file, Telegram env
vars) never enter the database or logs.

## Data flow for the core question

1. The user opens `amazon.in/.../dp/ASIN`. The content script asks the service
   worker for `GET /api/v1/products/ASIN`.
2. `service.evaluate` loads the latest price, the product-scoped offers plus
   non-product offers, and the wallet/profile/stacking settings. The result is
   memoised on the DB version and a 5-minute bucket.
3. For each offer, verification derives lifecycle and confidence, and
   eligibility returns YES/NO/UNKNOWN with reasons.
4. The optimizer enumerates combinations per instrument and computes exact
   paise amounts (with bounding readings for unknown bases). It ranks by
   risk-adjusted cost and exposes `best_confirmed` and `best_possible`.
5. Pricing compares the result with the observed history.
6. The panel renders the answer. "Save" posts a capture; "Applied/Didn't
   apply" posts feedback.

## Module dependency direction

```
cli, web  ──▶  service, alerts, scheduler  ──▶  optimizer, eligibility, verification, pricing, rules, normalize
                         │                               ▲
                         ▼                               │ (pure; no I/O)
                     repo, ingest  ──▶  db              domain, money
                         │
                     sources (adapters: fetch + shape only)
```

The domain modules (`domain`, `money`, `rules`, `eligibility`, `verification`,
`optimizer`, `pricing`) import nothing from I/O modules, and the optimizer
tests run with zero network or database. You can add a source by
implementing `fetch(config) -> FetchResult` and registering it in
`sources/__init__.py`. Nothing else changes.

## Technology decision matrix

| Need | Choice | Alternatives considered | Why | Complexity added | How it fails | Replace when |
|---|---|---|---|---|---|---|
| Language | Python 3.11+ | — | Your primary language; good stdlib | — | — | — |
| HTTP API + pages | `http.server` (ThreadingHTTPServer) | FastAPI+uvicorn, Flask | Zero dependencies, so a Windows install has nothing to compile or pin; ~600 lines of routing is enough for 20 endpoints | Hand-written validation | Slow under heavy concurrency (irrelevant here) | You need OpenAPI, websockets, or >10 req/s sustained |
| Templates | Escape-by-default helpers + strict CSP | Jinja2, HTMX, React | No dependency; CSP forbids inline script so an escaping bug can't execute | Discipline in `web/html.py` (Raw type) | A missed `esc()` shows literal text (caught by tests) | Pages become interactive enough to need client state |
| Database | SQLite (WAL) | PostgreSQL | Single user, single machine; file backup; transactional | Single-writer lock (5 s busy timeout) | `database is locked` under external long transactions | Multiple writers on multiple machines |
| Scheduling | In-process thread + DB leases | APScheduler, Task Scheduler per job | 3 jobs; leases give no-overlap and crash safety; job_runs gives an audit trail | ~150 lines | Process not running → nothing runs (Task Scheduler starts it at logon) | Many jobs with cron-like calendars |
| Fetching | `urllib` (host-pinned, robots, retries) | HTTPX, Scrapy, Playwright | Feeds are small XML; no JS rendering needed; no crawling | — | — | A permitted source needs HTTP/2 or async fan-out |
| Parsing | Deterministic regex | LLM (Ollama) | Measured 19/19 on the dev set; explainable; ms per record | Regex maintenance | New phrasing → rejected and counted (not guessed) | Measured unknown-rate on real captures stays high |
| Optimiser | Exhaustive enumeration + pruning | CP-SAT, ILP | Search space ≤ 2,000 per instrument; exact; brute-force verified | — | Very large offer sets are pruned (noted in results) | Real offer sets regularly exceed pruning limits |
| Extension | MV3, plain JS, no build | TypeScript + bundler | ~400 lines; no toolchain on Windows; Node tests for logic | No static types | Runtime type errors (mitigated by schema validation) | Extension grows beyond a few files |
| Notifications | Telegram Bot API (plain text) | Email, browser push | Free, phone push, simple | Bot setup | 401 → permanent failure; 429 → retry_after | You need richer formatting |
| AI | None | Ollama | No measured need; arithmetic must be deterministic | — | — | See roadmap V6 |

See `docs/adr/` for the decision records.
