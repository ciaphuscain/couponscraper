"""Performance benchmarks. Run: python -m benchmarks.bench [--quick]

Writes benchmarks/results/bench-<host>.json and prints a Markdown table.
All data is synthetic and generated here; nothing touches the network.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from couponhunter import alerts, ingest, repo, service  # noqa: E402
from couponhunter.db import open_db  # noqa: E402
from couponhunter.domain import Authenticity, Basis, DiscountType, Offer, OfferKind, PaymentInstrument, PaymentRequirement, ProductContext, UserProfile  # noqa: E402
from couponhunter.normalize import normalize_record  # noqa: E402
from couponhunter.optimizer import optimize  # noqa: E402
from couponhunter.sources import FetchResult, SourceRecord  # noqa: E402
from couponhunter.web.app import App, Request  # noqa: E402

NOW = datetime(2026, 10, 25, 8, 30, tzinfo=timezone.utc)
BANKS = ["hdfc", "icici", "sbi", "axis", "kotak"]
rng = random.Random(42)


def pctl(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def timed(fn, n: int) -> dict:
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    return {"n": n, "p50_ms": round(statistics.median(samples), 3), "p95_ms": round(pctl(samples, 0.95), 3),
            "max_ms": round(max(samples), 3)}


def gen_record(i: int, asin_pool: list[str]) -> dict:
    k = rng.choice(["product_coupon", "promo_code", "bank_instant", "bank_cashback", "portal_cashback"])
    rec = {"kind": k, "title": f"offer {i}", "valid_until": "2027-06-30"}
    if k in ("bank_instant", "bank_cashback"):
        rec |= {"discount": f"{rng.choice([5, 7.5, 10])}%", "max_discount": f"₹{rng.choice([500, 750, 1000, 1500])}",
                "payment": {"banks": [rng.choice(BANKS)], "types": ["credit_card"]}, "min_order": "₹2000"}
    elif k == "portal_cashback":
        rec |= {"discount": f"{rng.choice([2, 3, 5])}%", "max_discount": "₹300", "provider": f"portal{i % 4}"}
    else:
        rec |= {"discount": f"₹{rng.choice([50, 100, 200, 500])}", "asins": [rng.choice(asin_pool)]}
        if k == "promo_code":
            rec["code"] = f"CODE{i}"
    return rec


def gen_offers(n: int) -> list[Offer]:
    out = []
    kinds = [OfferKind.PRODUCT_COUPON, OfferKind.PROMO_CODE, OfferKind.BANK_INSTANT, OfferKind.BANK_CASHBACK,
             OfferKind.PORTAL_CASHBACK, OfferKind.AMAZON_PAY_CASHBACK]
    for i in range(n):
        k = kinds[i % len(kinds)]
        pay = PaymentRequirement(banks=frozenset({BANKS[i % 3]})) if k in (OfferKind.BANK_INSTANT, OfferKind.BANK_CASHBACK) else None
        pct = k not in (OfferKind.PRODUCT_COUPON, OfferKind.PROMO_CODE)
        out.append(Offer(
            id=str(i), kind=k, title=f"o{i}", discount_type=DiscountType.PERCENT if pct else DiscountType.FLAT,
            value=rng.choice([300, 500, 1000]) if pct else rng.choice([100, 300, 500]) * 100,
            max_discount=rng.choice([500, 750, 1500]) * 100 if pct else None, payment=pay,
            basis=rng.choice([Basis.UNKNOWN, Basis.PRICE, Basis.AFTER_COUPONS if not k.value.endswith("cashback") else Basis.PAYABLE]),
            authenticity=Authenticity.OFFICIAL, valid_until=NOW + timedelta(days=5)))
    return out


def bench(quick: bool) -> dict:
    scale = 1 if quick else 5
    res: dict = {}
    asins = [f"B0BENCH{i:03d}" for i in range(200)]

    recs = [gen_record(i, asins) for i in range(2_000 * scale)]
    t = time.perf_counter()
    for r in recs:
        normalize_record(r)
    res["normalize_records_per_s"] = round(len(recs) / (time.perf_counter() - t))

    with tempfile.TemporaryDirectory() as d:
        db = open_db(Path(d) / "b.db")
        ingest.register_source(db, "bench", "file_import", "bench", {}, NOW, min_interval_s=0)

        class A:
            adapter, requires_terms_review = "x", False

            def fetch(self, cfg):
                return FetchResult([SourceRecord(dict(r), "fixture", Authenticity.SOURCE_LISTED) for r in recs],
                                   snapshot=True, data_origin="synthetic")

        t = time.perf_counter()
        s1 = ingest.run_source(db, "bench", NOW, A())
        first = time.perf_counter() - t
        t = time.perf_counter()
        s2 = ingest.run_source(db, "bench", NOW + timedelta(minutes=1), A())
        replay = time.perf_counter() - t
        res["ingest_first_run"] = {"records": len(recs), "created": s1.created, "dup_in_listing": s1.duplicates,
                                   "seconds": round(first, 3), "records_per_s": round(len(recs) / first)}
        res["ingest_replay_dedup"] = {"created": s2.created, "duplicates": s2.duplicates, "seconds": round(replay, 3),
                                      "offer_rows_after": db.one("SELECT COUNT(*) FROM offers")[0]}

        # Product with long price history + watchlist of 100
        repo.set_setting(db, "wallet", [{"id": b, "label": b, "type": "credit_card", "bank": b} for b in BANKS[:3]])
        for i, a in enumerate(asins[:100]):
            pid = repo.upsert_product(db, a, NOW, title=f"P{i}")
            repo.watch(db, pid, NOW)
        pid0 = repo.get_product(db, asins[0])["id"]
        with db.tx() as c:
            for j in range(10_000):
                c.execute("INSERT INTO price_observations(product_id, price, observed_at, source, data_origin) VALUES (?,?,?,?,?)",
                          (pid0, 9_000_00 + rng.randint(-50_000, 50_000), (NOW - timedelta(minutes=10 * j)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "bench", "synthetic"))
            for a in asins[1:100]:
                pid = repo.get_product(db, a)["id"]
                c.execute("INSERT INTO price_observations(product_id, price, observed_at, source, data_origin) VALUES (?,?,?,?,?)",
                          (pid, 5_000_00, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "bench", "synthetic"))
        n_offers = len(repo.offers_for_asin(db, asins[0], NOW))
        res["offers_for_asin_lookup"] = {"candidate_offers": n_offers, **timed(lambda: repo.offers_for_asin(db, asins[0], NOW), 50)}
        res["price_history_query_10k_rows"] = timed(lambda: repo.price_history(db, pid0), 30)
        res["evaluate_product_10k_history"] = timed(lambda: service.evaluate(db, asins[0], NOW), 30)
        app = App(db, "t" * 43, 8765, clock=lambda: NOW)
        req = Request("GET", f"/api/v1/products/{asins[1]}", {}, {"host": "127.0.0.1:8765", "x-couponhunter-token": "t" * 43})
        res["api_product_lookup"] = {"candidate_offers": len(repo.offers_for_asin(db, asins[1], NOW)), **timed(lambda: app.handle(req), 50)}
        cookie = {"host": "127.0.0.1:8765", "cookie": f"ch_session={app.session}"}
        res["stress_watchlist_100_products_cold"] = timed(lambda: (service._CACHE.clear(), app.handle(Request("GET", "/watchlist", {}, cookie))), 2)
        res["stress_watchlist_100_products_warm"] = timed(lambda: app.handle(Request("GET", "/watchlist", {}, cookie)), 5)
        service._CACHE.clear()
        t = time.perf_counter()
        decisions = alerts.decide(db, NOW)
        res["stress_alert_decide_100_watched"] = {"seconds": round(time.perf_counter() - t, 3), "decisions": len(decisions)}
        res["db_size_mb"] = round(os.path.getsize(Path(d) / "b.db") / 1e6, 2)
        db.close()

    res["realistic"] = realistic()
    user = UserProfile(instruments=tuple(PaymentInstrument(b, b, "credit_card", bank=b) for b in BANKS[:3]))
    prod = ProductContext("B0BENCH000", 10_000_00)
    for n in (6, 12, 24, 60):
        offers = gen_offers(n)
        r = optimize(prod, offers, user, NOW)
        res[f"optimize_{n}_offers"] = {"scenarios": len(r.scenarios), **timed(lambda: optimize(prod, offers, user, NOW), 20 if n < 60 else 5)}
    return res


def realistic() -> dict:
    """Closer to real use: 100 watched products, 25 sitewide bank/cashback offers,
    2 product-specific offers each, 2 cards in the wallet, 60 price observations each."""
    out: dict = {}
    with tempfile.TemporaryDirectory() as d:
        db = open_db(Path(d) / "r.db")
        ingest.register_source(db, "r", "file_import", "r", {}, NOW, min_interval_s=0)
        asins = [f"B0REAL{i:04d}" for i in range(100)]
        recs = []
        for i in range(25):
            k = rng.choice(["bank_instant", "bank_cashback", "portal_cashback", "amazon_pay_cashback"])
            r = {"kind": k, "title": f"site {i}", "discount": f"{rng.choice([3, 5, 10])}%", "max_discount": "₹750",
                 "provider": f"prov{i}", "valid_until": "2027-06-30", "min_order": f"₹{rng.choice([1000, 5000])}"}
            if k.startswith("bank"):
                r["payment"] = {"banks": [rng.choice(BANKS)], "types": ["credit_card"]}
            recs.append(r)
        for a in asins:
            recs.append({"kind": "product_coupon", "title": "c", "discount": f"₹{rng.choice([100, 200, 500])}",
                         "asins": [a], "basis": "price", "valid_until": "2027-06-30"})
            recs.append({"kind": "promo_code", "title": "p", "discount": "5%", "max_discount": "₹200", "code": f"P{a}",
                         "asins": [a], "valid_until": "2027-06-30"})

        class A:
            adapter, requires_terms_review = "x", False

            def fetch(self, cfg):
                return FetchResult([SourceRecord(dict(r), "fixture", Authenticity.SOURCE_LISTED) for r in recs],
                                   snapshot=True, data_origin="synthetic")

        ingest.run_source(db, "r", NOW, A())
        repo.set_setting(db, "wallet", [{"id": b, "label": b, "type": "credit_card", "bank": b} for b in BANKS[:2]])
        with db.tx() as c:
            for i, a in enumerate(asins):
                pid = c.execute("INSERT INTO products(asin, title, created_at, data_origin) VALUES (?,?,?,?)",
                                (a, f"P{i}", "2026-01-01T00:00:00Z", "synthetic")).lastrowid
                c.execute("INSERT INTO watchlist(product_id, added_at) VALUES (?,?)", (pid, "2026-01-01T00:00:00Z"))
                for j in range(60):
                    c.execute("INSERT INTO price_observations(product_id, price, observed_at, source, data_origin) VALUES (?,?,?,?,?)",
                              (pid, 5_000_00 + rng.randint(-50_000, 50_000),
                               (NOW - timedelta(hours=12 * j)).strftime("%Y-%m-%dT%H:%M:%SZ"), "bench", "synthetic"))
        out["candidate_offers_per_product"] = len(repo.offers_for_asin(db, asins[0], NOW))
        app = App(db, "t" * 43, 8765, clock=lambda: NOW)
        api = Request("GET", f"/api/v1/products/{asins[3]}", {}, {"host": "127.0.0.1:8765", "x-couponhunter-token": "t" * 43})
        out["api_product_lookup_cold"] = timed(lambda: (service._CACHE.clear(), app.handle(api)), 50)
        out["api_product_lookup_warm"] = timed(lambda: app.handle(api), 50)
        cookie = {"host": "127.0.0.1:8765", "cookie": f"ch_session={app.session}"}
        out["watchlist_page_cold"] = timed(lambda: (service._CACHE.clear(), app.handle(Request("GET", "/watchlist", {}, cookie))), 3)
        out["watchlist_page_warm"] = timed(lambda: app.handle(Request("GET", "/watchlist", {}, cookie)), 10)
        out["overview_page_warm"] = timed(lambda: app.handle(Request("GET", "/", {}, cookie)), 10)
        service._CACHE.clear()
        t = time.perf_counter()
        n = len(alerts.decide(db, NOW))
        out["alert_decide_cold"] = {"seconds": round(time.perf_counter() - t, 3), "decisions": n}
        db.close()
    return out


def memory_soak(cycles: int) -> dict:
    """Experiment G (compressed): repeated ingest + alert cycles on a simulated clock."""
    with tempfile.TemporaryDirectory() as d:
        db = open_db(Path(d) / "m.db")
        ingest.register_source(db, "s", "file_import", "s", {}, NOW, min_interval_s=0)
        asins = [f"B0SOAK{i:04d}" for i in range(20)]
        for a in asins:
            repo.watch(db, repo.upsert_product(db, a, NOW, title=a), NOW)
        recs = [gen_record(i, asins) for i in range(100)]

        class A:
            adapter, requires_terms_review = "x", False

            def fetch(self, cfg):
                return FetchResult([SourceRecord(dict(r), "fixture", Authenticity.SOURCE_LISTED) for r in recs],
                                   snapshot=True, data_origin="synthetic")

        class Sink:
            name = "sink"
            n = 0

            def send(self, text):
                Sink.n += 1

        tracemalloc.start()
        snaps = []
        t = NOW
        for i in range(cycles):
            t += timedelta(minutes=15)
            for a in asins[:5]:
                repo.record_price(db, repo.get_product(db, a)["id"], 5_000_00 + rng.randint(-20_000, 20_000), t, "soak", "synthetic")
            ingest.run_source(db, "s", t, A())
            alerts.run_alerts(db, Sink(), t)
            if i % max(cycles // 10, 1) == 0:
                snaps.append(tracemalloc.get_traced_memory()[0])
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        out = {
            "cycles": cycles, "simulated_days": round(cycles * 15 / 1440, 1),
            "traced_mem_kb_samples": [s // 1024 for s in snaps], "traced_peak_kb": peak // 1024,
            "offer_rows": db.one("SELECT COUNT(*) FROM offers")[0],
            "distinct_fingerprints": db.one("SELECT COUNT(DISTINCT fingerprint) FROM offers")[0],
            "alerts_rows": db.one("SELECT COUNT(*) FROM alerts")[0],
            "duplicate_alert_keys": db.one("SELECT COUNT(*) - COUNT(DISTINCT dedup_key) FROM alerts")[0],
            "notifications_sent": Sink.n,
        }
        db.close()
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    env = {"python": platform.python_version(), "sqlite": __import__("sqlite3").sqlite_version, "os": platform.platform(),
           "cpu_count": os.cpu_count(), "machine": platform.machine(), "date": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    out = {"env": env, "bench": bench(a.quick), "soak": memory_soak(200 if a.quick else 1000)}
    dest = ROOT / "benchmarks" / "results"
    dest.mkdir(exist_ok=True)
    (dest / f"bench-{platform.node() or 'host'}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
