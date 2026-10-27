// Run: node --test tests/extension/lib.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const L = require("../../extension/lib.js");

test("asinFromUrl accepts product URLs only on amazon.in over https", () => {
  assert.strictEqual(L.asinFromUrl("https://www.amazon.in/Sony/dp/B09XS7JWHH/ref=x?th=1"), "B09XS7JWHH");
  assert.strictEqual(L.asinFromUrl("https://www.amazon.in/gp/product/b09xs7jwhh"), "B09XS7JWHH");
  for (const u of ["http://www.amazon.in/dp/B09XS7JWHH", "https://www.amazon.com/dp/B09XS7JWHH",
                   "https://www.amazon.in.evil.example/dp/B09XS7JWHH", "https://www.amazon.in/s?k=x", "not a url"]) {
    assert.strictEqual(L.asinFromUrl(u), null, u);
  }
});

test("parseInrToPaise", () => {
  assert.strictEqual(L.parseInrToPaise("₹1,23,456.5"), 12345650);
  assert.strictEqual(L.parseInrToPaise("₹ 499"), 49900);
  assert.strictEqual(L.parseInrToPaise("Currently unavailable"), null);
  assert.strictEqual(L.parseInrToPaise(undefined), null);
});

test("formatPaise uses Indian grouping", () => {
  assert.strictEqual(L.formatPaise(12345650), "₹1,23,456.50");
  assert.strictEqual(L.formatPaise(null), "—");
});

test("redemption links: https + allowlisted host only", () => {
  assert.ok(L.isSafeRedemptionUrl("https://www.amazon.in/promo"));
  for (const u of ["http://www.amazon.in/x", "https://amazon-deals.example/", "javascript:alert(1)",
                   "https://www.amazon.in@evil.example/", "https://www.amazon.in:444/"]) {
    assert.ok(!L.isSafeRedemptionUrl(u), u);
  }
});

test("backend URL must be loopback", () => {
  assert.strictEqual(L.normalizeBackendUrl("http://127.0.0.1:8765"), "http://127.0.0.1:8765");
  assert.strictEqual(L.normalizeBackendUrl("http://localhost:9000/"), "http://localhost:9000");
  for (const u of ["https://127.0.0.1:8765", "http://192.168.1.5:8765", "http://evil.example", "http://127.0.0.1:8765/x"]) {
    assert.strictEqual(L.normalizeBackendUrl(u), null, u);
  }
});

const scenario = () => ({ price: 100, instant_savings: 0, checkout_payable: 100, cash_cost: 100, expected_deferred: 0,
  risk_adjusted_deferred: 0, effective_if_all_paid: 100, success_probability_bp: 10000, rules_confirmed: true,
  assumptions: [], steps: [], applied: [], instrument: null });

test("validateEvaluation rejects malformed API data", () => {
  const good = { asin: "B09XS7JWHH", has_price: true, price: 100, best_confirmed: scenario(), best_possible: scenario(),
                 excluded: [], offers: [], notes: [] };
  assert.ok(L.validateEvaluation(good).ok);
  assert.ok(L.validateEvaluation({ asin: "B09XS7JWHH", has_price: false }).ok);
  const bad = [null, "x", { asin: "<b>", has_price: true }, { ...good, price: "100" },
    { ...good, best_possible: { ...scenario(), cash_cost: 1.5 } },
    { ...good, best_confirmed: { ...scenario(), applied: [{ offer_id: 1, title: {}, amount: 1, deferred: false }] } },
    { ...good, best_possible: { ...scenario(), steps: [{ html: "<img>" }] } }, { ...good, offers: null }];
  for (const b of bad) assert.ok(!L.validateEvaluation(b).ok, JSON.stringify(b));
});

// Minimal DOM stand-in: enough for extractPage's querySelector/querySelectorAll use.
function fakeDoc(map) {
  const node = (t) => ({ textContent: t });
  return {
    querySelector: (s) => (map[s] ? node([].concat(map[s])[0]) : null),
    querySelectorAll: (s) => ({ forEach: (f) => [].concat(map[s] || []).forEach((t) => f(node(t))) }),
  };
}

test("extractPage reads price/offers and warns when the layout changed", () => {
  const doc = fakeDoc({
    "#productTitle": "  Sony WH-1000XM5  ",
    ".priceToPay .a-offscreen": "₹26,990.00",
    ".basisPrice .a-offscreen": "₹34,990",
    "#vsxoffers_feature_div .offers-items-content": ["10% Instant Discount up to INR 1250 on HDFC Bank Credit Card", "10% Instant Discount up to INR 1250 on HDFC Bank Credit Card"],
    "#couponText": "Apply ₹500 coupon",
    "#dealBadge_feature_div": "Limited time deal",
    "#availability": "In stock",
  });
  const p = L.extractPage(doc, "https://www.amazon.in/dp/B09XS7JWHH");
  assert.strictEqual(p.price, 2699000);
  assert.strictEqual(p.list_price, 3499000);
  assert.strictEqual(p.offers.length, 3); // deduped bank offer + coupon + deal
  assert.deepStrictEqual(p.offers[2], { kind: "deal_price", title: "Limited time deal" });
  assert.strictEqual(p.in_stock, true);
  const broken = L.extractPage(fakeDoc({}), "https://www.amazon.in/dp/B09XS7JWHH");
  assert.strictEqual(broken.price, null);
  assert.ok(broken.warnings[0].includes("layout"));
});

test("extractListingPrices reads only watched tiles and skips reference prices", () => {
  const tile = (asin, price, strike) => ({
    getAttribute: () => asin,
    querySelector: (sel) => (sel.includes(":not(.a-text-price)") && price ? { textContent: price } : null),
    strike,
  });
  const doc = { querySelectorAll: (s) => (s === "[data-asin]" ? [
    tile("B0WATCHED1", "₹1,299"), tile("B0OTHER001", "₹99"), tile("B0WATCHED1", "₹1,000"),
    tile("B0WATCHED2", null), tile("", "₹5"),
  ] : []) };
  const out = L.extractListingPrices(doc, new Set(["B0WATCHED1", "B0WATCHED2"]));
  assert.deepStrictEqual(out, [{ asin: "B0WATCHED1", price: 129900 }]);
});

test("isAmazonPage", () => {
  assert.ok(L.isAmazonPage("https://www.amazon.in/s?k=x"));
  assert.ok(!L.isAmazonPage("https://www.amazon.in.evil.example/"));
  assert.ok(!L.isAmazonPage("http://www.amazon.in/"));
});

test("offers_area_seen distinguishes 'no offers' from 'not loaded'", () => {
  const withArea = { querySelector: (s) => (s === "#vsxoffers_feature_div" ? { textContent: "" } : null),
                     querySelectorAll: () => ({ forEach: () => {} }) };
  assert.strictEqual(L.extractPage(withArea, "https://www.amazon.in/dp/B09XS7JWHH").offers_area_seen, true);
  const without = { querySelector: () => null, querySelectorAll: () => ({ forEach: () => {} }) };
  assert.strictEqual(L.extractPage(without, "https://www.amazon.in/dp/B09XS7JWHH").offers_area_seen, false);
});

test("owner-approved extra redemption domains", () => {
  assert.ok(!L.isSafeRedemptionUrl("https://www.examplebank.com/offer"));
  assert.ok(L.isSafeRedemptionUrl("https://www.examplebank.com/offer", ["www.examplebank.com"]));
  assert.ok(!L.isSafeRedemptionUrl("http://www.examplebank.com/offer", ["www.examplebank.com"]));
  assert.ok(!L.isSafeRedemptionUrl("https://evil.example/", [{ toString: () => "evil.example" }]));
});
