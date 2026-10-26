# Part 6 — Detailed design

## 1. Database schema

Source of truth: `couponhunter/migrations/0001_initial.sql` (applied by `db.migrate`, recorded
in `schema_migrations`). Money is INTEGER paise, rates INTEGER basis points, times
`YYYY-MM-DDTHH:MM:SSZ` text (lexicographic order = time order). Every fact table carries
`data_origin` ∈ {live, manual, imported, synthetic}.

| Table | Purpose | Key constraints / indexes |
|---|---|---|
| `products` | ASIN, title, brand, category, seller | `asin UNIQUE CHECK(length=10)` |
| `watchlist` | target price, per-product min savings, alerts on/off | PK = product_id (FK cascade) |
| `price_observations` | price, list price, seller, stock, source, time | `UNIQUE(product_id, source, observed_at)` (idempotent replays); index (product_id, observed_at) |
| `sources` | adapter, config (no secrets), terms review, health, backoff | PK id |
| `source_runs` | per-run counts: fetched/created/updated/duplicates/rejected, errors | index (source_id, started_at) |
| `offers` | normalised offer + aggregates | `fingerprint UNIQUE` (cross-source dedup); indexes on valid_until, kind |
| `offer_products` | product scope (no rows = not product-scoped) | PK (offer_id, asin); index asin |
| `offer_sources` | **provenance**: per-source sighting, evidence type, raw text, stated expiry, first/last seen, `missing_since` | PK (offer_id, source_id) |
| `redemption_attempts` | outcome, reason, observed savings, snapshot, channel | `idempotency_key UNIQUE`; index (offer_id, attempted_at) |
| `purchases` | reference price, checkout paid, cash paid | `idempotency_key UNIQUE`; CHECKs |
| `cashback_records` + `cashback_events` | settlement state machine + audit trail | status CHECK; transitions enforced in `repo.transition_cashback` (compare-and-set) |
| `alerts` | outbox: reason, payload, savings, status, attempts, next attempt | `dedup_key UNIQUE`; index (status, next_attempt_at) |
| `job_leases`, `job_runs` | scheduler mutual exclusion + audit | PK job |
| `settings` | JSON values (profile, wallet categories, thresholds) | PK key |

**Aggregates.** `offers.valid_until`, `authenticity`, `last_confirmed_at` and `withdrawn_at` are
recomputed from `offer_sources` in the same transaction as every sighting
(`ingest.refresh_offer_aggregates`). Earliest stated expiry wins; the most authoritative live
source sets authenticity; the offer is withdrawn only when *every* source is missing it.

**Retention.** Nightly job: delete `source_runs`/`job_runs` older than 90 days; delete sent,
suppressed and failed alerts older than 180 days; keep price history and financial records.
Backups: 14 most recent kept.

## 2. Source adapter interface

```python
class SourceAdapter(Protocol):
    adapter: ClassVar[str]                 # registry key
    requires_terms_review: ClassVar[bool]  # network adapters: True
    def fetch(self, config: dict) -> FetchResult: ...

FetchResult(records: list[SourceRecord], snapshot: bool, data_origin: str,
            schema_fingerprint: str | None, snapshot_asin: str | None)
SourceRecord(data: dict, evidence_type: str, authenticity: Authenticity, source_url, raw_text)
SourceError(kind = transient | auth | rate_limited | config | schema | not_found, retry_after_s)
```

Adapters fetch and shape; `ingest.run_source` does everything else:

| Concern | Where |
|---|---|
| Validate / normalise / reject | `normalize.normalize_record` (rejects counted, never guessed) |
| Deduplicate | fingerprint (kind, provider slug, economics, code, payment, scope, eligibility) + `ON CONFLICT DO NOTHING` |
| Timestamp + provenance | `offer_sources` row per source |
| Rate limits | `min_interval_s`, `next_fetch_at` (exponential backoff, `Retry-After`) |
| Schema change detection | `schema_fingerprint` change → warning; reject ratio > 50% → run is `partial` and **no withdrawals** |
| Self-disable | auth/config errors immediately; 5 consecutive transient failures |

## 3. Offer state model

Four independent dimensions plus one record-level state machine:

| Dimension | Values | Derived from |
|---|---|---|
| Authenticity | official · source_listed · unverified · disputed · invalid | best live source's evidence; owner flag overrides |
| Lifecycle | active · not_started · stale · expired · withdrawn | `derive_lifecycle(now, valid_from, valid_until, last_confirmed_at, withdrawn, stale_after)` on every read |
| Eligibility (per product × user × instrument × time) | yes · no · unknown (+ reasons) | `eligibility.check_offer`, `match_payment` |
| Redemption evidence | counts of applied / failed attempts | `redemption_attempts` |
| Cashback settlement (per purchase) | expected → tracked → pending → confirmed → paid; →rejected (→pending on appeal) | `cashback_records` + events |

Confidence (probability that the offer applies when tried) =
`(successes·10000 + prior·2) / (attempts + 2)`, where the prior comes from authenticity
(official 95%, listed 75%, unverified 50%, disputed 20%), × 0.6 if stale. This is a documented
heuristic, not a learned model.

## 4. Eligibility rules

JSON rule trees evaluated with Kleene three-valued logic (`rules.py`):

```json
{"all": [
  {"field": "product.category", "op": "in", "value": ["Electronics"]},
  {"not": {"field": "product.seller", "op": "eq", "value": "Marketplace Seller X"}},
  {"field": "user.is_prime", "op": "eq", "value": true}
]}
```

Fields: `product.{asin,category,seller,brand,price}`, `user.{is_prime,is_new_customer}`. Ops:
eq, ne, in, not_in, gte, lte, gt, lt, contains. A missing field gives `unknown`, never `false`.
Rules are validated at ingest (depth ≤ 16). Flags with fixed semantics: `new_customer_only`,
`prime_only`, `account_specific` (always unknown → never "confirmed"), `one_time_use`.

## 5. Savings calculation rules

Application order: deal price (already in the observed price; informational) → product coupon →
promo code → bank instant discount → deferred benefits.

For each instant offer:
`basis_amount` = price (`PRICE`) | price − coupons so far (`AFTER_COUPONS`) | price − all instant
so far (`PAYABLE`). Minimum spend is checked against the same basis. Benefit = flat value or
⌊basis × bp / 10000⌋, capped at `max_discount` and at the remaining price.

```
checkout_payable   = price − Σ instant
cash_cost          = ⌈checkout_payable × instrument.cost_ratio_bp / 10000⌉   (gift-card balance)
funding_savings    = checkout_payable − cash_cost
expected_deferred  = Σ deferred (cashback), basis PRICE or PAYABLE — shown separately, never subtracted
risk_adjusted_cost = cash_cost(expected payable with instant offers weighted by confidence)
                     − Σ deferred × settlement probability × confidence
effective_if_all_paid = cash_cost − expected_deferred   (labelled as such)
```

**Unknown basis.** Four bounding readings (all least favourable; all at price; coupons at price +
others least favourable; instant at price + deferred least favourable). Headline = highest
payable; range shown. A combination is feasible only if every reading meets every minimum spend.
Verified against brute force in `tests/test_optimizer.py::BasisBounds`.

**Stacking.** Pairwise: explicit exclusion → NO; same kind → NO; deal price → YES; declared on
either side (NO wins; YES only if neither side says UNKNOWN); owner defaults; else UNKNOWN.
UNKNOWN pairs are allowed but make the scenario unconfirmed.

**Gift-card balance.** Modelled as a payment instrument with `cost_ratio_bp` (9700 = bought at 3%
off). Its saving is `funding_savings`. Face value is never counted as a saving, and bank offers
can't match it (no issuing bank).

### Worked example (generated by the optimizer, `tests/test_optimizer.py`)

Price ₹10,000 · coupon ₹500 · HDFC 10% up to ₹750 · portal cashback 5% up to ₹300 (80% settlement
prior) · wallet: HDFC credit card.

| Case | Confirmed best | Checkout | Cash cost | Cashback later (max / risk-adj.) | Possible best (unconfirmed) |
|---|---|---|---|---|---|
| All three stack; bank % after coupon; cashback on payable | coupon + HDFC + cashback | ₹8,750 | ₹8,750 | ₹300 / ₹228 | same |
| All stack; bank % on list price | coupon + HDFC + cashback | ₹8,750 | ₹8,750 | ₹300 / ₹228 | same: the ₹750 cap binds either way (₹1,000 vs ₹950 → ₹750) |
| Coupon and bank offer do NOT stack | HDFC + cashback | ₹9,250 | ₹9,250 | ₹300 / ₹228 | same |
| Stacking unknown (nothing declared) | HDFC alone | ₹9,250 | ₹9,250 | — | coupon + HDFC + cashback → ₹8,750 |
| Bank cap raised to ₹2,000, basis unknown | HDFC alone (₹1,000 off) | ₹9,000 | ₹9,000 | — | coupon + HDFC → ₹8,550 (₹8,500 if the bank % applies to list price) |
| Paying with gift-card balance bought at 3% off | coupon + cashback (HDFC offer can't apply) | ₹9,500 | ₹9,215 | ₹300 / ₹228 | same |

Risk-adjusted cashback ₹228 = ₹300 × 0.80 settlement × 0.95 confidence. The confirmed answer never
includes cashback in the price.

## 6. API (v1, loopback, `X-CouponHunter-Token` required)

| Method & path | Body | Returns |
|---|---|---|
| `GET /healthz` (no auth) | — | `{"ok": true}` |
| `GET /api/v1/status` | — | version, api_version, sources health, last job runs, alert outbox counts |
| `GET /api/v1/products/{ASIN}` | — | Evaluation: `price`, `price_is_stale`, `baseline`, `best_confirmed`, `best_possible`, `scenarios[≤8]`, `excluded[]`, `offers[]`, `notes[]`, `price_stats`, `deal_verdict`, `tracked`; 404 `{tracked:false}` if unknown |
| `POST /api/v1/products` | `{url|asin, title?, target_price?}` | 201 `{asin, tracked}` |
| `DELETE /api/v1/products/{ASIN}` | — | `{asin, tracked:false}` |
| `POST /api/v1/captures` | `{asin, price, list_price?, title?, seller?, in_stock?, offers:[text \| {kind:"deal_price",title}], complete?, watch?}` | 201 Evaluation + `capture` counts |
| `POST /api/v1/redemptions` | `{offer_id, asin?, outcome: applied\|failed\|not_shown\|partial, failure_reason?, observed_savings?, idempotency_key}` | 201 `{id}` (idempotent) |
| `GET /api/v1/offers` | — | active offers with confidence and scope |

All amounts are integer paise. Errors: `{"error": "..."}` with 400/401/404/413/421. Bodies are
capped at 64 KiB. Scenario objects: see `service.scenario_json`; the extension validates them
(`extension/lib.js::validateEvaluation`).

## 7. Notification design

`alerts.decide` evaluates each watched product (skipping stale prices and combinations with < 50%
success probability). Reasons, in priority order: at/below target; immediate savings ≥ max(₹200,
3% of price) (configurable); genuine price drop per deal-quality rules.

`enqueue`:
* dedup key = hash(product, reason kind, offer set, instrument, payable). An existing key is
  never re-alerted;
* within the cooldown (12 h), only a materially better saving (+max(₹100, 10%));
* a worse saving than one alerted in the last 7 days is suppressed (target alerts excepted);
* quiet hours (22:30–07:30 IST) → `deferred` until they end.

`deliver`: due alerts; ≥ 3 at once are sent as one digest; failures retry with exponential
backoff or `retry_after`; 400/401/403/404 or 5 attempts → `failed`. Message content: product,
reason, price now, confirmed checkout savings and what you pay, possible later cashback
(labelled not guaranteed), success probability, expiry within 48 h, payment method, top
assumptions, steps, deal caveat, `[SYNTHETIC TEST DATA]` marker if applicable, and the clean
product URL (no affiliate tags).

## 8. Extension architecture (Chrome MV3)

| File | Role | Permissions it relies on |
|---|---|---|
| `manifest.json` | `storage` permission; host permission `http://127.0.0.1/*`, `http://localhost/*`; content script on 4 amazon.in product URL patterns only | — |
| `lib.js` | Pure helpers: ASIN from URL, INR parsing, URL allowlists, backend URL validation, response schema validation, `extractPage` (fixed selectors, text only) | — |
| `content.js` | Closed Shadow DOM panel, `textContent` only; Save/Track, copy code, open allowlisted link, Applied/Didn't apply | none (talks only to its own service worker) |
| `background.js` | Sole backend client: checks `sender.id`, that the sender is an amazon.in product page, and that the message ASIN matches the tab; popup/options may only ask for status; 4 s timeout; `credentials: "omit"` | `storage`, host permission |
| `popup.*`, `options.*` | Status, dashboard link; backend URL (loopback only), token, auto-capture opt-in | `storage` |

Failure behaviour: backend down → "backend not reachable"; schema mismatch → "update the
extension/backend"; selectors broken → "couldn't read the price", with a warning and manual entry
still available. It never interacts with cart, checkout, payment, OTP or account elements.
