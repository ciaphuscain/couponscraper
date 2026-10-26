# CouponHunter

A personal, local savings engine for **Amazon India**. It answers one question:

> If I want to buy this product on amazon.in right now, what is the lowest price I can
> **realistically** pay with the discounts and cashback I'm eligible for, how sure is that, and
> what's the easiest legitimate way to get it?

* Runs on your own PC (Windows-first), ₹0 recurring cost, **no third-party Python dependencies**.
* **On PC:** a Chrome/Edge extension shows the answer on amazon.in product pages, including a
  buy-now / wait verdict, and (opt-in) keeps watched products' prices fresh as you browse.
* **On your phone:** share a product from the Amazon app to your Telegram bot to track it; ask
  `/best`, record prices and offers, report what worked. Alerts arrive there too.
* **Dashboard:** watchlist, offers, card insights (which card to use, which card you're missing),
  purchases and a cashback ledger that counts savings only when they're real.
* It **never** crawls Amazon, bypasses anti-bot controls, guesses coupon codes, automates
  checkout, inserts affiliate links, or touches your passwords, cards or OTPs.

## What makes it different

| Typical coupon tool | CouponHunter |
|---|---|
| Lists every code it found | Shows only what applies to *you*, *this product*, *now*, with reasons for what doesn't |
| Adds every discount together | Models stacking (yes/no/unknown), application order, caps, minimum spends; shows a **confirmed** best and a **possible** best with the exact assumptions |
| "Effective price" includes cashback | Cashback is shown separately, probability-weighted, and counted as saved only once **paid** |
| "Up to 70% off!" | Judges deals against observed price history; never claims an all-time low without coverage |
| Old offers linger | An offer no source has re-confirmed becomes *stale* and drops out of answers and alerts |

## Quick start (Windows PowerShell)

```powershell
git clone https://github.com/ciaphuscain/couponscraper.git; cd couponscraper
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -AutoStart   # add -Demo for SYNTHETIC sample data
couponhunter doctor          # checks everything and tells you how to fix what isn't right
```
Dashboard: http://127.0.0.1:8765 (sign in with `couponhunter token`).

Then load the extension (`chrome://extensions` → Developer mode → Load unpacked → `extension/`)
and paste the token in its options. Full guide: [docs/09-operations.md](docs/09-operations.md)
(install, auto-start, Telegram, backup, troubleshooting) and [docs/10-user-manual.md](docs/10-user-manual.md).

## Example (from the test suite)

Price ₹10,000 · coupon ₹500 · HDFC 10% up to ₹750 · cashback 5% up to ₹300:

| Rules | Answer |
|---|---|
| All confirmed to stack | Pay **₹8,750** with HDFC; + up to ₹300 cashback later (risk-adjusted ₹228) |
| Coupon and bank offer don't stack | Pay **₹9,250** (bank offer beats coupon) |
| Stacking unknown | Confirmed **₹9,250**; *possibly* ₹8,750 if the coupon and bank offer combine |
| Paying with gift-card balance bought at 3% off | Checkout ₹9,500, **cash cost ₹9,215** (bank offer can't apply; face value not double-counted) |

## Status

| Area | State |
|---|---|
| Offer import (JSON/CSV), manual entry, RSS (terms-gated), extension capture | ✅ |
| Dedup with provenance, expiry/staleness, source health and auto-disable | ✅ |
| Eligibility (3-valued), payment matching, stacking, optimizer | ✅ brute-force verified |
| Dashboard, JSON API, scheduler, Telegram/file alerts with anti-spam | ✅ |
| Chrome MV3 extension | ✅ unit + end-to-end tested (not on the Web Store) |
| Price history and deal quality | ✅ (only from observations that reach the system) |
| Cashback ledger, realised-savings accounting | ✅ |
| Buy-now / wait advice, price sparkline | ✅ evidence-named |
| Telegram companion bot (share from the Amazon app) | ✅ tested against a fake Bot API over HTTP |
| Opt-in passive price observation of watched products | ✅ E2E tested |
| Cross-product inference of bank offers ("possibly applies") | ✅ never confirmed, never alerted |
| Card insights | ✅ |
| `couponhunter doctor`, one-command Windows setup | ✅ setup script run in Windows CI |
| Amazon Creators API, email import, cashback-portal APIs | ❌ not available / not built (see [docs/02](docs/02-source-feasibility.md)) |

**Biggest limitation:** there's no permitted automated access to amazon.in. Prices and offers come
from what you see: product pages (extension), pages you browse (opt-in observation), what you
share or type into the Telegram bot, or imports. A product you never look at stays unpriced.

## Tests

```powershell
python -m unittest discover -s tests -t .     # Python (stdlib only)
node --test tests/extension/lib.test.js       # extension logic
node tests/extension/e2e.js                   # real Chromium + extension + backend (needs Playwright)
```
CI runs on Ubuntu and Windows (Python 3.11 and 3.13) plus the extension E2E test.

## Documentation

1. [Project intelligence](docs/01-project-intelligence.md)
2. [Source feasibility and access register](docs/02-source-feasibility.md)
3. [Requirements, assumptions, success metrics](docs/03-requirements.md)
4. [Architecture, diagram, technology matrix](docs/04-architecture.md) · [ADRs](docs/adr/)
5. [Detailed design: schema, state model, rules, savings maths, API, extension](docs/05-design.md)
6. [Security threat model](docs/06-security.md)
7. [Testing plan (all 25 required cases mapped)](docs/07-testing.md)
8. [Benchmarks and experiments](docs/08-benchmarks.md)
9. [Operations: Windows install, config, backup, troubleshooting, cost](docs/09-operations.md)
10. [User manual and extension install](docs/10-user-manual.md)
11. [Limitations, roadmap, scorecard, retrospective, interview bank](docs/11-retrospective.md)

## Repository layout

```
couponhunter/        domain (pure): domain, money, rules, eligibility, verification, optimizer, pricing
                     pipeline: normalize, ingest, sources/ (file_import, rss, http), repo, db, migrations/
                     app: service, alerts, notify/, scheduler, web/ (app, pages, html, server), cli, config, logs
extension/           Chrome MV3 extension (no build step)
tests/               unittest suites + tests/extension (Node unit + Playwright E2E)
benchmarks/          bench.py, experiments.py, results/
fixtures/            synthetic demo offers (labelled)
docs/                everything above
```

Personal-use software. Respect amazon.in's Conditions of Use and every source's terms; see
docs/06 §Residual risks.
