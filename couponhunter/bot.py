"""Telegram bot: the mobile companion.

Most Indian shopping happens in the Amazon app, where no extension can run.
The bot closes that gap: share a product from the app to the bot to track it,
ask for the best price, record the price or offers you see, and report
whether an offer worked, all from your phone.

Safety:
  * long polling (getUpdates): no webhook, no public URL, no open port;
  * only messages from the configured chat id are processed; others are
    ignored without a reply (no information leak to strangers);
  * the update offset is persisted, and update ids are used as idempotency
    keys, so a restart never double-records anything;
  * replies are plain text (no parse_mode).

Short links (amzn.in / amzn.to / a.co) from the app's Share button are
resolved by reading the shortener's redirect *header* once, without following
it, for a link the user explicitly sent. Nothing is fetched from amazon.in
(ADR-004 amendment, docs/adr/ADR-008).
"""

from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable
from urllib.parse import urlsplit

from . import ingest, logs, repo, service
from .amazon import SHORTLINK_HOSTS, InvalidAmazonURL, extract_asin, is_valid_asin
from .db import Database, utcnow
from .domain import Authenticity
from .money import fmt, parse_inr
from .notify import NotifyError, TelegramNotifier
from .sources import FetchResult, SourceRecord

log = logs.get("bot")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
ASIN_TOKEN_RE = re.compile(r"\b([A-Z0-9]{10})\b")
MAX_TEXT = 2000

HELP = """CouponHunter commands
• Share an Amazon product (or paste its link) to track it
• /best <link|ASIN>: lowest realistic price now
• /list: your watchlist
• /price <ASIN> <amount>: record a price you see in the app
• /offer <ASIN> <offer text>: add an offer you see (paste it as shown)
• /target <ASIN> <amount>: alert when you can pay this or less
• /applied <offer#> or /failed <offer#>: tell me if an offer worked
• /stop <ASIN>: stop watching
• /cards: which card saves you most
• /status: system health"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface the 3xx as an HTTPError so we can read Location without following it


def resolve_short_link(url: str, opener=None, max_hops: int = 2) -> str:
    """Return the ASIN behind an Amazon short link by reading redirect headers only."""
    opener = opener or urllib.request.build_opener(_NoRedirect())
    current = url
    for _ in range(max_hops):
        parts = urlsplit(current)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in SHORTLINK_HOSTS or parts.username or parts.port:
            break
        req = urllib.request.Request(current, method="HEAD", headers={"User-Agent": "CouponHunter/0.1 (link resolver)"})
        try:
            with opener.open(req, timeout=5):
                raise InvalidAmazonURL("short link did not redirect")
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308) or not e.headers.get("Location"):
                raise InvalidAmazonURL(f"short link returned HTTP {e.code}") from None
            current = urllib.request.urljoin(current, e.headers["Location"])
        except (urllib.error.URLError, TimeoutError, OSError):
            raise InvalidAmazonURL("couldn't reach the short-link service") from None
    return extract_asin(current)


def _asin_from(arg: str, resolver: Callable[[str], str]) -> str:
    arg = arg.strip()
    m = URL_RE.search(arg)
    if m:
        url = m.group(0).rstrip(").,")
        host = (urlsplit(url).hostname or "").lower()
        return resolver(url) if host in SHORTLINK_HOSTS else extract_asin(url)
    tok = arg.split()[0].upper() if arg.split() else ""
    if is_valid_asin(tok):
        return tok
    raise InvalidAmazonURL("send an amazon.in link or a 10-character ASIN")


def summary(db: Database, asin: str, now: datetime) -> str:
    """The core answer, phone-sized."""
    ev = service.evaluate(db, asin, now)
    head = ev.title or asin
    if ev.result is None:
        return (f"{head}\nTracking ✓. No price yet: send /price {asin} <amount> with the price you see, "
                f"or open it on your PC with the extension.\n{repo.product_url(asin)}")
    r = ev.result
    best = r.best_confirmed or r.baseline
    lines = [head]
    age = (now - r.product.price_observed_at).total_seconds() / 3600 if r.product.price_observed_at else 0
    lines.append(f"Price {fmt(r.product.price)} (seen {age:.0f}h ago{', STALE' if r.product.price_is_stale else ''})")
    if ev.advice and ev.advice.action != "unknown":
        label = {"buy_now": "BUY NOW", "wait": "WAIT", "fair": "Fair price"}[ev.advice.action]
        lines.append(f"{label}: {ev.advice.headline}")
    lines.append(f"Lowest realistic: {fmt(best.cash_cost)}" + (
        f" with {best.instrument.label}" if best.instrument and best.applied else ""))
    for a in best.applied:
        lines.append(f"  #{a.offer_id} {a.title}: {fmt(a.amount)}{' later, not guaranteed' if a.deferred else ''}")
    for st in best.steps[:4]:
        lines.append(f"  → {st}")
    p = r.best_possible
    if p is not best and p.cash_cost < best.cash_cost:
        lines.append(f"Possibly {fmt(p.cash_cost)} if: {p.assumptions[0] if p.assumptions else 'unconfirmed'}")
    if best.applied:
        lines.append("After trying: /applied <#> or /failed <#>")
    if any(o.is_synthetic for o in ev.offers):
        lines.append("[includes SYNTHETIC demo data]")
    lines.append(repo.product_url(asin))
    return "\n".join(lines)


class Bot:
    def __init__(self, db: Database, api: TelegramNotifier, clock: Callable[[], datetime] = utcnow,
                 resolver: Callable[[str], str] = resolve_short_link):
        self.db = db
        self.api = api
        self.clock = clock
        self.resolver = resolver
        self.chat_id = str(api.chat_id)
        self._stop = threading.Event()
        ingest.ensure_builtin_sources(db, clock())

    # -- message handling (pure: returns the reply text) ---------------------------

    def handle(self, update: dict) -> str | None:
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return None  # stranger: ignore silently
        text = str(msg.get("text") or msg.get("caption") or "")[:MAX_TEXT].strip()
        if not text:
            return None
        uid = int(update.get("update_id", 0))
        now = self.clock()
        try:
            return self._dispatch(text, uid, now)
        except InvalidAmazonURL as e:
            return f"Couldn't use that link: {e}"
        except repo.NotFound:
            return "I'm not tracking that product yet. Share its link first."
        except ValueError as e:
            return f"Sorry: {e}"

    def _dispatch(self, text: str, uid: int, now: datetime) -> str:
        if not text.startswith("/"):
            if URL_RE.search(text):
                return self._track(text, now)
            return HELP
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/best":
            if not arg:
                return "Usage: /best <link or ASIN>"
            asin = _asin_from(arg, self.resolver)
            repo.get_product(self.db, asin)
            return summary(self.db, asin, now)
        if cmd == "/list":
            return self._list(now)
        if cmd == "/price":
            asin, amount = self._asin_amount(arg)
            pid = repo.get_product(self.db, asin)["id"]
            repo.record_price(self.db, pid, amount, now, f"telegram:{uid}", "manual")
            return "Price recorded.\n\n" + summary(self.db, asin, now)
        if cmd == "/target":
            asin, amount = self._asin_amount(arg)
            pid = repo.get_product(self.db, asin)["id"]
            w = self.db.one("SELECT min_savings FROM watchlist WHERE product_id=?", (pid,))
            repo.watch(self.db, pid, now, target_price=amount, min_savings=w["min_savings"] if w else None)
            return f"I'll alert you when you can pay {fmt(amount)} or less."
        if cmd == "/offer":
            return self._offer(arg, uid, now)
        if cmd in ("/applied", "/failed"):
            oid = int(arg.strip().lstrip("#")) if arg.strip().lstrip("#").isdigit() else None
            if oid is None:
                return f"Usage: {cmd} <offer number from /best>"
            repo.record_redemption(self.db, oid, None, "applied" if cmd == "/applied" else "failed", now,
                                   channel="telegram", idempotency_key=f"tg-{uid}")
            return "Thanks, recorded. This updates how much I trust that offer."
        if cmd == "/stop":
            asin = _asin_from(arg, self.resolver)
            repo.unwatch(self.db, repo.get_product(self.db, asin)["id"])
            return f"Stopped watching {asin}."
        if cmd == "/cards":
            return self._cards(now)
        if cmd == "/status":
            return self._status(now)
        return HELP

    def _asin_amount(self, arg: str) -> tuple[str, int]:
        parts = arg.split()
        if len(parts) < 2:
            raise ValueError("usage: <ASIN or link> <amount>, e.g. /price B0ABCDEFGH 1299")
        asin = _asin_from(" ".join(parts[:-1]), self.resolver)
        amount = parse_inr(parts[-1])
        if amount <= 0:
            raise ValueError("amount must be positive")
        return asin, amount

    def _track(self, text: str, now: datetime) -> str:
        asin = _asin_from(text, self.resolver)
        title = URL_RE.sub("", text).strip(" -:\n")[:200]  # the app's share text usually starts with the title
        pid = repo.upsert_product(self.db, asin, now, title=title if len(title) > 3 else "")
        existing = self.db.one("SELECT target_price, min_savings FROM watchlist WHERE product_id=?", (pid,))
        if existing is None:
            repo.watch(self.db, pid, now)
        return "Tracking ✓\n\n" + summary(self.db, asin, now)

    def _offer(self, arg: str, uid: int, now: datetime) -> str:
        first, _, rest = arg.strip().partition(" ")
        if not rest:
            return "Usage: /offer <ASIN> <offer text exactly as shown>"
        asin = _asin_from(first, self.resolver)
        repo.get_product(self.db, asin)
        stats = ingest.RunStats("manual")
        rec = SourceRecord({"text": rest, "asins": [asin]}, "official_page", Authenticity.OFFICIAL,
                           raw_text=rest[:2000])
        with self.db.tx() as c:
            ingest.ingest_result(c, "manual", FetchResult([rec], snapshot=False, data_origin="manual"), now, stats,
                                 repo.extra_domains(repo.get_settings(self.db)))
        if stats.rejected:
            return "I couldn't understand that offer. Paste it exactly as the app shows it, e.g. " \
                   "\"10% Instant Discount up to INR 1250 on HDFC Bank Credit Card\"."
        warn = f" ({stats.warnings[0]})" if stats.warnings else ""
        return f"Offer saved{warn}.\n\n" + summary(self.db, asin, now)

    def _list(self, now: datetime) -> str:
        rows = repo.watchlist_rows(self.db)
        if not rows:
            return "Your watchlist is empty. Share a product from the Amazon app to me."
        out = []
        for w in rows[:25]:
            ev = service.evaluate(self.db, w["asin"], now)
            if ev.result is None:
                out.append(f"• {w['title'][:40] or w['asin']}: no price yet ({w['asin']})")
                continue
            best = ev.result.best_confirmed or ev.result.baseline
            flag = {"buy_now": " ✅", "wait": " ⏳"}.get(ev.advice.action if ev.advice else "", "")
            out.append(f"• {w['title'][:40] or w['asin']}: {fmt(best.cash_cost)}{flag} ({w['asin']})")
        return "Watchlist\n" + "\n".join(out)

    def _cards(self, now: datetime) -> str:
        from .insights import card_report

        r = card_report(self.db, now)
        lines = [f"Across {r.products_evaluated} watched product(s):"]
        for c in r.owned:
            lines.append(f"• {c.label}: best on {c.products_best}, saves {fmt(c.savings_where_best)}")
        if r.missing:
            m = r.missing[0]
            lines.append(f"A {m.label} would have saved {fmt(m.extra_savings)} more on {m.products} product(s).")
        if not r.owned:
            lines.append("Add your cards (bank + type only) in the dashboard Settings.")
        return "\n".join(lines)

    def _status(self, now: datetime) -> str:
        bad = self.db.query("SELECT name, disabled_reason, last_error FROM sources WHERE enabled=0 OR consecutive_failures>0")
        n_watch = self.db.one("SELECT COUNT(*) FROM watchlist")[0]
        failed = self.db.one("SELECT COUNT(*) FROM alerts WHERE status='failed'")[0]
        lines = [f"OK: {n_watch} watched, {self.db.one('SELECT COUNT(*) FROM offers')[0]} offers stored"]
        lines += [f"⚠ {b['name']}: {b['disabled_reason'] or b['last_error']}" for b in bad]
        if failed:
            lines.append(f"⚠ {failed} alert(s) failed to deliver")
        return "\n".join(lines)

    # -- polling loop ---------------------------------------------------------------

    def poll_once(self, timeout: int = 25) -> int:
        offset = int(repo.get_settings(self.db).get("telegram_offset") or 0)
        updates = self.api.get_updates(offset, timeout=timeout)
        for u in sorted(updates, key=lambda x: x.get("update_id", 0)):
            uid = int(u.get("update_id", 0))
            try:
                reply = self.handle(u)
                if reply:
                    self.api.send(reply, chat_id=self.chat_id)
            except NotifyError as e:
                log.warning("bot reply failed", extra={"fields": {"error": str(e)}})
            except Exception:
                log.exception("bot handler crashed")  # never let one message wedge the queue
            repo.set_setting(self.db, "telegram_offset", uid + 1)
        return len(updates)

    def run(self) -> None:
        delay = 5
        while not self._stop.is_set():
            try:
                self.poll_once()
                delay = 5
            except NotifyError as e:
                if e.permanent:
                    log.error("telegram bot disabled: %s", e)
                    return
                self._stop.wait(e.retry_after_s or delay)
                delay = min(delay * 2, 300)
            except Exception:
                log.exception("bot loop error")
                self._stop.wait(delay)
                delay = min(delay * 2, 300)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="couponhunter-bot", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
