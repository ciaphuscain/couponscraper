"""Dashboard pages. Pure functions of (database state, time) -> HTML string."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import repo, service
from ..db import Database, parse_iso
from ..domain import Lifecycle
from ..eligibility import describe_payment
from ..optimizer import Scenario
from ..verification import offer_confidence
from .html import Raw, badge, esc, field, form, join, link, money, page, pct, select, table, tag, textarea


@dataclass
class Ctx:
    db: Database
    now: datetime
    csrf: str
    flash: str | None


def ago(ts: str | None, now: datetime) -> str:
    dt = parse_iso(ts)
    if dt is None:
        return "never"
    s = (now - dt).total_seconds()
    if s < 0:
        return "in " + ago_delta(-s)
    return ago_delta(s) + " ago"


def ago_delta(s: float) -> str:
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s // 60)}m"
    if s < 172_800:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


IST = timezone(timedelta(hours=5, minutes=30))


def ist(dt: datetime | None) -> str:
    return "unknown" if dt is None else dt.astimezone(IST).strftime("%d %b %Y %H:%M IST")


def origin_badge(origin: str | None) -> Raw:
    if origin == "synthetic":
        return badge("synthetic test data", "synthetic")
    if origin == "live":
        return badge("seen on amazon.in", "ok")
    return badge(origin or "unknown")


LC_KIND = {Lifecycle.ACTIVE: "ok", Lifecycle.STALE: "warn", Lifecycle.EXPIRED: "bad", Lifecycle.WITHDRAWN: "bad",
           Lifecycle.NOT_STARTED: "warn"}


def error(status: int, message: str) -> str:
    return page(f"Error {status}", tag("p", message) + link("/", "Back to overview"))


def login(msg: str | None = None) -> str:
    body = join([
        tag("p", "Paste the API token printed by `couponhunter token` (stored in your data folder)."),
        Raw('<form method="post" action="/login" class="stack"><label>Token<input type="password" name="token" '
            'autocomplete="current-password" required></label><button type="submit">Sign in</button></form>'),
    ])
    return page("Sign in", body, flash=msg)


def _answer(s: Scenario | None, possible: Scenario, baseline: Scenario) -> Raw:
    if s is None:
        return tag("p", "No combination with fully known rules.", class_="muted")
    parts = [Raw('<div class="answer">'),
             tag("div", "Lowest realistic price now", class_="muted"),
             tag("div", money(s.cash_cost), class_="big")]
    detail = []
    if s.instant_savings or s.funding_savings:
        detail.append(f"{money(s.instant_savings + s.funding_savings)} off {money(baseline.cash_cost)} at checkout")
    if s.instrument is not None:
        detail.append(f"pay with {s.instrument.label}")
    detail.append(f"{pct(s.success_probability_bp)} chance every offer applies")
    parts.append(tag("div", " · ".join(detail)))
    if s.expected_deferred:
        parts.append(tag("div", f"Plus up to {money(s.expected_deferred)} cashback later (not guaranteed; "
                                f"risk-adjusted {money(s.risk_adjusted_deferred)}). If it all pays: "
                                f"{money(s.effective_if_all_paid)} effective.", class_="muted"))
    if s.steps:
        parts.append(Raw("<ol>") + join(tag("li", st) for st in s.steps) + Raw("</ol>"))
    if possible is not s and possible.cash_cost < s.cash_cost:
        parts.append(tag("p", f"Possibly {money(possible.cash_cost)} if unconfirmed conditions hold:"))
        parts.append(Raw('<ul class="tight">') + join(tag("li", a) for a in possible.assumptions[:5]) + Raw("</ul>"))
    parts.append(Raw("</div>"))
    return join(parts)


def overview(ctx: Ctx) -> str:
    rows = repo.watchlist_rows(ctx.db)
    settings = repo.get_settings(ctx.db)
    potential = 0
    actionable = []
    drops = []
    for w in rows:
        try:
            ev = service.evaluate(ctx.db, w["asin"], ctx.now, settings)
        except repo.NotFound:
            continue
        if ev.result is None:
            continue
        best = ev.result.best_confirmed
        if best and best.applied:
            saved = best.instant_savings + best.funding_savings
            potential += saved
            actionable.append([link(f"/product/{w['asin']}", w["title"] or w["asin"]), money(ev.result.product.price),
                               money(best.cash_cost), money(saved), pct(best.success_probability_bp)])
        if ev.verdict and ev.verdict.is_good_deal:
            drops.append([link(f"/product/{w['asin']}", w["title"] or w["asin"]), money(ev.result.product.price),
                          "; ".join(ev.verdict.reasons)])
    s = repo.savings_summary(ctx.db)
    broken = ctx.db.query("SELECT name, disabled_reason, last_error FROM sources WHERE enabled=0 OR consecutive_failures>0")
    cards = Raw('<div class="cards">') + join(
        Raw(f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>') for k, v in [
            ("Watched products", str(len(rows))),
            ("Actionable now", str(len(actionable))),
            ("Available instant savings", money(potential)),
            ("Realised savings (recorded)", money(s.realized_total)),
            ("Pending cashback (not yet paid)", money(s.cashback_pending + s.cashback_confirmed_unpaid)),
        ]) + Raw("</div>")
    body = [cards]
    if broken:
        body.append(tag("h2", "Source problems"))
        body.append(table(["Source", "Problem"], [[b["name"], b["disabled_reason"] or b["last_error"]] for b in broken]))
    body.append(tag("h2", "Actionable offers on your watchlist"))
    body.append(table(["Product", "Price", "You'd pay", "Saving", "Confidence"], actionable,
                      "No eligible savings right now on watched products."))
    body.append(tag("h2", "Genuine price drops"))
    body.append(table(["Product", "Price", "Why"], drops, "None detected (needs several price observations)."))
    return page("Overview", join(body), "/", ctx.flash)


def watchlist(ctx: Ctx) -> str:
    settings = repo.get_settings(ctx.db)
    rows = []
    for w in repo.watchlist_rows(ctx.db):
        best = possible = None
        ev = service.evaluate(ctx.db, w["asin"], ctx.now, settings)
        if ev.result is not None:
            best, possible = ev.result.best_confirmed, ev.result.best_possible
        stale = ev.result is not None and ev.result.product.price_is_stale
        rows.append([
            link(f"/product/{w['asin']}", w["title"] or "(untitled)"), w["asin"],
            join([money(w["last_price"]), " ", origin_badge(w["last_price_origin"]) if w["last_price"] else ""]),
            join([ago(w["last_observed_at"], ctx.now), " ", badge("stale", "warn") if stale else ""]),
            money(w["target_price"]), money(best.cash_cost if best else None),
            money(possible.cash_cost if possible and best and possible.cash_cost < best.cash_cost else None),
            form("/actions/unwatch", ctx.csrf, Raw(f'<input type="hidden" name="asin" value="{esc(w["asin"])}">'),
                 "Remove", inline=True),
        ])
    add = form("/actions/watch", ctx.csrf, join([
        field("Amazon.in product URL or ASIN", "url", required=True, placeholder="https://www.amazon.in/dp/…"),
        field("Title (optional)", "title"), field("Target price ₹ (optional)", "target"),
        field("Current price ₹ (optional; otherwise captured by the extension)", "price"),
    ]), "Watch product")
    body = join([table(["Product", "ASIN", "Last price", "Checked", "Target", "Best (confirmed)", "Best (if unconfirmed holds)", ""],
                       rows, "Your watchlist is empty."), tag("h2", "Add a product"), add])
    return page("Watchlist", body, "/watchlist", ctx.flash)


def offers(ctx: Ctx, include_expired: bool = False) -> str:
    rows = []
    for o, asins in repo.all_offers(ctx.db, ctx.now, include_expired=include_expired):
        c = offer_confidence(o)
        n_src = ctx.db.one("SELECT COUNT(*) FROM offer_sources WHERE offer_id=?", (int(o.id),))[0]
        restrictions = [describe_payment(o.payment)] if o.payment else []
        restrictions += [x for x, f in (("new customers only", o.new_customer_only), ("Prime only", o.prime_only),
                                        ("targeted account offer", o.account_specific)) if f]
        rows.append([
            o.kind.value.replace("_", " "), o.title, service.describe_offer_value(o),
            o.code or "", ", ".join(asins) if asins else "not product-specific", "; ".join(restrictions) or "—",
            ist(o.valid_until), badge(o.lifecycle.value, LC_KIND.get(o.lifecycle, "")),
            join([badge(o.authenticity.value), " ", f"{c.bp // 100}%"]),
            join([str(n_src), " ", badge("synthetic", "synthetic") if o.is_synthetic else ""]),
            join([form("/actions/flag", ctx.csrf, Raw(f'<input type="hidden" name="offer_id" value="{esc(o.id)}">'
                                                     '<input type="hidden" name="flag" value="invalid">'), "Mark invalid", inline=True)]),
        ])
    add = form("/actions/offer-text", ctx.csrf, join([
        textarea("Offer text, exactly as shown (e.g. \"10% Instant Discount up to INR 1250 on HDFC Bank Credit Card…\")", "text"),
        field("Applies to ASIN (optional)", "asin"), field("Public code (optional)", "code"),
        field("Valid until (YYYY-MM-DD, optional)", "valid_until"),
        select("Discount computed on", "basis", [("", "terms don't say"), ("price", "item price before other offers"),
                                                 ("after_coupons", "price after coupon/code"), ("payable", "final payable")]),
        select("Where did you see it?", "evidence", [("official_page", "On amazon.in or the bank's own site"),
                                                    ("user_entered", "Somewhere else (unverified)")]),
        field("Official offer link (https, amazon.in or bank site, optional)", "url"),
    ]), "Add offer")
    toggle = link("/offers" if include_expired else "/offers?expired=1",
                  "Hide expired" if include_expired else "Show expired")
    body = join([tag("p", toggle), table(["Type", "Offer", "Value", "Code", "Products", "Restrictions", "Expires",
                                          "State", "Source / confidence", "Sources", ""], rows,
                                         "No offers yet. Import a file, add one below, or capture from a product page."),
                 tag("h2", "Add an offer you've seen"), add])
    return page("Offer explorer", body, "/offers", ctx.flash)


def _scenario_rows(scenarios: list[Scenario]) -> list[list[object]]:
    out = []
    for s in scenarios:
        out.append([
            "; ".join(a.title for a in s.applied) or "No offers",
            s.instrument.label if s.instrument else "—",
            money(s.checkout_payable), money(s.cash_cost), money(s.expected_deferred) if s.expected_deferred else "—",
            money(s.risk_adjusted_cost), pct(s.success_probability_bp),
            badge("rules known", "ok") if s.rules_confirmed else badge("assumptions", "warn"),
        ])
    return out


def product(ctx: Ctx, asin: str) -> str:
    p = repo.get_product(ctx.db, asin)
    ev = service.evaluate(ctx.db, asin, ctx.now)
    body: list[object] = [tag("p", join([link(repo.product_url(asin), repo.product_url(asin)), "  ",
                                         badge(f"ASIN {asin}")]))]
    if ev.result is None:
        body.append(tag("p", "No price recorded yet. Open the product page with the extension and press Save, "
                             "or enter the price below.", class_="muted"))
    else:
        r = ev.result
        body.append(tag("p", join([f"Observed price {money(r.product.price)} ", origin_badge(ev.price_origin), " ",
                                   ago(repo.iso(r.product.price_observed_at), ctx.now), " ",
                                   badge("stale: refresh before buying", "warn") if r.product.price_is_stale else ""])))
        body.append(_answer(r.best_confirmed, r.best_possible, r.baseline))
        for n in r.notes:
            body.append(tag("p", n, class_="muted"))
        body.append(tag("h2", "All feasible combinations"))
        body.append(table(["Offers", "Pay with", "Checkout", "Cash cost", "Cashback (later)", "Risk-adjusted",
                           "Applies", "Rules"], _scenario_rows(r.scenarios[:10])))
        if r.excluded:
            body.append(tag("h2", "Offers that don't apply"))
            body.append(table(["Offer", "Why not"], [[e.title, e.reason] for e in r.excluded]))
        st = ev.stats
        if st and st.count:
            body.append(tag("h2", "Price history"))
            body.append(table(["Observations", "Days covered", "Min", "Median", "Max", "7-day trend"],
                              [[st.count, st.coverage_days, money(st.minimum), money(st.median), money(st.maximum),
                                pct(st.trend_7d_bp)]]))
            v = ev.verdict
            if v:
                body.append(Raw('<ul class="tight">') + join(tag("li", x) for x in v.reasons + v.caveats) + Raw("</ul>"))
        offers_here = repo.offers_for_asin(ctx.db, asin, ctx.now)
        if offers_here:
            body.append(tag("h2", "Did it work? (improves future confidence)"))
            rows = []
            for o in offers_here:
                idem = secrets.token_hex(8)
                hidden = Raw(f'<input type="hidden" name="asin" value="{esc(asin)}"><input type="hidden" name="offer_id" '
                             f'value="{esc(o.id)}"><input type="hidden" name="idem" value="{esc(idem)}">')
                rows.append([o.title, f"{o.redeemed_ok} ok / {o.redeemed_failed} failed", join([
                    form("/actions/redeem", ctx.csrf, hidden + Raw('<input type="hidden" name="outcome" value="applied">'),
                         "Applied", inline=True),
                    form("/actions/redeem", ctx.csrf, hidden + Raw('<input type="hidden" name="outcome" value="failed">'),
                         "Didn't apply", inline=True)])])
            body.append(table(["Offer", "Your history", ""], rows))
    body.append(tag("h2", "Record a price"))
    body.append(form("/actions/price", ctx.csrf, join([Raw(f'<input type="hidden" name="asin" value="{esc(asin)}">'),
                                                       field("Price ₹", "price", required=True),
                                                       field("List/MRP ₹ (optional)", "list_price")]), "Save price"))
    body.append(tag("h2", "Record a purchase"))
    body.append(form("/actions/purchase", ctx.csrf, join([
        Raw(f'<input type="hidden" name="asin" value="{esc(asin)}"><input type="hidden" name="idem" value="{secrets.token_hex(8)}">'),
        field("Price without offers ₹", "reference_price", ev.result.baseline.cash_cost / 100 if ev.result else "", required=True),
        field("Amount paid at checkout ₹", "checkout_paid", required=True),
        field("Cash actually spent ₹ (if paid with discounted gift-card balance)", "cash_paid"),
        field("Cashback expected ₹ (optional)", "cashback"), field("Cashback provider", "cashback_provider"),
        field("Notes", "notes")]), "Record purchase"))
    return page(p["title"] or asin, join(body), "/watchlist", ctx.flash)


def history(ctx: Ctx) -> str:
    s = repo.savings_summary(ctx.db)
    cards = Raw('<div class="cards">') + join(
        Raw(f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>') for k, v in [
            ("Realised total", money(s.realized_total)), ("Instant discounts", money(s.realized_instant)),
            ("Gift-card funding", money(s.realized_funding)), ("Cashback paid", money(s.cashback_paid)),
            ("Confirmed, not yet paid", money(s.cashback_confirmed_unpaid)), ("Pending / expected", money(s.cashback_pending)),
            ("Cashback confirmation rate", pct(s.confirmation_rate_bp) if s.confirmation_rate_bp is not None else "no data"),
        ]) + Raw("</div>")
    purchases = [[r["purchased_at"], r["title"] or "—", money(r["reference_price"]), money(r["checkout_paid"]),
                  money(r["cash_paid"]), money(r["reference_price"] - r["cash_paid"])]
                 for r in ctx.db.query("""SELECT pu.*, p.title FROM purchases pu LEFT JOIN products p ON p.id = pu.product_id
                                          ORDER BY purchased_at DESC LIMIT 100""")]
    attempts = [[r["attempted_at"], r["title"], r["outcome"], r["failure_reason"] or "", r["channel"]]
                for r in ctx.db.query("""SELECT ra.*, o.title FROM redemption_attempts ra JOIN offers o ON o.id = ra.offer_id
                                         ORDER BY attempted_at DESC LIMIT 100""")]
    cb_rows = []
    for r in ctx.db.query("SELECT * FROM cashback_records ORDER BY created_at DESC LIMIT 100"):
        nxt = sorted(repo.CASHBACK_TRANSITIONS[r["status"]])
        ctl = form("/actions/cashback", ctx.csrf, join([
            Raw(f'<input type="hidden" name="record_id" value="{r["id"]}"><input type="hidden" name="from" value="{esc(r["status"])}">'),
            select("", "status", [(x, x) for x in nxt]), field("", "amount", placeholder="₹ amount")]), "Update",
            inline=True) if nxt else ""
        cb_rows.append([r["provider"], money(r["advertised"]), r["status"], money(r["amount"]), ago(r["updated_at"], ctx.now), ctl])
    body = join([cards, tag("p", "Realised savings count only what you recorded paying and cashback marked paid. "
                                 "Pending cashback is never included.", class_="muted"),
                 tag("h2", "Purchases"), table(["When", "Product", "Without offers", "Checkout", "Cash", "Saved"], purchases),
                 tag("h2", "Cashback tracking"), table(["Provider", "Advertised", "Status", "Amount", "Updated", ""], cb_rows),
                 tag("h2", "Redemption attempts"), table(["When", "Offer", "Outcome", "Reason", "Via"], attempts)])
    return page("Savings & history", body, "/history", ctx.flash)


def health(ctx: Ctx, status: dict) -> str:
    srcs = [[s["name"], s["adapter"], badge("enabled", "ok") if s["enabled"] else badge("disabled", "bad"),
             ago(s["last_success_at"], ctx.now), s["consecutive_failures"], s["disabled_reason"] or s["last_error"] or "",
             s["next_fetch_at"] or ""] for s in status["sources"]]
    runs = [[r["source_id"], r["started_at"], r["status"], r["fetched"], r["created"], r["updated"], r["duplicates"],
             r["rejected"], (r["error"] or "")[:160]]
            for r in ctx.db.query("SELECT * FROM source_runs ORDER BY started_at DESC, id DESC LIMIT 25")]
    jobs = [[j["job"], ago(j["last_started"], ctx.now), j["status"]] for j in status["jobs"]]
    alerts_rows = [[a["created_at"], a["reason"], a["status"], a["attempts"], a["last_error"] or "", a["sent_at"] or ""]
                   for a in ctx.db.query("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 25")]
    body = join([
        tag("p", f"Version {status['version']} · API v{status['api_version']} · {status['offers']} offers stored"),
        tag("h2", "Sources"), table(["Source", "Adapter", "State", "Last success", "Failures", "Problem", "Next fetch"], srcs),
        tag("h2", "Recent source runs"),
        table(["Source", "Started", "Status", "Fetched", "New", "Updated", "Dup", "Rejected", "Messages"], runs),
        tag("h2", "Scheduler jobs"), table(["Job", "Last run", "Status"], jobs, "Scheduler hasn't run yet."),
        tag("h2", "Alert outbox"), table(["Created", "Reason", "Status", "Attempts", "Error", "Sent"], alerts_rows),
    ])
    return page("Health", body, "/health", ctx.flash)


def settings(ctx: Ctx, port: int) -> str:
    s = repo.get_settings(ctx.db)
    prof = s["profile"]

    def tri(v):
        return {True: "yes", False: "no"}.get(v, "")

    q = s.get("quiet_hours") or {}
    f = form("/actions/settings", ctx.csrf, join([
        select("Amazon Prime member?", "is_prime", [("", "not set"), ("yes", "yes"), ("no", "no")], tri(prof.get("is_prime"))),
        select("New Amazon customer?", "is_new_customer", [("", "not set"), ("yes", "yes"), ("no", "no")],
               tri(prof.get("is_new_customer"))),
        textarea("Payment methods you own (JSON; categories only, never card numbers). Example: "
                 '[{"id":"hdfc-cc","label":"HDFC Millennia credit card","type":"credit_card","bank":"hdfc","network":"visa"}, '
                 '{"id":"gc","label":"Gift card balance (bought at 3% off)","type":"amazon_pay_balance","cost_ratio_bp":9700}]',
                 "wallet", json.dumps(s["wallet"], indent=1), rows=6),
        field("Minimum savings to alert ₹", "min_savings", s["min_savings_paise"] / 100),
        field("…and at least this share of price (basis points, 300 = 3%)", "min_savings_pct_bp", s["min_savings_pct_bp"]),
        field("Alert cooldown per product (hours)", "cooldown_hours", s["cooldown_hours"]),
        field("Treat prices older than (hours) as stale", "price_fresh_hours", s["price_fresh_hours"]),
        field("Quiet hours start (IST, HH:MM)", "quiet_start", q.get("start", "")),
        field("Quiet hours end (IST, HH:MM)", "quiet_end", q.get("end", "")),
        textarea('Stacking rules you have confirmed (JSON), e.g. {"bank_instant+product_coupon":"yes"}', "stacking_defaults",
                 json.dumps(s["stacking_defaults"]), rows=2),
        Raw('<label><input type="checkbox" name="hypothetical"' + (" checked" if s["include_hypothetical_instruments"] else "")
            + '> Also show offers for cards I don\'t have</label>'),
    ]), "Save settings")
    body = join([f, tag("h2", "Browser extension"),
                 tag("p", f"Backend address: http://127.0.0.1:{port}. Get the token with `couponhunter token` and paste "
                          "it into the extension's options page."),
                 tag("h2", "Notifications"),
                 tag("p", "Telegram is configured with the environment variables COUPONHUNTER_TELEGRAM_TOKEN and "
                          "COUPONHUNTER_TELEGRAM_CHAT_ID (never stored in the database). Without them, alerts are "
                          "written to the console and to alerts.jsonl in your data folder.")])
    return page("Settings", body, "/settings", ctx.flash)
