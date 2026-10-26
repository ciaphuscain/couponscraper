// End-to-end: real Chromium + unpacked extension + real backend.
// amazon.in requests are fulfilled from a local fixture; nothing reaches Amazon.
// Run: node tests/extension/e2e.js   (needs python3 and Playwright's Chromium)
"use strict";
const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");

const ROOT = path.resolve(__dirname, "../..");
const EXT = path.join(ROOT, "extension");
const PORT = 18765;
const ASIN = "B0E2ETEST1";

function py(home, ...args) {
  return execFileSync("python3", ["-m", "couponhunter", "--home", home, "--port", String(PORT), ...args],
                      { cwd: ROOT, encoding: "utf8" });
}

async function waitFor(fn, ms = 10000, what = "condition") {
  const end = Date.now() + ms;
  for (;;) {
    try { const v = await fn(); if (v) return v; } catch (e) { /* retry */ }
    if (Date.now() > end) throw new Error("timeout waiting for " + what);
    await new Promise((r) => setTimeout(r, 150));
  }
}

// Text of the closed shadow root, via CDP (pierce: true reaches closed roots).
async function panelNodes(cdp) {
  const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
  const out = [];
  const walk = (n, inPanel) => {
    const here = inPanel || (n.attributes && n.attributes.includes("couponhunter-host"));
    if (here && n.nodeType === 3) out.push({ text: n.nodeValue, parentId: n.parentId });
    (n.children || []).forEach((c) => walk(c, here));
    (n.shadowRoots || []).forEach((c) => walk(c, here));
  };
  walk(root, false);
  return out;
}

async function panelText(cdp) {
  return (await panelNodes(cdp)).map((n) => n.text).join(" | ");
}

async function clickButton(cdp, page, label) {
  const node = (await panelNodes(cdp)).find((n) => n.text.startsWith(label));
  if (!node) throw new Error("button not found: " + label);
  const { model } = await cdp.send("DOM.getBoxModel", { nodeId: node.parentId });
  const [x1, y1, , , x3, y3] = model.content;
  await page.mouse.click((x1 + x3) / 2, (y1 + y3) / 2);
}

(async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "ch-e2e-"));
  py(home, "init");
  py(home, "settings", "wallet", JSON.stringify([{ id: "hdfc-cc", label: "HDFC credit card", type: "credit_card", bank: "hdfc" }]));
  const token = py(home, "token").trim();
  const server = spawn("python3", ["-m", "couponhunter", "--home", home, "--port", String(PORT), "serve", "--no-scheduler"],
                       { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"] });
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "ch-e2e-profile-"));
  let ctx;
  try {
    await waitFor(async () => (await fetch(`http://127.0.0.1:${PORT}/healthz`)).ok, 10000, "backend");
    ctx = await chromium.launchPersistentContext(userData, {
      headless: true,
      channel: "chromium", // new headless mode; the old one cannot load extensions
      args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
    });
    const sw = ctx.serviceWorkers()[0] || (await ctx.waitForEvent("serviceworker"));
    await sw.evaluate(([t, p]) => chrome.storage.local.set({ token: t, backendUrl: `http://127.0.0.1:${p}` }), [token, PORT]);

    const fixture = fs.readFileSync(path.join(__dirname, "fixtures", "amazon_product.html"), "utf8");
    const searchFixture = fs.readFileSync(path.join(__dirname, "fixtures", "amazon_search.html"), "utf8");
    let amazonRequests = 0;
    await ctx.route("https://www.amazon.in/**", (route) => {
      amazonRequests += 1;
      const isSearch = new URL(route.request().url()).pathname.startsWith("/s");
      route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: isSearch ? searchFixture : fixture });
    });

    const page = await ctx.newPage();
    const cdp = await ctx.newCDPSession(page);
    const t0 = Date.now();
    await page.goto(`https://www.amazon.in/E2E-Fixture/dp/${ASIN}/ref=sr_1_1`);
    let text = await waitFor(async () => { const t = await panelText(cdp); return t.includes("Save price") && t; }, 10000, "panel");
    const firstPaintMs = Date.now() - t0;
    assert.ok(text.includes("Not tracked yet"), text);

    // One click: save price + offers and track.
    await clickButton(cdp, page, "Track");
    text = await waitFor(async () => { const t = await panelText(cdp); return t.includes("Lowest realistic price") && t; }, 10000, "result");
    // Coupon + bank stacking is not confirmed -> confirmed best is the bank offer alone.
    assert.ok(text.includes("₹9,250"), text);
    assert.ok(text.includes("Possibly ₹8,750"), text);
    assert.ok(text.includes("2 new offer(s), 1 not understood"), text);
    await page.screenshot({ path: path.join(home, "panel-unconfirmed.png") });

    // Owner confirms coupon+bank stack (e.g. after seeing it at checkout) -> reload shows ₹8,750 as confirmed.
    py(home, "settings", "stacking_defaults", JSON.stringify({ "bank_instant+product_coupon": "yes" }));
    await page.reload();
    text = await waitFor(async () => { const t = await panelText(cdp); return t.includes("Lowest realistic price") && t; }, 10000, "result 2");
    assert.ok(text.includes("₹8,750"), text);
    assert.ok(!text.includes("Possibly"), text);

    // Redemption feedback round-trips.
    await clickButton(cdp, page, "Applied");
    await waitFor(async () => (await panelText(cdp)).includes("Thanks"), 5000, "feedback");
    const ev = JSON.parse(py(home, "evaluate", ASIN, "--json"));
    assert.strictEqual(ev.best_confirmed.cash_cost, 875000);
    await page.screenshot({ path: path.join(home, "panel-confirmed.png") });

    // Non-product amazon.in pages get no panel. With passive observation OFF, nothing is recorded.
    const priceRows = () => JSON.parse(py(home, "evaluate", ASIN, "--json")).price_stats.count;
    const before = priceRows();
    await page.goto("https://www.amazon.in/s?k=headphones");
    await page.waitForTimeout(800);
    assert.strictEqual(await panelText(cdp), "");
    assert.strictEqual(priceRows(), before);

    // Opt in: the watched product's search-result price is recorded; the unwatched one is not.
    await sw.evaluate(() => chrome.storage.local.set({ observeWatched: true }));
    await page.goto("https://www.amazon.in/s?k=headphones&page=2");
    await waitFor(() => priceRows() === before + 1, 8000, "passive observation");
    const ev2 = JSON.parse(py(home, "evaluate", ASIN, "--json"));
    assert.strictEqual(ev2.price, 949900);
    assert.throws(() => py(home, "evaluate", "B0NOTWATCH"));

    console.log(JSON.stringify({ ok: true, panel_first_render_ms: firstPaintMs, amazon_requests_served_locally: amazonRequests,
                                 screenshots: [path.join(home, "panel-unconfirmed.png"), path.join(home, "panel-confirmed.png")] }));
  } finally {
    if (ctx) await ctx.close();
    server.kill();
  }
})().catch((e) => { console.error(e); process.exit(1); });
