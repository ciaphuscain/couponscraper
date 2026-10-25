"use strict";
(async () => {
  const s = await chrome.storage.local.get({ backendUrl: "http://127.0.0.1:8765" });
  const base = CHLib.normalizeBackendUrl(s.backendUrl) || "http://127.0.0.1:8765";
  document.getElementById("dash").href = base + "/";
  document.getElementById("opts").addEventListener("click", (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
  chrome.runtime.sendMessage({ type: "status" }, (r) => {
    const el = document.getElementById("status");
    if (r && r.ok) {
      el.textContent = `Backend OK (v${r.data.version}, ${r.data.offers} offers)`;
    } else {
      el.textContent = (r && r.error) || "Backend not reachable";
      el.className = "e";
    }
  });
})();
