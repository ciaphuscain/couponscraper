"use strict";
const $ = (id) => document.getElementById(id);
(async () => {
  const s = await chrome.storage.local.get({ backendUrl: "http://127.0.0.1:8765", token: "", autoCapture: false });
  $("backend").value = s.backendUrl;
  $("token").value = s.token;
  $("auto").checked = s.autoCapture;
})();
$("save").addEventListener("click", async () => {
  const base = CHLib.normalizeBackendUrl($("backend").value.trim());
  if (!base) { $("msg").textContent = "Backend must be http://127.0.0.1:<port> or http://localhost:<port>"; return; }
  await chrome.storage.local.set({ backendUrl: base, token: $("token").value.trim(), autoCapture: $("auto").checked });
  $("msg").textContent = "Saved.";
});
$("test").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "status" }, (r) => {
    $("msg").textContent = r && r.ok ? `Connected: backend v${r.data.version}` : `Failed: ${(r && r.error) || "no response"}`;
  });
});
