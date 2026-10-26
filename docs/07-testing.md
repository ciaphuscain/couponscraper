# Testing plan and coverage of required cases

Run everything:

```powershell
python -m unittest discover -s tests -t . -v      # 120+ Python tests, stdlib only
node --test tests/extension/lib.test.js           # extension logic
node tests/extension/e2e.js                       # real Chromium + extension + backend (needs Playwright)
```

CI (`.github/workflows/ci.yml`) runs the first two plus a CLI smoke test on Ubuntu and **Windows**
(Python 3.11 and 3.13), and the E2E test on Ubuntu.

| Layer | Files |
|---|---|
| Unit (pure domain) | `test_optimizer.py`, `test_parsing.py` (money, URLs, normalise, text parser, rules, pricing) |
| Property (vs brute force) | `test_optimizer.py::BasisBounds` (400 random offer sets) |
| Database / recovery | `test_db.py` (constraints, state machine, SIGKILL mid-transaction, backup/restore, cache invalidation across connections) |
| Integration (pipeline) | `test_ingest.py`, `test_alerts.py`, `test_scheduler.py` |
| API / security | `test_web.py` (auth, CSRF, DNS rebinding, XSS, body cap, malformed input, real socket) |
| Contract | `lib.test.js::validateEvaluation` ↔ `service.scenario_json` |
| Extension unit | `tests/extension/lib.test.js` |
| End-to-end | `tests/extension/e2e.js` (amazon.in served from a local fixture) |
| Performance / soak | `benchmarks/bench.py` |
| Experiments | `benchmarks/experiments.py` |

## The 25 required cases

| # | Case | Test |
|---|---|---|
| 1 | Expired coupon | `EligibilityCases.test_expired_offer_excluded` |
| 2 | Unknown expiration | `test_unknown_expiry_is_noted_not_excluded` |
| 3 | New-user-only | `test_new_user_only` |
| 4 | Account-specific | `test_account_specific_never_confirmed` |
| 5 | Wrong payment method | `test_wrong_payment_method` |
| 6 | Minimum spend not met | `test_minimum_spend_not_met`, `test_minimum_spend_after_coupon` |
| 7 | Discount cap reached | `test_discount_cap`, `test_cap_makes_bank_basis_irrelevant_in_spec_fixture` |
| 8 | Two mutually exclusive coupons | `test_two_product_coupons_mutually_exclusive`, `test_explicit_exclusion` |
| 9 | Coupon + bank stack | `WorkedExample.test_all_three_stack_bank_after_coupon` |
| 10 | Coupon + bank don't stack | `test_coupon_and_bank_do_not_stack` |
| 11 | Cashback pending but not paid | `test_db.py::test_pending_cashback_not_counted_as_realized`, `test_cashback_never_reduces_checkout_amount` |
| 12 | Gift-card double counting | `GiftCardAccounting.*` |
| 13 | Product variant mismatch | `test_variant_mismatch_by_asin` |
| 14 | Seller mismatch | `test_seller_mismatch` |
| 15 | Duplicate offer from multiple sources | `test_cross_source_dedup_keeps_provenance` |
| 16 | Stale price observation | `test_alerts.py::test_stale_price_never_alerts` |
| 17 | Missing price history | `PricingTests.test_insufficient_history`, `test_web.py::test_unknown_product` |
| 18 | Malformed HTML/XML from source | `RssTests.test_malformed_and_not_a_feed`, `test_high_reject_rate_does_not_withdraw`; extension `extractPage` layout-change test |
| 19 | Network timeout | `HttpClientTests.test_bounded_retry_then_fail`, `test_failure_does_not_refresh_and_offers_go_stale` |
| 20 | Notification delivery failure | `test_delivery_failure_retries_then_fails`, `test_permanent_failure` |
| 21 | Invalid Amazon URL | `AmazonUrlTests.test_rejects` |
| 22 | Malicious external URL | `test_redemption_url_allowlist`, `test_manual_offer_text` |
| 23 | Process crash during update | `CrashRecoveryTests.test_kill_during_write_leaves_consistent_db`, `test_crash_mid_ingest_rolls_back` |
| 24 | Database constraint violation | `SchemaTests.test_constraints` |
| 25 | Extension receives malformed API data | `lib.test.js` "validateEvaluation rejects malformed API data" |

## Tests that caught real bugs during development

* Double-escaped dashboard markup: found by screenshot review; `test_markup_not_double_escaped`
  added and shown to fail on the old code.
* A two-reading shortcut for unknown bases accepted infeasible combinations: caught by the
  brute-force property test.
* Scenario trimming changed which scenario was "best confirmed": found because the benchmark's
  alert-decision count changed on identical data; `TrimTests` added and shown to fail on the old
  code.
* Flat-price "lowest observed" and within-noise drops flagged as deals: found by
  `test_inflated_reference_price` and Experiment D.
* Synthetic fixture offers merging into real captures: found while writing the E2E test.
