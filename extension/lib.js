/* CouponHunter extension: pure helpers shared by content script, service
 * worker and Node tests. No DOM access except in extractPage(doc), which
 * only reads text from the page the user is viewing. */
(function (root) {
  "use strict";

  const ASIN_RE = /^[A-Z0-9]{10}$/;
  const PATH_PATTERNS = [
    /\/dp\/([A-Za-z0-9]{10})(?:[/?#]|$)/,
    /\/gp\/product\/([A-Za-z0-9]{10})(?:[/?#]|$)/,
    /\/gp\/aw\/d\/([A-Za-z0-9]{10})(?:[/?#]|$)/,
  ];
  const AMAZON_HOSTS = new Set(["www.amazon.in", "amazon.in", "m.amazon.in"]);
  const REDEMPTION_HOSTS = new Set([
    "www.amazon.in", "amazon.in", "m.amazon.in", "www.hdfcbank.com", "www.icicibank.com",
    "www.sbicard.com", "www.axisbank.com", "www.kotak.com", "www.idfcfirstbank.com",
  ]);

  function asinFromUrl(href) {
    let u;
    try { u = new URL(href); } catch (e) { return null; }
    if (u.protocol !== "https:" || !AMAZON_HOSTS.has(u.hostname)) return null;
    for (const re of PATH_PATTERNS) {
      const m = u.pathname.match(re);
      if (m) {
        const a = m[1].toUpperCase();
        return ASIN_RE.test(a) ? a : null;
      }
    }
    return null;
  }

  /* "₹1,23,456.50" -> 12345650 paise. Returns null rather than guessing. */
  function parseInrToPaise(text) {
    if (typeof text !== "string") return null;
    const m = text.replace(/\s+/g, " ").match(/(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*)(?:\.([0-9]{1,2}))?/i);
    if (!m) return null;
    const whole = parseInt(m[1].replace(/,/g, ""), 10);
    if (!Number.isFinite(whole)) return null;
    const frac = m[2] ? parseInt((m[2] + "0").slice(0, 2), 10) : 0;
    const p = whole * 100 + frac;
    return p > 0 && p < 1e11 ? p : null;
  }

  function formatPaise(p) {
    if (typeof p !== "number" || !Number.isFinite(p)) return "—";
    const rupees = Math.floor(Math.abs(p) / 100);
    const paise = Math.abs(p) % 100;
    const s = rupees.toLocaleString("en-IN");
    return (p < 0 ? "-" : "") + "₹" + s + (paise ? "." + String(paise).padStart(2, "0") : "");
  }

  function isSafeRedemptionUrl(href) {
    let u;
    try { u = new URL(href); } catch (e) { return false; }
    return u.protocol === "https:" && !u.username && !u.password && !u.port && REDEMPTION_HOSTS.has(u.hostname);
  }

  /* Only loopback backends; anything else could exfiltrate what the user browses. */
  function normalizeBackendUrl(s) {
    let u;
    try { u = new URL(s); } catch (e) { return null; }
    if (u.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(u.hostname)) return null;
    if (u.pathname !== "/" || u.search || u.hash || u.username) return null;
    return u.origin;
  }

  const isInt = (v) => typeof v === "number" && Number.isInteger(v);
  const isStr = (v) => typeof v === "string";
  const optStr = (v) => v === null || v === undefined || isStr(v);

  function validScenario(s) {
    if (!s || typeof s !== "object") return false;
    for (const k of ["price", "instant_savings", "checkout_payable", "cash_cost", "expected_deferred",
                     "risk_adjusted_deferred", "effective_if_all_paid", "success_probability_bp"]) {
      if (!isInt(s[k])) return false;
    }
    if (typeof s.rules_confirmed !== "boolean") return false;
    if (!Array.isArray(s.assumptions) || !s.assumptions.every(isStr)) return false;
    if (!Array.isArray(s.steps) || !s.steps.every(isStr)) return false;
    if (!Array.isArray(s.applied)) return false;
    for (const a of s.applied) {
      if (!a || !isInt(a.offer_id) || !isStr(a.title) || !isInt(a.amount) || typeof a.deferred !== "boolean") return false;
      if (!optStr(a.code) || !optStr(a.redemption_url)) return false;
    }
    if (s.instrument !== null && (typeof s.instrument !== "object" || !isStr(s.instrument.label))) return false;
    return true;
  }

  /* Defends the panel against a buggy or mismatched backend: anything that
   * doesn't match the v1 contract is rejected instead of half-rendered. */
  function validateEvaluation(d) {
    if (!d || typeof d !== "object" || !isStr(d.asin) || !ASIN_RE.test(d.asin)) return { ok: false, why: "bad asin" };
    if (typeof d.has_price !== "boolean") return { ok: false, why: "missing has_price" };
    if (!d.has_price) return { ok: true };
    if (!isInt(d.price)) return { ok: false, why: "bad price" };
    if (d.best_confirmed !== null && !validScenario(d.best_confirmed)) return { ok: false, why: "bad best_confirmed" };
    if (!validScenario(d.best_possible)) return { ok: false, why: "bad best_possible" };
    if (!Array.isArray(d.excluded) || !Array.isArray(d.offers) || !Array.isArray(d.notes)) return { ok: false, why: "bad lists" };
    return { ok: true };
  }

  const PRICE_SELECTORS = [
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    ".priceToPay .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
  ];
  const LIST_PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    ".basisPrice .a-offscreen",
    ".a-price.a-text-price[data-a-strike='true'] .a-offscreen",
  ];
  const OFFER_SELECTORS = [
    "#vsxoffers_feature_div .offers-items-content",
    "#ppd .a-carousel-card .offers-items-content",
    "#itembox-InstantBankDiscount .a-truncate-full",
    "#itembox-Partner .a-truncate-full",
  ];
  const COUPON_SELECTORS = ["#couponText", "#promoPriceBlockMessage_feature_div label", "[id^='couponText']"];
  const DEAL_SELECTORS = ["#dealBadge_feature_div", "#dealBadgeSupportingText"];

  function textOf(el) {
    return el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "";
  }

  function firstText(doc, sels) {
    for (const s of sels) {
      const t = textOf(doc.querySelector(s));
      if (t) return t;
    }
    return "";
  }

  /* Reads only product facts that Amazon displays to this user on this page.
   * Never reads forms, cookies, account widgets or anything outside the
   * selectors below. Returns warnings instead of throwing when the page
   * layout has changed. */
  function extractPage(doc, href) {
    const warnings = [];
    const asin = asinFromUrl(href);
    const title = firstText(doc, ["#productTitle"]).slice(0, 300);
    const priceText = firstText(doc, PRICE_SELECTORS);
    const price = parseInrToPaise(priceText);
    if (!price) warnings.push("price not found on page (layout may have changed)");
    const listPrice = parseInrToPaise(firstText(doc, LIST_PRICE_SELECTORS));
    const seller = firstText(doc, ["#sellerProfileTriggerId", "#merchantInfoFeature_feature_div .offer-display-feature-text-message"]).slice(0, 120) || null;
    const offers = [];
    const seen = new Set();
    for (const s of OFFER_SELECTORS) {
      doc.querySelectorAll(s).forEach((el) => {
        const t = textOf(el).slice(0, 600);
        if (t && !seen.has(t)) { seen.add(t); offers.push(t); }
      });
    }
    for (const s of COUPON_SELECTORS) {
      const t = textOf(doc.querySelector(s));
      if (t && /coupon/i.test(t) && !seen.has(t)) { seen.add(t); offers.push(t.slice(0, 300)); }
    }
    const deal = firstText(doc, DEAL_SELECTORS);
    if (deal && /deal/i.test(deal)) offers.push({ kind: "deal_price", title: deal.slice(0, 120) });
    const avail = firstText(doc, ["#availability"]).toLowerCase();
    const inStock = avail ? !/unavailable|out of stock/.test(avail) : null;
    return { asin, title, price, list_price: listPrice || null, seller, offers: offers.slice(0, 30), in_stock: inStock, warnings };
  }

  const api = { asinFromUrl, parseInrToPaise, formatPaise, isSafeRedemptionUrl, normalizeBackendUrl, validateEvaluation, extractPage };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CHLib = api;
})(typeof self !== "undefined" ? self : this);
