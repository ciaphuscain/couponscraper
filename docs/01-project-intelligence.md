# Part 1 — Project intelligence

## 1. The real engineering challenge

The hard problem is not *finding* coupon codes. It is answering, for one
specific product, one specific person and one specific moment:

> What is the lowest price I can **realistically** pay, how sure is that, and
> what exactly do I click?

That needs four things working together. Each is unglamorous and easy to get
subtly wrong:

1. **Evidence handling.** Every offer is a *claim* made by some source at some
   time. Claims go stale, conflict, and come from sources with different
   trustworthiness. Losing provenance turns a claim into a "fact" by accident.
2. **Eligibility under uncertainty.** Terms depend on facts we often don't
   know (is this a targeted offer? does the bank % apply before or after the
   coupon?). "Unknown" has to stay a distinct value all the way through.
3. **Correct money arithmetic.** That means integer paise, explicit
   application order, caps, minimum spends, and gift-card balances accounted
   at cash cost. Instant discounts must stay separate from deferred cashback.
4. **Access constraints.** Amazon's terms exclude robots and data extraction
   from its licence, and the Product Advertising API has been replaced by an
   eligibility-gated Creators API. So the system can't simply "go and get
   the data". It has to be useful mostly from what the user already sees and
   chooses to share with it.

## 2. Why conventional coupon scrapers fail

| Failure mode | Why it happens | What CouponHunter does instead |
|---|---|---|
| Expired or fake codes dominate | Sites optimise for code *count*; nobody revalidates | Lifecycle is *derived* from timestamps on every read; a silent source makes offers STALE, never more trusted |
| "Up to 80% off" is meaningless | Reference prices are set by sellers and often inflated | Deal quality is judged against our own observed median; "lowest" claims are bounded by history coverage |
| Stacking is assumed | Simplest code path is "sum all discounts" | Pairwise stacking is YES/NO/UNKNOWN; unknown pairs are flagged, and the subsets without them are always shown |
| Cashback counted as a discount | One "effective price" number | Instant, funding (gift-card) and deferred savings are separate fields; deferred is probability-weighted and never subtracted from checkout |
| Gift-card double-counting | Face value and discount both "saved" | Balance is modelled as a payment instrument with a cash cost ratio |
| Stealth scraping arms race | Blocks get answered with proxies/CAPTCHA solvers | Out of scope by design. An access refusal disables the source |
| Affiliate hijacking | Revenue model of many coupon extensions | No affiliate links are inserted or modified, ever |

## 3. Highest-value user workflows

1. **"I'm on a product page — what should I pay?"** The extension panel shows
   the lowest realistic price, the payment method to use and the steps to take.
   It also shows what *might* be cheaper and why that isn't confirmed. One click
   saves the page's price and offers.
2. **"Tell me when it's worth buying."** Watchlist + target price → a
   Telegram alert only when a materially better, eligible combination
   appears, outside quiet hours, never twice for the same state.
3. **"Did that actually work?"** Applied ✓ / Didn't apply ✗ feedback updates
   per-offer confidence. Purchases and cashback settlements give *realised*
   savings, kept separate from projected savings.

## 4. Biggest access and verification limitations

* **No automated amazon.in data.** We do not crawl Amazon. Prices and offers
  arrive only when the user views a page (extension), types them in, or
  imports them. Price-drop alerts can only fire on observations that reach
  the system. This is the system's most important functional limit.
* **Creators API is gated.** It requires an approved Associates account with
  recent qualifying sales (reported, not verified for India), so it is not
  assumed. Even where available, the PA-API 5 documentation we found exposes
  price, savings basis and deal type, but **no coupon or bank-offer fields**.
* **No cashback portal or bank APIs** are available to an individual.
  Portal and bank offers come from manual entry or the product page.
* **Redemption can't be tested automatically.** Submitting codes to checkout
  to see if they work would be speculative code submission. Verification
  instead comes from the user's own Applied/Failed feedback.
* **Offer text on product pages is truncated** in the carousel. Minimum
  spends or exclusions may be missing, so parse confidence drops and warnings
  are shown.

## 5. Most promising technical differentiators

* A **three-valued eligibility engine** (YES/NO/UNKNOWN) whose unknowns
  surface as explicit assumptions on each scenario.
* An **exact, explainable optimizer** that bounds unknown "basis" readings,
  verified against brute-force enumeration, and reports a confirmed best
  alongside a possible best.
* **Provenance-preserving dedup.** One offer seen in three places is one row
  with three sightings. Expiry disagreements resolve to the earliest date, and
  a high reject rate (a likely format change) never withdraws offers.
* **Honest alerts.** Thresholds, dedup keys, cooldown, downgrade suppression,
  quiet hours and grouping. No alert ever fires on a stale price.

## 6. Largest over-engineering risks (and what was rejected)

| Temptation | Why rejected |
|---|---|
| Microservices / queue / Kafka | One user, < 1k offers, one machine. A modular monolith is debuggable in one process |
| PostgreSQL | No concurrency need SQLite+WAL can't meet; adds a service to run on Windows |
| Playwright crawling | Not authorised for amazon.in; the browser the user already has is the "renderer" |
| LLM offer parsing | Deterministic regex parser measured 19/19 on the dev set; arithmetic must not be stochastic |
| CP/ILP solver | Search space is < a few thousand combinations after per-kind pruning; enumeration is exact and testable |
| React SPA | Server-rendered pages with a strict CSP are smaller, safer and need no build |
| Proxy rotation / CAPTCHA solving | Prohibited by design |
