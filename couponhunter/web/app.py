"""HTTP application: JSON API for the extension + server-rendered dashboard.

The App is transport-independent (handle() takes a Request, returns a
Response) so it is tested without sockets; server.py adapts it to
http.server. Security controls applied to every request:

  * Host header must be the loopback address we bound (DNS-rebinding defence);
  * API calls need the X-CouponHunter-Token header (constant-time compare);
  * dashboard needs a session cookie derived from the token (HttpOnly,
    SameSite=Strict) and every POST carries a CSRF token;
  * request bodies are size-capped; JSON is validated field by field;
  * responses carry a no-inline-script CSP and nosniff.
"""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from .. import API_VERSION, __version__, ingest, repo, service
from ..amazon import InvalidAmazonURL, extract_asin, normalize_asin
from ..config import derive
from ..db import Database, iso, utcnow
from ..domain import Authenticity
from ..money import parse_inr
from ..sources import FetchResult, SourceRecord
from . import pages

MAX_BODY = 64 * 1024
STATIC = Path(__file__).with_name("static")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)  # lower-case keys
    body: bytes = b""

    def q(self, name: str, default: str = "") -> str:
        return (self.query.get(name) or [default])[0]

    def json(self) -> dict[str, Any]:
        try:
            v = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HttpError(400, "invalid JSON") from e
        if not isinstance(v, dict):
            raise HttpError(400, "JSON body must be an object")
        return v

    def form(self) -> dict[str, str]:
        try:
            parsed = parse_qs(self.body.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
        except (UnicodeDecodeError, ValueError) as e:
            raise HttpError(400, "invalid form body") from e
        return {k: v[0] for k, v in parsed.items()}

    def cookie(self, name: str) -> str | None:
        for part in self.headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False, default=str).encode(),
                    {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})


def html_response(text: str, status: int = 200, extra: dict[str, str] | None = None) -> Response:
    return Response(status, text.encode(), {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                                            **(extra or {})})


def redirect(to: str, flash: str | None = None) -> Response:
    from urllib.parse import quote
    loc = to + (("&" if "?" in to else "?") + "flash=" + quote(flash[:200]) if flash else "")
    return Response(303, b"", {"Location": loc})


def _int_paise(v: Any, name: str, required: bool = False) -> int | None:
    if v is None or v == "":
        if required:
            raise HttpError(400, f"{name} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise HttpError(400, f"{name} must be integer paise")
    if v <= 0 or v > 10**11:
        raise HttpError(400, f"{name} out of range")
    return v


def _form_rupees(v: str, name: str) -> int | None:
    if not v.strip():
        return None
    try:
        p = parse_inr(v)
    except ValueError as e:
        raise HttpError(400, f"{name}: {e}") from e
    if p <= 0:
        raise HttpError(400, f"{name} must be positive")
    return p


class App:
    def __init__(self, db: Database, token: str, port: int, clock: Callable[[], datetime] = utcnow):
        self.db = db
        self.token = token
        self.port = port
        self.clock = clock
        self.csrf = derive(token, "csrf")
        self.session = derive(token, "session")
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        ingest.ensure_builtin_sources(db, clock())

    # -- dispatch -------------------------------------------------------------

    def handle(self, req: Request) -> Response:
        try:
            resp = self._dispatch(req)
        except HttpError as e:
            if req.path.startswith("/api/"):
                resp = json_response({"error": e.message}, e.status)
            else:
                resp = html_response(pages.error(e.status, e.message), e.status)
        except (repo.NotFound, InvalidAmazonURL, KeyError) as e:
            msg = str(e) if isinstance(e, InvalidAmazonURL) else "not found"
            status = 400 if isinstance(e, InvalidAmazonURL) else 404
            resp = json_response({"error": msg}, status) if req.path.startswith("/api/") else html_response(
                pages.error(status, msg), status)
        except (ValueError, repo.InvalidTransition) as e:
            resp = json_response({"error": str(e)[:300]}, 400) if req.path.startswith("/api/") else html_response(
                pages.error(400, str(e)[:300]), 400)
        resp.headers.update(SECURITY_HEADERS)
        return resp

    def _dispatch(self, req: Request) -> Response:
        if req.headers.get("host", "") not in self.allowed_hosts:
            raise HttpError(421, "unexpected Host header")
        if len(req.body) > MAX_BODY:
            raise HttpError(413, "request too large")
        p, m = req.path, req.method
        if p == "/healthz":
            return json_response({"ok": True})
        if p == "/static/app.css" and m == "GET":
            return Response(200, (STATIC / "app.css").read_bytes(),
                            {"Content-Type": "text/css; charset=utf-8", "Cache-Control": "max-age=300"})
        if p.startswith("/api/"):
            self._require_token(req)
            return self._api(req)
        if p == "/login":
            return self._login(req)
        if not self._has_session(req):
            return redirect("/login")
        if m == "POST":
            self._require_csrf(req)
            return self._action(req)
        if m != "GET":
            raise HttpError(405, "method not allowed")
        return self._page(req)

    # -- auth ---------------------------------------------------------------------

    def _require_token(self, req: Request) -> None:
        got = req.headers.get("x-couponhunter-token", "")
        if not hmac.compare_digest(got.encode(), self.token.encode()):
            raise HttpError(401, "missing or invalid API token")

    def _has_session(self, req: Request) -> bool:
        c = req.cookie("ch_session") or ""
        return hmac.compare_digest(c.encode(), self.session.encode())

    def _require_csrf(self, req: Request) -> None:
        origin = req.headers.get("origin")
        if origin and origin.replace("http://", "") not in self.allowed_hosts:
            raise HttpError(403, "cross-origin form post refused")
        if not hmac.compare_digest(req.form().get("csrf", "").encode(), self.csrf.encode()):
            raise HttpError(403, "invalid CSRF token")

    def _login(self, req: Request) -> Response:
        if req.method == "POST":
            given = req.form().get("token", "").strip()
            if hmac.compare_digest(given.encode(), self.token.encode()):
                return Response(303, b"", {
                    "Location": "/",
                    "Set-Cookie": f"ch_session={self.session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000",
                })
            return html_response(pages.login("That token didn't match."), 401)
        return html_response(pages.login())

    # -- JSON API -------------------------------------------------------------------

    def _api(self, req: Request) -> Response:
        now = self.clock()
        parts = [x for x in req.path.split("/") if x]  # ["api", "v1", ...]
        if len(parts) < 3 or parts[1] != f"v{API_VERSION}":
            raise HttpError(404, f"unknown API path; this backend speaks v{API_VERSION}")
        res, rest, m = parts[2], parts[3:], req.method
        if res == "status" and m == "GET":
            return json_response(self.status(now))
        if res == "products" and m == "GET" and len(rest) == 1:
            asin = normalize_asin(rest[0])
            try:
                ev = service.evaluate(self.db, asin, now)
            except repo.NotFound:
                return json_response({"asin": asin, "tracked": False}, 404)
            out = service.evaluation_json(ev, self.db, now)
            out["tracked"] = self.db.one("SELECT 1 FROM watchlist w JOIN products p ON p.id=w.product_id WHERE p.asin=?",
                                         (asin,)) is not None
            return json_response(out)
        if res == "products" and m == "POST" and not rest:
            body = req.json()
            asin = extract_asin(str(body.get("url") or body.get("asin") or ""))
            pid = repo.upsert_product(self.db, asin, now, title=str(body.get("title") or "")[:300])
            repo.watch(self.db, pid, now, target_price=_int_paise(body.get("target_price"), "target_price"))
            return json_response({"asin": asin, "tracked": True}, 201)
        if res == "products" and m == "DELETE" and len(rest) == 1:
            repo.unwatch(self.db, repo.get_product(self.db, rest[0])["id"])
            return json_response({"asin": normalize_asin(rest[0]), "tracked": False})
        if res == "captures" and m == "POST" and not rest:
            return json_response(self.capture(req.json(), now), 201)
        if res == "redemptions" and m == "POST" and not rest:
            return json_response(self.redemption(req.json(), now), 201)
        if res == "offers" and m == "GET" and not rest:
            return json_response({"offers": [service.offer_json(o, a) for o, a in repo.all_offers(self.db, now)]})
        raise HttpError(404, "unknown API endpoint")

    def capture(self, body: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Record what the user's own browser showed on an amazon.in product page.

        Only called when the user clicks "Save" in the extension panel (or
        has opted in to automatic capture). Holds no cookies or account data.
        """
        asin = normalize_asin(str(body.get("asin", "")))
        price = _int_paise(body.get("price"), "price", required=True)
        list_price = _int_paise(body.get("list_price"), "list_price")
        title = str(body.get("title") or "")[:300]
        seller = (str(body["seller"])[:120] if body.get("seller") else None)
        offers_in = body.get("offers") or []
        if not isinstance(offers_in, list) or len(offers_in) > 30:
            raise HttpError(400, "offers must be a list of at most 30 items")
        records = []
        for item in offers_in:
            if isinstance(item, str):
                data: dict[str, Any] = {"text": item[:2000], "asins": [asin]}
            elif isinstance(item, dict) and item.get("kind") == "deal_price":
                data = {"kind": "deal_price", "title": str(item.get("title") or "Deal")[:200], "asins": [asin],
                        "valid_until": item.get("valid_until")}
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                data = {"text": item["text"][:2000], "asins": [asin]}
            else:
                continue
            records.append(SourceRecord(data=data, evidence_type="user_observed", authenticity=Authenticity.OFFICIAL,
                                        source_url=f"https://www.amazon.in/dp/{asin}", raw_text=str(data.get("text") or data.get("title"))))
        pid = repo.upsert_product(self.db, asin, now, title=title, data_origin="live", seller=seller)
        new_price = repo.record_price(self.db, pid, price, now, "extension", "live", list_price=list_price, seller=seller,
                                      in_stock=body.get("in_stock") if isinstance(body.get("in_stock"), bool) else None)
        stats = ingest.RunStats("extension")
        with self.db.tx() as c:
            ingest.ingest_result(
                c, "extension",
                FetchResult(records, snapshot=bool(body.get("complete", True)), data_origin="live", snapshot_asin=asin),
                now, stats, frozenset(repo.get_settings(self.db).get("extra_redemption_domains", [])),
            )
            c.execute("UPDATE sources SET last_success_at=?, last_fetch_at=? WHERE id='extension'", (iso(now), iso(now)))
        if body.get("watch"):
            repo.watch(self.db, pid, now)
        ev = service.evaluate(self.db, asin, now)
        out = service.evaluation_json(ev, self.db, now)
        out["capture"] = {"price_recorded": new_price, "offers_created": stats.created, "offers_updated": stats.updated,
                          "offers_unparsed": stats.rejected, "messages": (stats.errors + stats.warnings)[:10]}
        return out

    def redemption(self, body: dict[str, Any], now: datetime) -> dict[str, Any]:
        outcome = body.get("outcome")
        if outcome not in ("applied", "failed", "not_shown", "partial"):
            raise HttpError(400, "outcome must be applied, failed, not_shown or partial")
        offer_id = body.get("offer_id")
        if not isinstance(offer_id, int):
            raise HttpError(400, "offer_id must be an integer")
        pid = None
        if body.get("asin"):
            pid = repo.get_product(self.db, str(body["asin"]))["id"]
        key = str(body.get("idempotency_key") or secrets.token_hex(8))[:64]
        rid = repo.record_redemption(
            self.db, offer_id, pid, outcome, now, failure_reason=str(body.get("failure_reason") or "")[:300] or None,
            observed_savings=_int_paise(body.get("observed_savings"), "observed_savings"),
            snapshot={"client": "extension"}, channel=str(body.get("channel") or "extension")[:20], idempotency_key=key,
        )
        return {"id": rid}

    def status(self, now: datetime) -> dict[str, Any]:
        sources = [dict(r) for r in self.db.query(
            "SELECT id, name, adapter, enabled, consecutive_failures, disabled_reason, last_success_at, last_error, "
            "last_error_at, next_fetch_at FROM sources ORDER BY id")]
        jobs = [dict(r) for r in self.db.query(
            "SELECT job, MAX(started_at) last_started, status FROM job_runs GROUP BY job")]
        outbox = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) n FROM alerts GROUP BY status")}
        return {"version": __version__, "api_version": API_VERSION, "now": iso(now), "sources": sources, "jobs": jobs,
                "alerts": outbox, "offers": self.db.one("SELECT COUNT(*) FROM offers")[0]}

    # -- dashboard ----------------------------------------------------------------

    def _page(self, req: Request) -> Response:
        now = self.clock()
        flash = req.q("flash") or None
        ctx = pages.Ctx(self.db, now, self.csrf, flash)
        p = req.path
        if p == "/":
            return html_response(pages.overview(ctx))
        if p == "/watchlist":
            return html_response(pages.watchlist(ctx))
        if p == "/offers":
            return html_response(pages.offers(ctx, include_expired=req.q("expired") == "1"))
        if p.startswith("/product/"):
            return html_response(pages.product(ctx, normalize_asin(p.split("/")[2])))
        if p == "/history":
            return html_response(pages.history(ctx))
        if p == "/cards":
            return html_response(pages.cards(ctx))
        if p == "/health":
            return html_response(pages.health(ctx, self.status(now)))
        if p == "/settings":
            return html_response(pages.settings(ctx, self.port))
        raise HttpError(404, "page not found")

    def _action(self, req: Request) -> Response:
        now = self.clock()
        f = req.form()
        a = req.path
        if a == "/actions/watch":
            asin = extract_asin(f.get("url", ""))
            pid = repo.upsert_product(self.db, asin, now, title=f.get("title", "")[:300])
            repo.watch(self.db, pid, now, target_price=_form_rupees(f.get("target", ""), "target price"))
            price = _form_rupees(f.get("price", ""), "price")
            if price:
                repo.record_price(self.db, pid, price, now, "manual", "manual")
            return redirect(f"/product/{asin}", f"Watching {asin}")
        if a == "/actions/unwatch":
            repo.unwatch(self.db, repo.get_product(self.db, f.get("asin", ""))["id"])
            return redirect("/watchlist", "Removed from watchlist")
        if a == "/actions/price":
            p = repo.get_product(self.db, f.get("asin", ""))
            price = _form_rupees(f.get("price", ""), "price")
            if price is None:
                raise HttpError(400, "price is required")
            repo.record_price(self.db, p["id"], price, now, "manual", "manual",
                              list_price=_form_rupees(f.get("list_price", ""), "list price"))
            return redirect(f"/product/{p['asin']}", "Price recorded")
        if a == "/actions/offer-text":
            return self._manual_offer(f, now)
        if a == "/actions/flag":
            repo.flag_offer(self.db, int(f.get("offer_id", "0")), f.get("flag") or None)
            return redirect(f.get("back") if f.get("back", "").startswith("/") else "/offers", "Offer updated")
        if a == "/actions/redeem":
            p = repo.get_product(self.db, f.get("asin", ""))
            outcome = f.get("outcome", "")
            if outcome not in ("applied", "failed", "not_shown", "partial"):
                raise HttpError(400, "bad outcome")
            repo.record_redemption(self.db, int(f.get("offer_id", "0")), p["id"], outcome, now,
                                   failure_reason=f.get("reason") or None,
                                   idempotency_key=f.get("idem") or None)
            return redirect(f"/product/{p['asin']}", "Thanks: recorded")
        if a == "/actions/purchase":
            p = repo.get_product(self.db, f.get("asin", "")) if f.get("asin") else None
            ref = _form_rupees(f.get("reference_price", ""), "reference price")
            paid = _form_rupees(f.get("checkout_paid", ""), "amount paid")
            if ref is None or paid is None:
                raise HttpError(400, "reference price and amount paid are required")
            cash = _form_rupees(f.get("cash_paid", ""), "cash paid")
            purchase = repo.record_purchase(self.db, p["id"] if p else None, now, ref, paid, cash,
                                            notes=f.get("notes", ""), idempotency_key=f.get("idem") or None)
            cb = _form_rupees(f.get("cashback", ""), "expected cashback")
            if cb:
                repo.create_cashback(self.db, f.get("cashback_provider") or "unknown", now, advertised=cb,
                                     purchase_id=purchase)
            return redirect("/history", "Purchase recorded")
        if a == "/actions/cashback":
            amount = _form_rupees(f.get("amount", ""), "amount") or 0
            repo.transition_cashback(self.db, int(f.get("record_id", "0")), f.get("status", ""), amount, now,
                                     note=f.get("note", ""), expected_version_status=f.get("from") or None)
            return redirect("/history", "Cashback updated")
        if a == "/actions/settings":
            return self._save_settings(f)
        raise HttpError(404, "unknown action")

    def _manual_offer(self, f: dict[str, str], now: datetime) -> Response:
        text = f.get("text", "").strip()
        if not text:
            raise HttpError(400, "offer text is required")
        data: dict[str, Any] = {"text": text}
        if f.get("asin"):
            data["asins"] = [normalize_asin(f["asin"])]
        if f.get("code"):
            data["code"] = f["code"]
        if f.get("valid_until"):
            data["valid_until"] = f["valid_until"]
        if f.get("basis"):
            data["basis"] = f["basis"]
        if f.get("url"):
            data["redemption_url"] = f["url"]
        evidence = f.get("evidence") if f.get("evidence") in ("official_page", "user_entered") else "user_entered"
        auth = Authenticity.OFFICIAL if evidence == "official_page" else Authenticity.UNVERIFIED
        stats = ingest.RunStats("manual")
        with self.db.tx() as c:
            ingest.ingest_result(c, "manual", FetchResult(
                [SourceRecord(data, evidence, auth, raw_text=text)], snapshot=False, data_origin="manual"), now, stats,
                frozenset(repo.get_settings(self.db).get("extra_redemption_domains", [])))
        if stats.rejected:
            return redirect("/offers", "Couldn't understand that offer: " + "; ".join(stats.errors)[:150])
        back = f"/product/{data['asins'][0]}" if data.get("asins") else "/offers"
        msg = "Offer saved" + (f" ({'; '.join(stats.warnings)[:120]})" if stats.warnings else "")
        return redirect(back, msg)

    def _save_settings(self, f: dict[str, str]) -> Response:
        def tri(v: str) -> bool | None:
            return {"yes": True, "no": False}.get(v)

        repo.set_setting(self.db, "profile", {"is_prime": tri(f.get("is_prime", "")),
                                              "is_new_customer": tri(f.get("is_new_customer", ""))})
        try:
            wallet = json.loads(f.get("wallet", "[]") or "[]")
            stacking = json.loads(f.get("stacking_defaults", "{}") or "{}")
        except json.JSONDecodeError as e:
            raise HttpError(400, f"invalid JSON: {e}") from e
        repo.set_setting(self.db, "wallet", wallet)
        if not isinstance(stacking, dict) or any(v not in ("yes", "no", "unknown") for v in stacking.values()):
            raise HttpError(400, "stacking_defaults values must be yes/no/unknown")
        repo.set_setting(self.db, "stacking_defaults", stacking)
        min_s = _form_rupees(f.get("min_savings", ""), "minimum savings")
        if min_s is not None:
            repo.set_setting(self.db, "min_savings_paise", min_s)
        for key in ("cooldown_hours", "price_fresh_hours", "min_savings_pct_bp"):
            if f.get(key, "").strip():
                repo.set_setting(self.db, key, int(f[key]))
        qs, qe = f.get("quiet_start", "").strip(), f.get("quiet_end", "").strip()
        if qs and qe:
            datetime.strptime(qs, "%H:%M"), datetime.strptime(qe, "%H:%M")
            repo.set_setting(self.db, "quiet_hours", {"start": qs, "end": qe, "utc_offset_minutes": 330})
        repo.set_setting(self.db, "include_hypothetical_instruments", f.get("hypothetical") == "on")
        return redirect("/settings", "Settings saved")


__all__ = ["App", "Request", "Response", "HttpError"]
