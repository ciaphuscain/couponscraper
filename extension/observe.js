/* Opt-in passive observer for non-product amazon.in pages. Records prices
 * only for products already on your watchlist; reads [data-asin] tiles only;
 * renders nothing. Off unless enabled in the extension options. */
(function () {
  "use strict";
  const L = self.CHLib;
  if (L.asinFromUrl(location.href)) return; // product pages are handled by content.js
  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { ok: false })); } catch (e) { res({ ok: false }); }
  });
  async function scan() {
    const w = await send({ type: "watched" });
    if (!w.ok || !w.data || !Array.isArray(w.data.asins) || !w.data.asins.length) return;
    const items = L.extractListingPrices(document, new Set(w.data.asins));
    if (items.length) await send({ type: "observe", items });
  }
  (async () => {
    const s = await send({ type: "settings" });
    if (!s.ok || !s.data.observeWatched) return;
    await scan();
    setTimeout(scan, 4000); // late-loading tiles (lazy search results)
  })();
})();
