# ADR-008: Telegram companion, short-link resolution and opt-in passive observation

**Status:** accepted (amends ADR-004)

**Context.** The biggest limitation was data freshness: prices arrived only when the owner opened a
product page on a PC with the extension, while most shopping happens in the Amazon mobile app.

**Decisions.**
1. **Telegram bot via long polling.** No webhook, public URL or open port. Messages are accepted
   only from the configured chat id, and others are ignored silently. The update offset is
   persisted and update ids are used as idempotency keys.
2. **Short-link resolution.** When the owner *sends* an `amzn.in` / `amzn.to` / `a.co` link (the
   app's Share button produces these), the backend makes one HEAD request to the shortener and
   reads the `Location` header. It never follows it, allows at most 2 shortener hops, and doesn't
   fetch amazon.in. This is a single user-initiated lookup, equivalent to tapping the link, and
   not data extraction from amazon.in.
3. **Passive observation (opt-in, default off).** On amazon.in listing pages the owner browses,
   the extension reads prices of products **already on the watchlist** from `[data-asin]` tiles.
   The backend drops unwatched ASINs (never stored) and records an unchanged price at most once
   an hour. Checkout, sign-in, account, payment and order paths are excluded in the manifest.

**Consequences.** Price freshness now follows normal browsing and mobile use. Remaining grey area
(docs/06): reading prices the owner views is still "reading Amazon pages", even though it is
user-initiated, local and minimal. It stays opt-in.
