/* Service worker: the only part of the extension that talks to the backend.
 * Content scripts ask it via messages; it checks who is asking and what they
 * ask for before forwarding a fixed set of operations. */
importScripts("lib.js");

const DEFAULTS = { backendUrl: "http://127.0.0.1:8765", token: "", autoCapture: false, observeWatched: false };
const WATCHED_TTL_MS = 10 * 60 * 1000;
let watchedCache = { at: 0, data: null };
const TIMEOUT_MS = 4000;

async function settings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...s };
}

async function call(method, path, body) {
  const s = await settings();
  const base = CHLib.normalizeBackendUrl(s.backendUrl);
  if (!base) return { ok: false, error: "Backend URL must be http://127.0.0.1:<port>" };
  if (!s.token) return { ok: false, error: "Set your API token in the extension options" };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(base + path, {
      method,
      headers: { "X-CouponHunter-Token": s.token, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      credentials: "omit",
      cache: "no-store",
    });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    return { ok: resp.ok, status: resp.status, data, error: resp.ok ? null : (data && data.error) || `HTTP ${resp.status}` };
  } catch (e) {
    return { ok: false, status: 0, error: "CouponHunter backend not reachable. Is `couponhunter serve` running?" };
  } finally {
    clearTimeout(timer);
  }
}

function fromAmazonProductPage(sender) {
  return sender.id === chrome.runtime.id && sender.tab && typeof sender.url === "string" && CHLib.asinFromUrl(sender.url);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const pageAsin = fromAmazonProductPage(sender);
  const fromAmazon = sender.id === chrome.runtime.id && sender.tab && typeof sender.url === "string" &&
    CHLib.isAmazonPage(sender.url);
  // Listing pages (no ASIN in URL) may only use the passive-observation operations.
  if (msg && fromAmazon && !pageAsin && ["settings", "watched", "observe"].includes(msg.type)) {
    handleListing(msg).then(reply, () => reply({ ok: false, error: "internal error" }));
    return true;
  }
  // Popup/options pages are extension pages (options opens in a tab, so check the URL, not sender.tab).
  const fromPopup = sender.id === chrome.runtime.id && typeof sender.url === "string" &&
    sender.url.startsWith(chrome.runtime.getURL(""));
  // Product-page content scripts may use every operation for *their own* ASIN;
  // the popup may only ask for backend status.
  if (!msg || typeof msg.type !== "string" || (!pageAsin && !(fromPopup && msg.type === "status"))) {
    reply({ ok: false, error: "rejected" });
    return false;
  }
  const handle = async () => {
    switch (msg.type) {
      case "status":
        return call("GET", "/api/v1/status");
      case "evaluate":
        if (msg.asin !== pageAsin) return { ok: false, error: "asin mismatch" };
        return call("GET", `/api/v1/products/${pageAsin}`);
      case "capture": {
        const p = msg.payload || {};
        if (p.asin !== pageAsin || !Number.isInteger(p.price)) return { ok: false, error: "invalid capture" };
        return call("POST", "/api/v1/captures", {
          asin: pageAsin, title: String(p.title || "").slice(0, 300), price: p.price,
          list_price: Number.isInteger(p.list_price) ? p.list_price : null, seller: p.seller || null,
          in_stock: typeof p.in_stock === "boolean" ? p.in_stock : null,
          offers: Array.isArray(p.offers) ? p.offers.slice(0, 30) : [], watch: !!p.watch,
          offers_area_seen: p.offers_area_seen === true,
        });
      }
      case "track":
        return call("POST", "/api/v1/products", { asin: pageAsin, title: String(msg.title || "").slice(0, 300) });
      case "redeem":
        if (!Number.isInteger(msg.offer_id) || !["applied", "failed", "not_shown"].includes(msg.outcome)) {
          return { ok: false, error: "invalid feedback" };
        }
        return call("POST", "/api/v1/redemptions", {
          offer_id: msg.offer_id, asin: pageAsin, outcome: msg.outcome, channel: "extension",
          idempotency_key: String(msg.key || "").slice(0, 64),
        });
      case "settings": {
        const st = await settings();
        return { ok: true, data: { autoCapture: st.autoCapture, observeWatched: st.observeWatched } };
      }
      default:
        return { ok: false, error: "unknown request" };
    }
  };
  handle().then(reply, () => reply({ ok: false, error: "internal error" }));
  return true; // async reply
});

async function handleListing(msg) {
  const st = await settings();
  if (msg.type === "settings") return { ok: true, data: { autoCapture: false, observeWatched: st.observeWatched } };
  if (!st.observeWatched) return { ok: false, error: "disabled" };
  if (msg.type === "watched") {
    if (watchedCache.data && Date.now() - watchedCache.at < WATCHED_TTL_MS) return { ok: true, data: watchedCache.data };
    const r = await call("GET", "/api/v1/watchlist/asins");
    if (r.ok && r.data && Array.isArray(r.data.asins)) watchedCache = { at: Date.now(), data: r.data };
    return r;
  }
  if (msg.type === "observe") {
    const items = Array.isArray(msg.items) ? msg.items.slice(0, 50).filter(
      (i) => i && typeof i.asin === "string" && /^[A-Z0-9]{10}$/.test(i.asin) && Number.isInteger(i.price) && i.price > 0) : [];
    if (!items.length) return { ok: false, error: "nothing to record" };
    return call("POST", "/api/v1/observations", { items });
  }
  return { ok: false, error: "unknown request" };
}
