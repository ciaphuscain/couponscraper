"""URL validation, normalisation, text parsing, RSS and HTTP-client safety."""

import io
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from couponhunter import rules
from couponhunter.amazon import InvalidAmazonURL, canonical_product_url, extract_asin
from couponhunter.domain import Basis, DiscountType, OfferKind
from couponhunter.money import fmt, parse_inr, percent_of, rupees
from couponhunter.normalize import NormalizationError, normalize_record, parse_offer_text, safe_redemption_url
from couponhunter.pricing import Observation, compute_stats, judge_price
from couponhunter.sources import SourceError
from couponhunter.sources.http import HttpClient
from couponhunter.sources.rss import RssAdapter


class MoneyTests(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_inr("₹1,23,456.5"), 12_345_650)
        self.assertEqual(parse_inr("Rs. 500"), 50_000)
        self.assertEqual(fmt(12_345_650), "₹1,23,456.50")
        self.assertEqual(fmt(100_000_00), "₹1,00,000")
        self.assertEqual(percent_of(999, 1_000), 99)  # floors
        with self.assertRaises(ValueError):
            parse_inr("free")
        with self.assertRaises(TypeError):
            rupees(1.5)


class AmazonUrlTests(unittest.TestCase):
    def test_valid_forms(self):
        for u in ["https://www.amazon.in/Sony-WH-1000XM5/dp/B09XS7JWHH/ref=sr_1_1?keywords=x",
                  "https://amazon.in/dp/b09xs7jwhh", "https://www.amazon.in/gp/product/B09XS7JWHH",
                  "http://m.amazon.in/gp/aw/d/B09XS7JWHH", "B09XS7JWHH"]:
            self.assertEqual(extract_asin(u), "B09XS7JWHH", u)

    def test_rejects(self):
        for u in ["https://www.amazon.com/dp/B09XS7JWHH", "https://amazon.in.evil.example/dp/B09XS7JWHH",
                  "https://user:pw@www.amazon.in/dp/B09XS7JWHH", "https://www.amazon.in:8443/dp/B09XS7JWHH",
                  "javascript:alert(1)//www.amazon.in/dp/B09XS7JWHH", "https://amzn.in/d/abc",
                  "https://www.amazon.in/s?k=headphones", "file:///etc/passwd", "", "x" * 3000,
                  "http://169.254.169.254/dp/B09XS7JWHH"]:
            with self.assertRaises(InvalidAmazonURL, msg=u):
                extract_asin(u)

    def test_canonical_url_strips_tracking(self):
        self.assertEqual(canonical_product_url("b09xs7jwhh"), "https://www.amazon.in/dp/B09XS7JWHH")


class NormalizeTests(unittest.TestCase):
    def test_redemption_url_allowlist(self):
        self.assertEqual(safe_redemption_url("https://www.amazon.in/promo"), "https://www.amazon.in/promo")
        for bad in ["http://www.amazon.in/promo", "https://amazon-offers.example/promo", "javascript:alert(1)",
                    "https://www.amazon.in@evil.example/", "data:text/html,<script>"]:
            self.assertIsNone(safe_redemption_url(bad), bad)

    def test_malicious_record_fields(self):
        rec = {"kind": "promo_code", "title": "<script>alert(1)</script>\x00", "discount": "₹100",
               "code": "SAVE100", "redemption_url": "https://evil.example/x"}
        n = normalize_record(rec)
        self.assertNotIn("\x00", n.title)
        self.assertIsNone(n.redemption_url)
        self.assertTrue(any("dropped" in w for w in n.warnings))
        with self.assertRaises(NormalizationError):
            normalize_record({**rec, "code": "<img src=x>"})

    def test_rejects(self):
        bad = [
            {"kind": "bank_instant", "title": "x", "discount": "10%"},  # no payment restriction
            {"kind": "product_coupon", "title": "x", "discount": "150%"},
            {"kind": "product_coupon", "title": "x", "discount": "₹5", "asins": ["NOTANASIN!"]},
            {"kind": "product_coupon", "title": "x", "discount": "₹5", "valid_until": "2026-01-01T00:00:00"},
            {"kind": "product_coupon", "title": "x", "discount": "₹5", "eligibility": {"field": 1}},
            {"kind": "product_coupon", "title": "x", "discount": "₹5", "valid_from": "2026-02-01",
             "valid_until": "2026-01-01"},
        ]
        for r in bad:
            with self.assertRaises(NormalizationError, msg=r):
                normalize_record(r)

    def test_date_only_expiry_is_end_of_day_ist(self):
        n = normalize_record({"kind": "product_coupon", "title": "x", "discount": "₹5", "valid_until": "2026-11-01"})
        self.assertEqual(n.valid_until, datetime(2026, 11, 1, 18, 29, 59, tzinfo=timezone.utc))

    def test_fingerprint_ignores_wording(self):
        a = normalize_record({"kind": "product_coupon", "title": "Save ₹500", "discount": "₹500", "asins": ["B0TESTASIN"]})
        b = normalize_record({"kind": "product_coupon", "title": "Coupon: 500 off", "discount": "500",
                              "asins": ["b0testasin"]})
        c = normalize_record({"kind": "product_coupon", "title": "Save ₹500", "discount": "₹500", "asins": ["B0OTHERVAR"]})
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), c.fingerprint())


class TextParserTests(unittest.TestCase):
    CASES = [
        ("10% Instant Discount up to INR 1250 on HDFC Bank Credit Card Non-EMI Trxn. Minimum purchase value INR 5000",
         OfferKind.BANK_INSTANT, DiscountType.PERCENT, 1_000, rupees(1250), rupees(5000)),
        ("Flat INR 500 Instant Discount on ICICI Bank Credit Cards (excluding Amazon Pay ICICI Credit Card). "
         "Minimum purchase value INR 7500", OfferKind.BANK_INSTANT, DiscountType.FLAT, rupees(500), None, rupees(7500)),
        ("Save ₹500 with coupon", OfferKind.PRODUCT_COUPON, DiscountType.FLAT, rupees(500), None, None),
        ("Apply 5% coupon", OfferKind.PRODUCT_COUPON, DiscountType.PERCENT, 500, None, None),
        ("Use code SAVE200 to get ₹200 off on orders above ₹1999", OfferKind.PROMO_CODE, DiscountType.FLAT,
         rupees(200), None, rupees(1999)),
        ("Upto ₹300 cashback as Amazon Pay Balance when you pay with Amazon Pay UPI", OfferKind.AMAZON_PAY_CASHBACK,
         DiscountType.FLAT, rupees(300), None, None),
    ]

    def test_cases(self):
        for text, kind, dtype, value, cap, mn in self.CASES:
            n = normalize_record({"text": text})
            self.assertEqual((n.kind, n.discount_type, n.value, n.max_discount, n.min_order),
                             (kind, dtype, value, cap, mn), text)
            self.assertEqual(n.basis, Basis.UNKNOWN)  # the parser never invents a basis

    def test_exclusions_lower_confidence(self):
        p = parse_offer_text(self.CASES[1][0])
        self.assertLess(p.confidence_bp, 10_000)
        self.assertTrue(any("exclusions" in w for w in p.warnings))

    def test_targeted_and_new_customer(self):
        n = normalize_record({"text": "Flat ₹100 off on first order for selected customers. Use code NEW100"})
        self.assertTrue(n.new_customer_only and n.account_specific)

    def test_unparseable(self):
        for t in ["Great deals today!", "Bank offer 10%"]:
            with self.assertRaises(NormalizationError, msg=t):
                normalize_record({"text": t})


class RuleTests(unittest.TestCase):
    def test_three_valued(self):
        r = {"all": [{"field": "product.category", "op": "eq", "value": "Electronics"},
                     {"field": "user.is_prime", "op": "eq", "value": True}]}
        self.assertTrue(rules.evaluate(r, {"product": {"category": "electronics"}, "user": {"is_prime": True}}))
        self.assertIsNone(rules.evaluate(r, {"product": {"category": "electronics"}, "user": {}}))
        self.assertFalse(rules.evaluate(r, {"product": {"category": "books"}, "user": {}}))
        self.assertIsNone(rules.evaluate({"not": {"field": "x", "op": "eq", "value": 1}}, {}))
        with self.assertRaises(rules.RuleError):
            rules.validate({"all": []})
        deep = {"field": "a", "op": "eq", "value": 1}
        for _ in range(20):
            deep = {"not": deep}
        with self.assertRaises(rules.RuleError):
            rules.validate(deep)


class PricingTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def obs(self, prices, days_apart=2):
        start = self.NOW - timedelta(days=days_apart * (len(prices) - 1))
        return [Observation(p, start + timedelta(days=i * days_apart)) for i, p in enumerate(prices)]

    def test_insufficient_history(self):
        v = judge_price(compute_stats(self.obs([100]), self.NOW), 90, list_price=200)
        self.assertFalse(v.is_good_deal)
        self.assertTrue(any("too little history" in c for c in v.caveats))

    def test_inflated_reference_price(self):
        s = compute_stats(self.obs([1000] * 10), self.NOW)
        v = judge_price(s, 1000, list_price=2000)  # "50% off!"
        self.assertFalse(v.is_good_deal)
        self.assertTrue(any("inflated" in c for c in v.caveats))

    def test_genuine_drop(self):
        s = compute_stats(self.obs([1000] * 10), self.NOW)
        v = judge_price(s, 900)
        self.assertTrue(v.is_good_deal)
        self.assertTrue(any("lowest in the 18 days" in r for r in v.reasons))

    def test_short_coverage_no_lowest_claim(self):
        s = compute_stats(self.obs([1000] * 6, days_apart=1), self.NOW)
        v = judge_price(s, 900)
        self.assertTrue(v.is_good_deal)
        self.assertFalse(any("lowest" in r for r in v.reasons))
        self.assertTrue(any("covers only" in c for c in v.caveats))

    def test_new_low_within_noise_is_not_a_deal(self):
        s = compute_stats(self.obs([1000, 1030, 970, 1010, 990, 1020, 980] * 3), self.NOW)
        v = judge_price(s, 970)
        self.assertFalse(v.is_good_deal)
        self.assertTrue(any("within normal price variation" in c for c in v.caveats))

    def test_pre_sale_inflation(self):
        s = compute_stats(self.obs([1000, 1000, 1000, 1000, 1300, 1300, 1050], days_apart=2), self.NOW)
        self.assertTrue(s.raised_recently)


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def open(self, req, timeout):
        self.urls.append(req.full_url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(r)


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://feeds.example/", code, "x", headers or {}, io.BytesIO(b""))


class HttpClientTests(unittest.TestCase):
    def client(self, responses, **kw):
        op = FakeOpener(responses)
        return HttpClient("feeds.example", sleep=lambda s: None, opener_factory=lambda h: op, **kw), op

    def test_host_pinning(self):
        c, _ = self.client([])
        for u in ["https://other.example/feed", "http://feeds.example/feed", "https://feeds.example:444/x"]:
            with self.assertRaises(SourceError):
                c.get(u)

    def test_robots_disallow(self):
        c, _ = self.client([b"User-agent: *\nDisallow: /\n"])
        with self.assertRaises(SourceError) as cm:
            c.get("https://feeds.example/feed")
        self.assertEqual(cm.exception.kind, "auth")

    def test_bounded_retry_then_fail(self):
        c, op = self.client([http_error(404), http_error(503), http_error(503), http_error(503), b"never"], retries=2)
        with self.assertRaises(SourceError):
            c.get("https://feeds.example/feed")
        self.assertEqual(len(op.urls), 4)  # robots + 3 attempts

    def test_403_not_retried(self):
        c, op = self.client([http_error(404), http_error(403)])
        with self.assertRaises(SourceError) as cm:
            c.get("https://feeds.example/feed")
        self.assertEqual((cm.exception.kind, len(op.urls)), ("auth", 2))

    def test_429_retry_after(self):
        c, _ = self.client([http_error(404), http_error(429, {"Retry-After": "120"})])
        with self.assertRaises(SourceError) as cm:
            c.get("https://feeds.example/feed")
        self.assertEqual((cm.exception.kind, cm.exception.retry_after_s), ("rate_limited", 120))


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Deals</title>
<item><title>Save &#8377;500 with coupon on Sony headphones</title>
<link>https://www.amazon.in/dp/B09XS7JWHH?tag=someaffiliate-21</link><description>Limited time</description></item>
<item><title>10% Instant Discount up to INR 1250 on HDFC Bank Credit Card</title><link>https://deals.example/p/1</link></item>
<item><title>Amazing stuff you will love</title><link>https://deals.example/p/2</link></item>
</channel></rss>"""


class RssTests(unittest.TestCase):
    def test_parse_feed(self):
        r = RssAdapter().parse(RSS, "https://deals.example/feed")
        self.assertEqual(len(r.records), 3)
        self.assertEqual(r.records[0].data["asins"], ["B09XS7JWHH"])
        n = normalize_record(r.records[0].data)
        self.assertEqual(n.value, rupees(500))
        self.assertIsNone(n.redemption_url)  # affiliate-tagged feed links are never used as redemption links

    def test_refuses_dtd(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        with self.assertRaises(SourceError):
            RssAdapter().parse(bomb, "https://x.example/")

    def test_malformed_and_not_a_feed(self):
        for body in [b"<html><body>hello", b"<html><body>hi</body></html>", b"\x00\x01"]:
            with self.assertRaises(SourceError):
                RssAdapter().parse(body, "https://x.example/")


if __name__ == "__main__":
    unittest.main()
