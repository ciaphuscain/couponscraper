"""Worked-example and edge-case tests for the savings optimizer.

Spec fixture: price ₹10,000, product coupon ₹500, bank offer 10% up to ₹750,
cashback 5% up to ₹300. Each test pins down one stacking/basis interpretation.
"""

import unittest
from datetime import timedelta

from couponhunter.domain import (
    Authenticity,
    Basis,
    DiscountType,
    Lifecycle,
    OfferKind,
    PaymentInstrument,
)
from couponhunter.money import rupees
from couponhunter.optimizer import optimize, rank_by
from tests.factories import (
    HDFC_CC,
    NOW,
    NO,
    SBI_DC,
    UNKNOWN,
    YES,
    cashback_5pct,
    coupon_500,
    hdfc_10pct,
    offer,
    product,
    user,
)

ALL_STACK = {"product_coupon": YES, "bank_instant": YES, "portal_cashback": YES}


def ids(s):
    return set(s.offer_ids)


class WorkedExample(unittest.TestCase):
    def test_all_three_stack_bank_after_coupon(self):
        offers = [coupon_500(stacks_with=ALL_STACK), hdfc_10pct(stacks_with=ALL_STACK), cashback_5pct(stacks_with=ALL_STACK)]
        r = optimize(product(), offers, user(HDFC_CC), NOW)
        best = r.best_confirmed
        self.assertEqual(ids(best), {"coupon500", "hdfc10", "cb5"})
        # 10,000 - 500 = 9,500; 10% of 9,500 = 950 -> capped at 750; payable 8,750
        self.assertEqual(best.checkout_payable, rupees(8_750))
        self.assertEqual(best.instant_savings, rupees(1_250))
        # 5% of 8,750 = 437.50 -> capped 300; deferred, NOT in checkout price
        self.assertEqual(best.expected_deferred, rupees(300))
        self.assertEqual(best.effective_if_all_paid, rupees(8_450))
        # risk-adjusted: deferred 300 * 0.8 settlement * 0.95 confidence = 228
        self.assertEqual(best.risk_adjusted_deferred, rupees(228))
        self.assertTrue(best.rules_confirmed)

    def test_cap_makes_bank_basis_irrelevant_in_spec_fixture(self):
        # Before coupon: 10% of 10,000 = 1,000 -> 750. After: 950 -> 750. Same.
        for basis in (Basis.PRICE, Basis.AFTER_COUPONS, Basis.UNKNOWN):
            offers = [coupon_500(stacks_with=ALL_STACK), hdfc_10pct(basis=basis, stacks_with=ALL_STACK)]
            best = optimize(product(), offers, user(HDFC_CC), NOW).scenarios[0]
            self.assertEqual(best.checkout_payable, rupees(8_750), basis)

    def test_bank_basis_matters_when_cap_is_higher(self):
        def run(basis):
            o = [coupon_500(stacks_with=ALL_STACK), hdfc_10pct(basis=basis, max_discount=rupees(2_000), stacks_with=ALL_STACK)]
            return optimize(product(), o, user(HDFC_CC), NOW)

        self.assertEqual(run(Basis.PRICE).scenarios[0].checkout_payable, rupees(8_500))  # 10,000-500-1,000
        self.assertEqual(run(Basis.AFTER_COUPONS).scenarios[0].checkout_payable, rupees(8_550))  # 950 off
        unk = run(Basis.UNKNOWN)
        both = next(s for s in unk.scenarios if ids(s) == {"coupon500", "hdfc10"})
        # Unknown basis -> headline uses the less favourable reading, range disclosed.
        self.assertEqual(both.checkout_payable, rupees(8_550))
        self.assertEqual(both.optimistic_payable, rupees(8_500))
        self.assertFalse(both.rules_confirmed)
        self.assertTrue(any("before or after" in a for a in both.assumptions))

    def test_coupon_and_bank_do_not_stack(self):
        no_stack = {"bank_instant": NO, "portal_cashback": YES}
        offers = [
            coupon_500(stacks_with=no_stack),
            hdfc_10pct(stacks_with={"product_coupon": NO, "portal_cashback": YES}),
            cashback_5pct(stacks_with={"product_coupon": YES, "bank_instant": YES}),
        ]
        r = optimize(product(), offers, user(HDFC_CC), NOW)
        for s in r.scenarios:
            self.assertFalse({"coupon500", "hdfc10"} <= ids(s))
        best = r.best_confirmed
        # bank alone (₹750) beats coupon alone (₹500)
        self.assertEqual(ids(best), {"hdfc10", "cb5"})
        self.assertEqual(best.checkout_payable, rupees(9_250))
        self.assertEqual(best.expected_deferred, rupees(300))  # 5% of 9,250 = 462.50 -> cap

    def test_unknown_stacking_shows_both_conservative_and_possible(self):
        offers = [coupon_500(), hdfc_10pct()]  # nothing declared
        r = optimize(product(), offers, user(HDFC_CC), NOW)
        self.assertEqual(ids(r.best_possible), {"coupon500", "hdfc10"})
        self.assertFalse(r.best_possible.rules_confirmed)
        self.assertTrue(any("can be combined" in a for a in r.best_possible.assumptions))
        self.assertEqual(ids(r.best_confirmed), {"hdfc10"})
        self.assertEqual(r.best_confirmed.checkout_payable, rupees(9_250))

    def test_cashback_basis_unknown_range(self):
        cb = cashback_5pct(basis=Basis.UNKNOWN, max_discount=None, stacks_with=ALL_STACK)
        r = optimize(product(), [coupon_500(stacks_with=ALL_STACK), cb], user(HDFC_CC), NOW)
        s = next(s for s in r.scenarios if ids(s) == {"coupon500", "cb5"})
        # pessimistic: 5% of payable 9,500 = 475 (vs 500 on list price)
        self.assertEqual(s.expected_deferred, rupees(475))
        self.assertFalse(s.rules_confirmed)

    def test_cashback_never_reduces_checkout_amount(self):
        r = optimize(product(), [cashback_5pct()], user(HDFC_CC), NOW)
        s = next(s for s in r.scenarios if s.applied)
        self.assertEqual(s.checkout_payable, rupees(10_000))
        self.assertEqual(s.cash_cost, rupees(10_000))
        self.assertEqual(s.expected_deferred, rupees(300))
        self.assertLess(s.risk_adjusted_deferred, s.expected_deferred)


class EligibilityCases(unittest.TestCase):
    def test_wrong_payment_method(self):
        r = optimize(product(), [hdfc_10pct()], user(SBI_DC), NOW)
        self.assertTrue(all(not s.applied for s in r.scenarios))
        self.assertIn("hdfc10", {e.offer_id for e in r.excluded})

    def test_hypothetical_instrument_is_suggested_but_flagged(self):
        r = optimize(product(), [hdfc_10pct()], user(SBI_DC), NOW, include_hypothetical=True)
        s = next(s for s in r.scenarios if s.applied)
        self.assertTrue(s.instrument.hypothetical)
        self.assertFalse(s.rules_confirmed)
        self.assertEqual(r.best_confirmed.checkout_payable, rupees(10_000))

    def test_minimum_spend_not_met(self):
        o = hdfc_10pct(min_order=rupees(15_000))
        r = optimize(product(), [o], user(HDFC_CC), NOW)
        self.assertTrue(all(not s.applied for s in r.scenarios))

    def test_minimum_spend_after_coupon(self):
        # min ₹9,800 is met on list price (10,000) but not after the ₹500 coupon (9,500)
        stack = {"product_coupon": YES, "bank_instant": YES}
        o = hdfc_10pct(min_order=rupees(9_800), basis=Basis.AFTER_COUPONS, stacks_with=stack)
        r = optimize(product(), [coupon_500(stacks_with=stack), o], user(HDFC_CC), NOW)
        self.assertFalse(any(ids(s) == {"coupon500", "hdfc10"} for s in r.scenarios))
        self.assertEqual(ids(r.best_confirmed), {"hdfc10"})

    def test_discount_cap(self):
        o = hdfc_10pct(max_discount=rupees(100))
        s = optimize(product(), [o], user(HDFC_CC), NOW).scenarios[0]
        self.assertEqual(s.instant_savings, rupees(100))

    def test_expired_offer_excluded(self):
        o = coupon_500(valid_until=NOW - timedelta(hours=1), lifecycle=Lifecycle.EXPIRED)
        r = optimize(product(), [o], user(), NOW)
        self.assertEqual(r.excluded[0].offer_id, "coupon500")
        self.assertIn("expired", r.excluded[0].reason)

    def test_unknown_expiry_is_noted_not_excluded(self):
        r = optimize(product(), [coupon_500(valid_until=None)], user(), NOW)
        self.assertEqual(ids(r.scenarios[0]), {"coupon500"})

    def test_stale_offer_is_not_confirmed(self):
        r = optimize(product(), [coupon_500(lifecycle=Lifecycle.STALE)], user(), NOW)
        self.assertEqual(r.best_confirmed.applied, [])

    def test_new_user_only(self):
        o = coupon_500(new_customer_only=True)
        self.assertIn("coupon500", {e.offer_id for e in optimize(product(), [o], user(is_new_customer=False), NOW).excluded})
        r = optimize(product(), [o], user(), NOW)
        self.assertFalse(r.best_possible.rules_confirmed)
        self.assertEqual(r.best_confirmed.applied, [])

    def test_account_specific_never_confirmed(self):
        r = optimize(product(), [coupon_500(account_specific=True)], user(), NOW)
        self.assertEqual(r.best_confirmed.applied, [])
        self.assertTrue(any("targeted" in a for a in r.best_possible.assumptions))

    def test_two_product_coupons_mutually_exclusive(self):
        c2 = offer("coupon300", OfferKind.PRODUCT_COUPON, discount_type=DiscountType.FLAT, value=rupees(300), basis=Basis.PRICE)
        r = optimize(product(), [coupon_500(), c2], user(), NOW)
        self.assertTrue(all(len(s.applied) <= 1 for s in r.scenarios))
        self.assertEqual(ids(r.best_confirmed), {"coupon500"})

    def test_explicit_exclusion(self):
        a = coupon_500(stacks_with=ALL_STACK)
        b = hdfc_10pct(stacks_with=ALL_STACK, excludes=frozenset({"coupon500"}))
        r = optimize(product(), [a, b], user(HDFC_CC), NOW)
        self.assertFalse(any({"coupon500", "hdfc10"} <= ids(s) for s in r.scenarios))

    def test_seller_mismatch(self):
        rule = {"field": "product.seller", "op": "eq", "value": "Appario Retail"}
        o = coupon_500(eligibility=rule)
        r = optimize(product(seller="Some Other Seller"), [o], user(), NOW)
        self.assertEqual(r.excluded[0].offer_id, "coupon500")
        r2 = optimize(product(), [o], user(), NOW)  # seller unknown
        self.assertFalse(r2.best_possible.rules_confirmed)

    def test_variant_mismatch_by_asin(self):
        o = coupon_500(eligibility={"field": "product.asin", "op": "in", "value": ["B0OTHERVAR"]})
        r = optimize(product(), [o], user(), NOW)
        self.assertEqual(r.excluded[0].offer_id, "coupon500")

    def test_deal_price_not_double_counted(self):
        deal = offer("ld", OfferKind.DEAL_PRICE, discount_type=DiscountType.NONE, title="Lightning Deal")
        r = optimize(product(), [deal], user(), NOW)
        self.assertEqual(r.best_confirmed.checkout_payable, rupees(10_000))
        self.assertTrue(any("Lightning Deal" in n for n in r.notes))

    def test_disputed_offer_lowers_confidence(self):
        r = optimize(product(), [coupon_500(authenticity=Authenticity.DISPUTED)], user(), NOW)
        self.assertLess(r.best_possible.success_probability_bp, 5_000)


class GiftCardAccounting(unittest.TestCase):
    def test_discounted_gift_card_not_double_counted(self):
        # Balance bought at 3% below face value. Paying ₹10,000 of balance costs ₹9,700 cash.
        gc = PaymentInstrument("gc", "Amazon Pay balance (bought at 3% off)", "amazon_pay_balance", cost_ratio_bp=9_700)
        r = optimize(product(), [hdfc_10pct()], user(gc), NOW)
        s = r.best_confirmed
        self.assertEqual(s.checkout_payable, rupees(10_000))
        self.assertEqual(s.cash_cost, rupees(9_700))
        self.assertEqual(s.funding_savings, rupees(300))
        self.assertEqual(s.instant_savings, 0)  # HDFC offer can't apply to balance payments
        self.assertIn("hdfc10", {e.offer_id for e in r.excluded})

    def test_card_beats_gift_card_when_bank_offer_bigger(self):
        gc = PaymentInstrument("gc", "Gift card balance", "amazon_pay_balance", cost_ratio_bp=9_700)
        r = optimize(product(), [hdfc_10pct()], user(gc, HDFC_CC), NOW)
        self.assertEqual(r.best_confirmed.instrument.id, "hdfc-cc")  # 750 off beats 300


class NaiveRankingMistakes(unittest.TestCase):
    def test_highest_advertised_vs_risk_adjusted(self):
        # A big unverified portal cashback vs a smaller official instant discount.
        big_cb = offer("bigcb", OfferKind.PORTAL_CASHBACK, discount_type=DiscountType.FLAT, value=rupees(900),
                       basis=Basis.PAYABLE, authenticity=Authenticity.UNVERIFIED, settlement_probability_bp=5_000,
                       stacks_with={"bank_instant": NO})
        r = optimize(product(), [big_cb, hdfc_10pct(stacks_with={"portal_cashback": NO})], user(HDFC_CC), NOW)
        self.assertEqual(ids(rank_by(r, "highest_advertised_discount")), {"bigcb"})
        self.assertEqual(ids(rank_by(r, "risk_adjusted")), {"hdfc10"})


class BasisBounds(unittest.TestCase):
    """The bounding readings must agree with brute-force enumeration of every basis reading."""

    def test_matches_brute_force(self):
        import itertools
        import random

        from couponhunter import optimizer as opt
        from couponhunter.verification import offer_confidence

        rng = random.Random(7)
        kinds = [OfferKind.PRODUCT_COUPON, OfferKind.PROMO_CODE, OfferKind.BANK_INSTANT, OfferKind.PORTAL_CASHBACK,
                 OfferKind.BANK_CASHBACK]
        bases = {OfferKind.PRODUCT_COUPON: [Basis.PRICE], OfferKind.PROMO_CODE: [Basis.PRICE, Basis.AFTER_COUPONS],
                 OfferKind.BANK_INSTANT: [Basis.PRICE, Basis.AFTER_COUPONS],
                 OfferKind.PORTAL_CASHBACK: [Basis.PRICE, Basis.PAYABLE], OfferKind.BANK_CASHBACK: [Basis.PRICE, Basis.PAYABLE]}
        checked = 0
        for trial in range(400):
            offers = []
            for k in kinds:
                if rng.random() < 0.8:
                    pct = rng.random() < 0.6
                    offers.append(offer(f"{k.value}{trial}", k, basis=Basis.UNKNOWN,
                                        discount_type=DiscountType.PERCENT if pct else DiscountType.FLAT,
                                        value=rng.choice([500, 1000, 2000]) if pct else rupees(rng.choice([200, 500, 900])),
                                        max_discount=rupees(rng.choice([500, 1500, 5000])) if pct else None,
                                        min_order=rupees(rng.choice([0, 8500, 9000, 9400, 9600])) or None,
                                        stacks_with={x.value: YES for x in kinds}))
            price = rupees(rng.choice([9_000, 10_000, 12_000]))
            prod = product(price // 100)
            r = optimize(prod, offers, user(), NOW)
            got = {s.offer_ids: s for s in r.scenarios}
            for n in range(len(offers) + 1):
                for combo in itertools.combinations(offers, n):
                    conf = {o.id: offer_confidence(o).bp for o in combo}
                    results = []
                    for choice in itertools.product(*(bases[o.kind] for o in combo)):
                        res = opt._evaluate(price, list(combo), {o.id: b for o, b in zip(combo, choice)}, None, conf)
                        results.append(res)
                    key = tuple(sorted(o.id for o in combo))
                    feasible = all(not isinstance(x, str) for x in results)
                    self.assertEqual(feasible, key in got, (trial, key))
                    if feasible:
                        self.assertEqual(got[key].checkout_payable, max(x[1] for x in results), (trial, key))
                        self.assertEqual(got[key].optimistic_payable, min(x[1] for x in results), (trial, key))
                        checked += 1
        self.assertGreater(checked, 1000)


class TrimTests(unittest.TestCase):
    def test_trim_preserves_best_confirmed_and_order(self):
        from couponhunter.service import KEEP_SCENARIOS, trim_scenarios

        many = [offer(f"c{i}", OfferKind.PRODUCT_COUPON, discount_type=DiscountType.FLAT, value=rupees(1000 + i),
                      basis=Basis.PRICE, account_specific=True) for i in range(3)]
        many += [offer(f"p{i}", OfferKind.PROMO_CODE, discount_type=DiscountType.FLAT, value=rupees(1000 + i),
                       basis=Basis.PRICE, account_specific=True) for i in range(3)]
        many += [cashback_5pct(account_specific=True), hdfc_10pct(stacks_with={})]
        r = optimize(product(), many, user(HDFC_CC, SBI_DC), NOW)
        self.assertGreater(len(r.scenarios), KEEP_SCENARIOS)
        before = r.best_confirmed
        self.assertNotIn(before, r.scenarios[:KEEP_SCENARIOS])  # the case that broke
        r.scenarios = trim_scenarios(r.scenarios, before)
        self.assertIs(r.best_confirmed, before)
        self.assertEqual(r.scenarios, sorted(r.scenarios, key=lambda s: (s.risk_adjusted_cost, s.cash_cost,
                                                                          len(s.applied), not s.rules_confirmed)))


class Bounds(unittest.TestCase):
    def test_per_kind_limit_keeps_weaker_offer_that_stacks(self):
        # Strongest coupon doesn't stack with the bank offer; a weaker one does.
        strong = offer("c900", OfferKind.PRODUCT_COUPON, discount_type=DiscountType.FLAT, value=rupees(900),
                       basis=Basis.PRICE, stacks_with={"bank_instant": NO})
        weak = coupon_500(stacks_with={"bank_instant": YES})
        fillers = [offer(f"f{i}", OfferKind.PRODUCT_COUPON, discount_type=DiscountType.FLAT, value=rupees(10 + i),
                         basis=Basis.PRICE, stacks_with={"bank_instant": NO}) for i in range(10)]
        bank = hdfc_10pct()  # silent on stacking; the coupons declare it
        r = optimize(product(), [strong, weak, bank, *fillers], user(HDFC_CC), NOW, per_kind_limit=2)
        self.assertEqual(ids(r.best_confirmed), {"coupon500", "hdfc10"})
        self.assertTrue(any("pruned" in n for n in r.notes))

    def test_steps_built_for_shown_scenarios(self):
        r = optimize(product(), [coupon_500()], user(), NOW)
        self.assertEqual(r.best_confirmed.steps, ["Tick the coupon checkbox on the product page"])

    def test_pruning_bounds_search(self):
        many = [
            offer(f"c{i}", OfferKind.PRODUCT_COUPON, discount_type=DiscountType.FLAT, value=rupees(i), basis=Basis.PRICE)
            for i in range(1, 40)
        ] + [
            offer(f"p{i}", OfferKind.PROMO_CODE, discount_type=DiscountType.FLAT, value=rupees(i), basis=Basis.PRICE)
            for i in range(1, 40)
        ]
        r = optimize(product(), many, user(), NOW, max_combinations=100, per_kind_limit=20)
        self.assertLessEqual(len(r.scenarios), 100)
        self.assertTrue(any("pruned" in n for n in r.notes))
        self.assertIn("c39", ids(r.best_possible))


if __name__ == "__main__":
    unittest.main()
