/* Product-page panel. Runs only on amazon.in product URLs (see manifest).
 * Renders inside a closed Shadow DOM using textContent only, so neither the
 * page nor backend data can inject markup. Never touches cart, checkout,
 * payment or account elements. */
(function () {
  "use strict";
  const L = self.CHLib;
  const asin = L.asinFromUrl(location.href);
  if (!asin || document.getElementById("couponhunter-host")) return;

  const host = document.createElement("div");
  host.id = "couponhunter-host";
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483646;";
  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = `
    .p{font:13px/1.45 system-ui,"Segoe UI",sans-serif;width:340px;max-height:70vh;overflow:auto;background:#fff;color:#1d1d1b;
       border:1px solid #d5d5cf;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);padding:12px}
    .h{display:flex;justify-content:space-between;align-items:center;font-weight:600;margin-bottom:6px}
    .big{font-size:20px;font-weight:700}.m{color:#6b6b66}.w{color:#9a5b00}.e{color:#a12a2a}
    button{font:inherit;border:1px solid #1f6f50;background:#1f6f50;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer;margin:2px 4px 2px 0}
    button.s{background:#fff;color:#1f6f50}.o{border-top:1px solid #eee;padding:6px 0}.min{width:auto;padding:6px 10px;cursor:pointer}.ok{color:#1f6f50;font-weight:600}
    ol,ul{margin:4px 0;padding-left:18px}.syn{color:#5b3fa0;font-weight:600}`;
  root.appendChild(style);
  const panel = document.createElement("div");
  panel.className = "p";
  root.appendChild(panel);
  document.documentElement.appendChild(host);

  const el = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  };
  const btn = (text, onClick, secondary) => {
    const b = el("button", text, secondary ? "s" : "");
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  };
  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { ok: false, error: chrome.runtime.lastError?.message })); }
    catch (e) { res({ ok: false, error: "extension reloaded; refresh the page" }); }
  });
  let collapsed = false;

  function header(extra) {
    const h = el("div", null, "h");
    h.appendChild(el("span", "CouponHunter"));
    const right = el("span");
    if (extra) right.appendChild(extra);
    right.appendChild(btn(collapsed ? "Show" : "Hide", () => { collapsed = !collapsed; refresh(); }, true));
    h.appendChild(right);
    return h;
  }

  function render(state) {
    panel.replaceChildren();
    if (collapsed) {
      panel.className = "p min";
      panel.appendChild(header());
      return;
    }
    panel.className = "p";
    panel.appendChild(header());
    if (state.error) {
      panel.appendChild(el("div", state.error, "e"));
      return;
    }
    const d = state.data;
    const page = L.extractPage(document, location.href);
    const actions = el("div");
    actions.appendChild(btn("Save price & offers from this page", () => capture(page, false)));
    if (!d || !d.tracked) actions.appendChild(btn("Track", () => capture(page, true), true));
    if (!d || !d.has_price) {
      panel.appendChild(el("div", d && d.message ? d.message : "Not tracked yet.", "m"));
      panel.appendChild(actions);
      page.warnings.forEach((w) => panel.appendChild(el("div", w, "w")));
      return;
    }
    const best = d.best_confirmed || d.baseline;
    if (d.offers.some((o) => o.is_synthetic)) panel.appendChild(el("div", "Includes SYNTHETIC demo offers", "syn"));
    const adv = d.advice;
    if (adv && typeof adv.headline === "string" && ["buy_now", "wait", "fair"].includes(adv.action)) {
      const label = { buy_now: "Buy now", wait: "Wait", fair: "Fair price" }[adv.action];
      panel.appendChild(el("div", `${label}: ${adv.headline}`, adv.action === "wait" ? "w" : adv.action === "buy_now" ? "ok" : "m"));
    }
    panel.appendChild(el("div", "Lowest realistic price", "m"));
    panel.appendChild(el("div", L.formatPaise(best.cash_cost), "big"));
    const facts = [];
    if (best.instant_savings + best.funding_savings > 0) facts.push(`${L.formatPaise(best.instant_savings + best.funding_savings)} off at checkout`);
    if (best.instrument) facts.push(`pay with ${best.instrument.label}`);
    facts.push(`${Math.floor(best.success_probability_bp / 100)}% chance all offers apply`);
    panel.appendChild(el("div", facts.join(" · ")));
    if (d.price_is_stale) panel.appendChild(el("div", "Stored price is old — press Save to refresh.", "w"));
    if (page.price && page.price !== d.price) panel.appendChild(el("div", `Page shows ${L.formatPaise(page.price)}; stored ${L.formatPaise(d.price)}. Press Save to update.`, "w"));
    if (best.expected_deferred > 0) {
      panel.appendChild(el("div", `+ up to ${L.formatPaise(best.expected_deferred)} cashback later (not guaranteed)`, "m"));
    }
    if (best.steps.length) {
      const ol = el("ol");
      best.steps.forEach((s) => ol.appendChild(el("li", s)));
      panel.appendChild(ol);
    }
    const poss = d.best_possible;
    if (poss && poss.cash_cost < best.cash_cost) {
      panel.appendChild(el("div", `Possibly ${L.formatPaise(poss.cash_cost)} if:`, "w"));
      const ul = el("ul");
      poss.assumptions.slice(0, 3).forEach((a) => ul.appendChild(el("li", a)));
      panel.appendChild(ul);
    }
    best.applied.forEach((a) => {
      const row = el("div", null, "o");
      row.appendChild(el("div", `${a.title} — ${L.formatPaise(a.amount)}${a.deferred ? " later" : ""}`));
      if (a.code) row.appendChild(btn(`Copy code ${a.code}`, () => navigator.clipboard.writeText(a.code)));
      if (a.redemption_url && L.isSafeRedemptionUrl(a.redemption_url)) {
        row.appendChild(btn("Open offer page", () => window.open(a.redemption_url, "_blank", "noopener,noreferrer"), true));
      }
      const key = `${asin}-${a.offer_id}-${Date.now()}`;
      row.appendChild(btn("Applied ✓", () => feedback(a.offer_id, "applied", key), true));
      row.appendChild(btn("Didn't apply ✗", () => feedback(a.offer_id, "failed", key), true));
      panel.appendChild(row);
    });
    if (d.excluded.length) panel.appendChild(el("div", `${d.excluded.length} offer(s) don't apply to you — see dashboard`, "m"));
    panel.appendChild(actions);
    if (state.note) panel.appendChild(el("div", state.note, "m"));
  }

  async function capture(page, watch) {
    if (!page.price) {
      render({ error: "Couldn't read the price on this page. Enter it in the dashboard instead." });
      return;
    }
    const r = await send({ type: "capture", payload: { ...page, watch } });
    if (!r.ok) return render({ error: r.error });
    const c = r.data.capture || {};
    const check = L.validateEvaluation(r.data);
    if (!check.ok) return render({ error: `Unexpected backend response (${check.why}). Update the extension/backend.` });
    render({ data: { ...r.data, tracked: r.data.tracked || watch }, note: `Saved: ${c.offers_created || 0} new offer(s), ${c.offers_unparsed || 0} not understood.` });
  }

  async function feedback(offerId, outcome, key) {
    const r = await send({ type: "redeem", offer_id: offerId, outcome, key });
    render({ data: last, note: r.ok ? "Thanks — recorded." : r.error });
  }

  let last = null;
  async function refresh() {
    if (collapsed) return render({});
    const r = await send({ type: "evaluate", asin });
    if (!r.ok && r.status === 404) { last = { asin, has_price: false, tracked: false }; return render({ data: last }); }
    if (!r.ok) return render({ error: r.error });
    const check = L.validateEvaluation(r.data);
    if (!check.ok) return render({ error: `Unexpected backend response (${check.why}). Update the extension/backend.` });
    last = r.data;
    render({ data: last });
  }

  (async () => {
    const s = await send({ type: "settings" });
    if (s.ok && s.data.autoCapture) {
      const page = L.extractPage(document, location.href);
      if (page.price) return capture(page, false);
    }
    refresh();
  })();
})();
