# Security and privacy threat model

Assets: your purchase history and wallet categories, the local API token, the Telegram bot
token, and the integrity of the savings advice (a manipulated answer can cost money or lead to a
phishing page).

| # | Threat | Mitigation | Residual risk | Verification |
|---|---|---|---|---|
| T1 | **Malicious website calls the local API** (CSRF / drive-by) | API needs `X-CouponHunter-Token`; browsers can't add custom headers cross-origin without a CORS preflight, and no CORS headers are ever sent. Dashboard POSTs need SameSite=Strict cookie + CSRF token + same-origin `Origin` | Malware already on the PC can read the token file | `test_web.py::test_api_requires_token`, `test_csrf_required` |
| T2 | **DNS rebinding** to 127.0.0.1 | Host header must equal `127.0.0.1:<port>`/`localhost:<port>` → 421 | — | `test_dns_rebinding_host_check` |
| T3 | **Stored XSS** via offer titles, product titles or feed text | All HTML through escape-by-default helpers; CSP `default-src 'none'`, no inline script; extension renders with `textContent` in a closed Shadow DOM | A future template bug would show literal text, not execute | `test_stored_xss_is_escaped`, `test_markup_not_double_escaped` |
| T4 | **Phishing / malicious redemption links** from feeds | Links kept only if https on an allowlist (amazon.in + named bank domains + owner-added); feed item links never used; extension re-checks allowlist before `window.open(…, noopener)` | Owner could allowlist a bad domain | `test_redemption_url_allowlist`, `lib.test.js` |
| T5 | **SSRF via submitted URLs** | Product URLs are parsed, never fetched. Feed fetches are host-pinned (redirects only within the host), https only, size- and time-capped | A malicious *registered* feed can still serve misleading content (T7) | `AmazonUrlTests`, `HttpClientTests.test_host_pinning` |
| T6 | **SQL injection** | Parameterised queries everywhere; the only dynamic SQL fragments are fixed column names (`repo.upsert_product` whitelist) and `?` placeholders | — | Code review; malformed input tests |
| T7 | **Manipulated or fake offer terms** | Feed offers are UNVERIFIED (50% prior), never "confirmed" without corroboration or your feedback; less trustworthy sources can't overwrite better ones; rejected-record ratio guards against mass changes; you can mark offers invalid | A fake offer is shown as unverified until you flag it | `test_less_authoritative_source_does_not_overwrite_title` |
| T8 | **XML entity attacks** in feeds | DTDs refused outright; 2 MB cap | — | `RssTests.test_refuses_dtd` |
| T9 | **Malicious extension messages** (other extensions, page scripts) | Service worker accepts messages only from its own extension id; page operations only when the sender tab is an amazon.in product page whose ASIN equals the message ASIN; popup/options limited to `status`; payload fields re-validated | A compromised Amazon page could send a fake price for *that* ASIN via our own content script → mitigated by the stored price only affecting your own advice | Code review (`background.js`) |
| T10 | **Malformed or mismatched backend responses** | `validateEvaluation` rejects anything not matching the v1 schema; panel shows "update extension/backend" | — | `lib.test.js` |
| T11 | **Leaked notification token** | Token only in env vars; redacted from repr, errors (exception chain suppressed) and logs | Process environment is readable by the same OS user | `TelegramTests.test_token_never_in_error_or_repr`, `logs.redact` |
| T12 | **Unauthorised dashboard access** | Loopback bind only (non-loopback refused); session cookie derived via HMAC (raw token never in a cookie) | Other OS users on the same machine with the token file | `config.load`, `test_login_sets_strict_httponly_cookie` |
| T13 | **Sensitive shopping data exposure** | Wallet stores categories only; card-number-like strings rejected; no Amazon cookies, passwords, OTPs or session data are ever read; order refs optional | DB holds purchase history in plain SQLite (encrypt the disk; BitLocker) | `test_wallet_rejects_card_numbers` |
| T14 | **Over-broad extension permissions** | `storage` + loopback host permission only; the panel script runs on 4 product-URL patterns; the opt-in observer script runs on other amazon.in pages minus sensitive paths (T21); no `tabs`, `cookies`, `webRequest`, `scripting` or `<all_urls>` | — | `manifest.json` review; E2E asserts no panel on search pages |
| T15 | **Affiliate / attribution tampering** | None performed. Product links are canonical `/dp/ASIN` without tags. Cashback portal journeys are shown as steps the user follows | — | `canonical_product_url` test |
| T16 | **Dependency compromise** | Zero runtime dependencies. Playwright is CI-only and pinned | Python/Chrome themselves | `pyproject.toml` |
| T17 | **Fake gift-card marketplaces** | Not integrated. A discounted balance is something you model yourself after buying from a legitimate seller | Your own purchase decision | — |
| T18 | **Request floods / huge bodies** | 64 KiB body cap; 30 offers per capture; rule depth ≤16 | Local DoS is self-inflicted | `test_body_limit` |
| T19 | **Strangers messaging the Telegram bot** | Only the configured chat id is processed; others get no reply (no oracle); long polling means no inbound port | Anyone with the bot token could read updates → token stays in env vars | `test_bot.py::test_strangers_are_ignored_silently` |
| T20 | **Short-link resolution abused for SSRF** | Only https `amzn.in`/`amzn.to`/`a.co`; HEAD; redirects never followed; ≤2 shortener hops; result must parse as an amazon.in product URL; 5 s timeout | — | `ShortLinkTests` |
| T21 | **Passive observation over-collecting browsing data** | Opt-in; watched ASINs only (enforced by the backend, unwatched never stored); hourly limit for unchanged prices; checkout/sign-in/account/payment/order paths excluded in the manifest; no panel/DOM changes on those pages | It runs on other amazon.in pages when enabled | `test_passive_observations_only_for_watched_and_rate_limited`, E2E |
| T22 | **Replay/duplication after bot restart** | Persisted update offset; update id as idempotency key | — | `test_poll_persists_offset_and_survives_handler_crash` |
| T23 | **Phishing / spoofed promotional emails** | Imported offers always UNVERIFIED; links and their text dropped; attachments ignored; scripts/styles stripped; From/Subject stored as evidence; 200 MB / 5,000-message caps | A spoofed email can still add an unverified offer, shown as such | `test_email.py` |

## Residual risks you need to decide on

1. **Extension page capture and Amazon's Conditions of Use.** Capture is user-initiated,
   reads only the page you chose to view, stores locally, and is never redistributed. Amazon's
   licence nevertheless excludes "data gathering and extraction tools" (docs/02, [S]). If you
   aren't comfortable, leave auto-capture off and don't press Save: the panel still shows
   advice from data you enter manually.
2. **Plain-text local database.** Use full-disk encryption on the always-on laptop.
3. **Remote access** is unsupported. If you need it, use a VPN such as Tailscale and keep the
   loopback bind. Never port-forward the dashboard.
