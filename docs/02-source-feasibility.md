# Part 2 — Source feasibility and access register

Research date: 2026-10-25. Each claim is marked **[V]** (verified from a primary
source), **[S]** (secondary source; needs owner confirmation) or **[A]**
(assumption). The amazon.in and Associates pages could not be fetched from
the development container (DNS refused), so Amazon-specific claims below are
**[S]** unless stated. Confirm them in your browser before relying on them.

## Key findings

* **Amazon Conditions of Use [S].** Amazon's licence excludes "any collection
  and use of any product listings, descriptions, or prices" and "any use of
  data mining, robots, or similar data gathering and extraction tools". This
  is quoted from a summary of Amazon's Conditions of Use and the Amazon
  Payment Services terms, not the amazon.in page itself. **Consequence:** no
  server-side fetching of amazon.in, at any frequency.
* **PA-API 5.0 is retired; the Creators API replaces it [S].** Amazon's
  deprecation notice says PA-API calls now return 403; sources disagree on
  whether the cut-off was 30 Apr or 15 May 2026. A third party reports that
  eligibility requires an approved Associates account with ~10 qualifying
  sales in 30 days. This was not verified for India.
* **PA-API OffersV2 exposes price, SavingBasis, DealDetails (incl.
  LIGHTNING_DEAL) and availability; no coupon field was found [S].**
  Creators API field names were not verified.
* **Chrome Web Store affiliate policy (enforced from 10 Jun 2025) [V,
  developer.chrome.com]:** extensions may add affiliate links only with
  disclosure, a user action and a direct user benefit. CouponHunter inserts
  no affiliate links at all.
* **Telegram Bot API [S]:** keep to about 1 message per second per chat; 429
  responses carry `retry_after`. Free.

## Source-access assessment

| Source | Data | Auth | Eligibility | Rate limit | Permitted use | Storage / attribution | Cost | Reliability | Coverage | Effort | Available now? | Fallback |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Extension page capture** (implemented) | Title, price, list price, seller, stock, offer/coupon/deal text shown to *this* user | None (user's own session; no cookies read) | Any user | One capture per click (auto-capture opt-in) | **Grey area [A]**: user-initiated, single page the user chose to view, personal non-commercial use, no crawling or redistribution. Equivalent to the user copying text by hand, but arguably within "extraction tools". Owner must accept this (docs/06-security.md §Residual risks) | Local DB only; never redistributed | ₹0 | Breaks when Amazon changes markup → panel warns, manual entry still works | Only products the user visits | Done | Yes | Manual entry |
| **Manual entry** (implemented) | Anything the user types or pastes | — | — | — | Yes | Local | ₹0 | As good as the user | Whatever the user enters | Done | Yes | — |
| **File import JSON/CSV** (implemented) | Offers from emails, issuer pages, exported feeds | — | — | — | Yes, for data the user may use | Local | ₹0 | High (deterministic) | User-curated | Done | Yes | — |
| **RSS/Atom deal feeds** (implemented, off until terms reviewed) | Third-party deal headlines | None | Publisher's terms | Robots.txt honoured; ≥30-min interval; 429 backoff | Only if the publisher's terms permit feed use; owner records a `--terms-note` | Local; feed links are not reused (often affiliate-tagged) | ₹0 | Low (unverified claims) | Variable; often no bank-offer detail | Done | Depends on publisher | Manual |
| **Amazon Creators API** | Price, savings basis, deal type, availability | Associates credentials (OAuth-style) | Approved Associates account with recent qualifying sales [S] | Per-account TPS/TPD [A] | Display rules apply (attribution, freshness) [A] | Restricted caching [A] | ₹0 if eligible | High | Watched ASINs | Medium | **No** (not assumed) | Extension capture |
| **Amazon offer/coupon pages via crawler** | — | — | — | — | **Not permitted [S]** | — | — | — | — | — | **Rejected** | — |
| **Bank / card issuer offer pages** | Bank promo terms | None | — | — | Issuer ToS vary; not automated [A] | Local | ₹0 | Medium | Major banks | Manual copy-paste | Manual only | Extension (bank carousel on product page) |
| **Cashback portals (CashKaro, GoPaisa, …)** | Portal rates, tracked transactions | Account | Partner/affiliate programs only [A] | — | No public consumer API found [A] | — | — | — | — | — | **No** | Manual entry + cashback ledger |
| **Promotional emails** (implemented, file-based) | Codes, bank offers, stated expiry | None: you save `.eml` files or export `.mbox` (e.g. Google Takeout) | User | — | Your own mail, with your consent | Local; only offer lines + From/Subject kept | ₹0 | Low–medium (sender spoofing → always unverified) | Your subscriptions | Done (`couponhunter import-email`) | Yes | Paste text |
| **Gift-card resellers** | Discounted balance | Account | — | — | High fraud risk; transferability rules [A] | — | — | — | — | — | **Not integrated** | User models a known purchase as a wallet instrument with `cost_ratio_bp` |

## What can and cannot be verified automatically

| Question | Automatic? | How |
|---|---|---|
| Is the offer still listed by its sources? | Yes, for snapshot sources | Absence from a complete listing → `missing_since`; all sources missing → WITHDRAWN |
| Is it expired? | Yes, when an expiry is stated | Derived on every read; earliest stated expiry wins |
| Is the product / seller / category eligible? | Partly | Rule tree over known product facts; unknown facts → UNKNOWN |
| Does my payment method qualify? | Yes, categorically | Wallet holds bank/type/network, never numbers |
| New-customer-only, Prime-only? | Yes if profile is set | Otherwise UNKNOWN |
| Targeted / account-specific? | **No** | Always an assumption; never in "confirmed" |
| Do two offers stack? | Only if a source or the owner says so | Otherwise UNKNOWN → shown as "possible", not "confirmed" |
| Did it apply at checkout? | **No** (not tested automatically) | User's Applied/Failed feedback → Beta-style confidence update |
| Will cashback pay out? | **No** | Probability prior per kind, replaced by the measured rate per provider after ≥3 settled records |
