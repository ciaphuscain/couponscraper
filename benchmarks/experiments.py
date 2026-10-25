"""Engineering experiments B, C, D and F (see docs/experiments.md).

Run: python -m benchmarks.experiments
All inputs are synthetic or author-written and labelled as such; results
describe the system's behaviour on those inputs, not real-world accuracy.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from couponhunter import alerts, ingest, repo, service  # noqa: E402
from couponhunter.db import open_db  # noqa: E402
from couponhunter.domain import (  # noqa: E402
    Authenticity, Basis, DiscountType, Offer, OfferKind, PaymentInstrument, PaymentRequirement, ProductContext, Tri,
    UserProfile,
)
from couponhunter.money import rupees  # noqa: E402
from couponhunter.normalize import NormalizationError, normalize_record  # noqa: E402
from couponhunter.optimizer import optimize, rank_by  # noqa: E402
from couponhunter.pricing import Observation, compute_stats, judge_price  # noqa: E402
from couponhunter.sources import FetchResult, SourceError, SourceRecord  # noqa: E402

NOW = datetime(2026, 10, 25, 8, 30, tzinfo=timezone.utc)

# --- Experiment B: offer-text parsing accuracy ----------------------------------
# Author-written phrasings modelled on amazon.in offer wording. NOT a sample of
# live pages; real accuracy must be re-measured on captured texts (docs).
LABELLED = [
    ("10% Instant Discount up to INR 1250 on HDFC Bank Credit Card Non-EMI Trxn. Minimum purchase value INR 5000",
     {"kind": "bank_instant", "dt": "percent", "value": 1000, "max": 125000, "min": 500000, "banks": ["hdfc"]}),
    ("Flat INR 500 Instant Discount on ICICI Bank Credit Cards Transactions. Minimum purchase value INR 7500",
     {"kind": "bank_instant", "dt": "flat", "value": 50000, "max": None, "min": 750000, "banks": ["icici"]}),
    ("7.5% Instant Discount up to INR 1000 on SBI Credit Card EMI Transactions. Minimum purchase value INR 10000",
     {"kind": "bank_instant", "dt": "percent", "value": 750, "max": 100000, "min": 1000000, "banks": ["sbi"]}),
    ("5% Instant Discount up to ₹750 on Axis Bank Debit Cards",
     {"kind": "bank_instant", "dt": "percent", "value": 500, "max": 75000, "min": None, "banks": ["axis"]}),
    # Label corrected after the first run: "on orders of ₹20,000 and above" is a minimum order.
    ("Flat ₹1,500 Instant Discount on Kotak Bank Credit Card EMI on orders of ₹20,000 and above",
     {"kind": "bank_instant", "dt": "flat", "value": 150000, "max": None, "min": 2000000, "banks": ["kotak"]}),
    ("10% Instant Discount up to INR 1,000 on IDFC FIRST Bank Credit Card Transactions",
     {"kind": "bank_instant", "dt": "percent", "value": 1000, "max": 100000, "min": None, "banks": ["idfc"]}),
    ("Save ₹500 with coupon", {"kind": "product_coupon", "dt": "flat", "value": 50000, "max": None, "min": None}),
    ("Apply ₹100 coupon", {"kind": "product_coupon", "dt": "flat", "value": 10000, "max": None, "min": None}),
    ("Save 5% with coupon", {"kind": "product_coupon", "dt": "percent", "value": 500, "max": None, "min": None}),
    ("Apply 12% coupon", {"kind": "product_coupon", "dt": "percent", "value": 1200, "max": None, "min": None}),
    ("Coupon: Extra ₹250 off", {"kind": "product_coupon", "dt": "flat", "value": 25000, "max": None, "min": None}),
    ("Use code SAVE200 to get ₹200 off on orders above ₹1999",
     {"kind": "promo_code", "dt": "flat", "value": 20000, "max": None, "min": 199900}),
    ("Get 10% off up to ₹300 with code AMZ10",
     {"kind": "promo_code", "dt": "percent", "value": 1000, "max": 30000, "min": None}),
    ("Upto ₹300 cashback as Amazon Pay Balance when you pay with Amazon Pay UPI",
     {"kind": "amazon_pay_cashback", "dt": "flat", "value": 30000, "max": None, "min": None}),
    ("Get 5% back with Amazon Pay ICICI Bank credit card for Prime members",
     {"kind": "bank_cashback", "dt": "percent", "value": 500, "max": None, "min": None, "banks": ["icici"]}),
    ("Get 3% back with Amazon Pay ICICI Bank credit card for non-Prime members",
     {"kind": "bank_cashback", "dt": "percent", "value": 300, "max": None, "min": None, "banks": ["icici"]}),
    ("Flat ₹100 cashback on your first Amazon Pay UPI transaction",
     {"kind": "amazon_pay_cashback", "dt": "flat", "value": 10000, "max": None, "min": None}),
    ("5% cashback up to ₹150 on HDFC Bank Debit Cards",
     {"kind": "bank_cashback", "dt": "percent", "value": 500, "max": 15000, "min": None, "banks": ["hdfc"]}),
    ("Up to 6% cashback on Amazon via the portal",
     {"kind": "portal_cashback", "dt": "percent", "value": 600, "max": None, "min": None}),
    ("Get GST invoice and save up to 28% on business purchases.", None),
    ("No Cost EMI available on select cards. Please check 'EMI options' above for more details.", None),
    ("Buy 2 or more and get 5% off", None),  # multi-buy: deliberately unsupported -> should not be mis-parsed
    ("Free delivery on your first order", None),
    ("Top brands, great deals", None),
    ("Partner Offers: Get special pricing on select products", None),
    ("Sign up for Prime and save", None),
]


def experiment_b() -> dict:
    tp = fp = fn = wrong = 0
    unknown = 0
    failures = []
    for text, label in LABELLED:
        try:
            n = normalize_record({"text": text})
        except NormalizationError as e:
            if label is None:
                continue  # correctly rejected
            unknown += 1
            fn += 1
            failures.append({"text": text, "error": str(e)})
            continue
        if label is None:
            fp += 1
            failures.append({"text": text, "parsed_as": n.kind.value})
            continue
        got = {"kind": n.kind.value, "dt": n.discount_type.value, "value": n.value, "max": n.max_discount, "min": n.min_order}
        if "banks" in label:
            got["banks"] = sorted(n.payment.banks) if n.payment else []
        exp = dict(label)
        if got == exp:
            tp += 1
        else:
            wrong += 1
            failures.append({"text": text, "expected": exp, "got": got})
    offers = sum(1 for _, l in LABELLED if l is not None)
    non = len(LABELLED) - offers
    accepted = tp + wrong + fp
    return {
        "dataset": f"{len(LABELLED)} author-written strings ({offers} offers, {non} non-offers); not sampled from live pages",
        "exact_field_match": tp, "parsed_with_wrong_fields": wrong, "non_offers_accepted": fp,
        "offers_rejected_as_unknown": unknown,
        "precision_exact": round(tp / accepted, 3) if accepted else None,
        "recall_exact": round(tp / offers, 3),
        "false_positive_rate_on_non_offers": round(fp / non, 3),
        "unknown_rate_on_offers": round(unknown / offers, 3),
        "failures": failures,
    }


# --- Experiment C: ranking strategies ----------------------------------------------

def _random_case(rng: random.Random) -> tuple[ProductContext, list[Offer], UserProfile]:
    price = rupees(rng.choice([2_000, 5_000, 10_000, 25_000, 60_000]))
    offers: list[Offer] = []
    auth = [Authenticity.OFFICIAL, Authenticity.SOURCE_LISTED, Authenticity.UNVERIFIED]

    def tri() -> Tri:
        return rng.choice([Tri.YES, Tri.YES, Tri.NO, Tri.UNKNOWN])

    if rng.random() < 0.7:
        offers.append(Offer("coupon", OfferKind.PRODUCT_COUPON, "coupon", discount_type=DiscountType.FLAT,
                            value=price * rng.choice([3, 5, 8]) // 100, basis=Basis.PRICE, authenticity=rng.choice(auth),
                            stacks_with={"bank_instant": tri(), "portal_cashback": tri()}))
    for bank in ("hdfc", "sbi"):
        if rng.random() < 0.6:
            offers.append(Offer(f"bank-{bank}", OfferKind.BANK_INSTANT, f"{bank} instant", discount_type=DiscountType.PERCENT,
                                value=rng.choice([500, 1000]), max_discount=rupees(rng.choice([500, 1000, 1500])),
                                min_order=rupees(rng.choice([1_000, 5_000])), basis=rng.choice([Basis.UNKNOWN, Basis.AFTER_COUPONS]),
                                payment=PaymentRequirement(banks=frozenset({bank})), authenticity=rng.choice(auth)))
    if rng.random() < 0.7:
        offers.append(Offer("portal", OfferKind.PORTAL_CASHBACK, "portal cashback", discount_type=DiscountType.PERCENT,
                            value=rng.choice([300, 500, 1000, 1500]), max_discount=rupees(rng.choice([300, 1000, 3000])),
                            basis=Basis.UNKNOWN, authenticity=rng.choice(auth),
                            settlement_probability_bp=rng.choice([4_000, 6_500, 8_500])))
    wallet = [PaymentInstrument("hdfc", "HDFC CC", "credit_card", bank="hdfc"),
              PaymentInstrument("sbi", "SBI DC", "debit_card", bank="sbi")]
    if rng.random() < 0.5:
        wallet.append(PaymentInstrument("gc", "Gift balance", "amazon_pay_balance", cost_ratio_bp=rng.choice([9_600, 9_800])))
    return ProductContext("B0EXPC0000", price), offers, UserProfile(instruments=tuple(wallet))


def _expected_cost(s, settle_scale: float = 1.0) -> float:
    """Expected cash outlay under the model's probabilities (optionally with settlement rates scaled)."""
    exp_payable = s.price - sum(a.amount * a.confidence_bp / 10_000 for a in s.applied if not a.deferred)
    ratio = s.instrument.cost_ratio_bp / 10_000 if s.instrument else 1.0
    deferred = sum(a.amount * min(a.settle_bp * settle_scale, 10_000) / 10_000 * a.confidence_bp / 10_000
                   for a in s.applied if a.deferred)
    return exp_payable * ratio - deferred


def experiment_c(cases: int = 2_000) -> dict:
    rng = random.Random(11)
    strategies = ["highest_advertised_discount", "lowest_checkout_price", "lowest_effective_if_all_paid", "risk_adjusted"]
    disagree = {s: 0 for s in strategies}
    regret = {s: 0.0 for s in strategies}
    regret_pess = {s: 0.0 for s in strategies}
    for _ in range(cases):
        prod, offers, user = _random_case(rng)
        r = optimize(prod, offers, user, NOW)
        picks = {s: rank_by(r, s) for s in strategies}
        best = min(_expected_cost(sc) for sc in r.scenarios)
        best_p = min(_expected_cost(sc, 0.75) for sc in r.scenarios)
        for s, sc in picks.items():
            if sc.offer_ids != picks["risk_adjusted"].offer_ids or sc.instrument != picks["risk_adjusted"].instrument:
                disagree[s] += 1
            regret[s] += _expected_cost(sc) - best
            regret_pess[s] += _expected_cost(sc, 0.75) - best_p
    return {
        "cases": cases,
        "pick_differs_from_risk_adjusted_pct": {s: round(100 * disagree[s] / cases, 1) for s in strategies},
        "mean_expected_regret_inr": {s: round(regret[s] / cases / 100, 2) for s in strategies},
        "mean_regret_if_cashback_settles_25pct_less_inr": {s: round(regret_pess[s] / cases / 100, 2) for s in strategies},
        "caveat": "Expected cost uses the optimizer's own probability model, so risk_adjusted is optimal by construction "
                  "under it; the result shows how often and by how much naive strategies diverge, not real-world savings.",
    }


# --- Experiment D: deal-quality alerts vs naive "% off" alerts --------------------

def experiment_d(n_per_class: int = 100) -> dict:
    rng = random.Random(5)
    rows = []
    for cls in ("genuine_drop", "inflated_reference", "pre_sale_inflation", "steady"):
        for _ in range(n_per_class):
            base = rupees(rng.choice([1_000, 3_000, 10_000, 40_000]))
            series = [base * (100 + rng.randint(-3, 3)) // 100 for _ in range(90)]
            list_price = base * rng.choice([120, 130, 140]) // 100
            if cls == "genuine_drop":
                series[-1] = base * rng.choice([80, 85, 88]) // 100
            elif cls == "inflated_reference":
                list_price = base * 2
            elif cls == "pre_sale_inflation":
                for i in range(76, 89):
                    series[i] = base * 125 // 100
                series[-1] = base * (100 + rng.randint(-2, 0)) // 100
                list_price = base * 125 // 100 * 13 // 10
            rows.append((cls, series, list_price))

    def naive(series, lp):
        return (lp - series[-1]) * 100 // lp >= 20 or (series[-2] - series[-1]) * 100 // series[-2] >= 10

    def history(series, lp):  # exactly what service.evaluate does: stats over all observations incl. the current one
        obs = [Observation(p, NOW - timedelta(days=89 - i)) for i, p in enumerate(series)]
        return judge_price(compute_stats(obs, NOW), series[-1], lp).is_good_deal

    out = {}
    for name, rule in (("naive_percent_off", naive), ("history_aware", history)):
        tp = sum(1 for c, s, lp in rows if rule(s, lp) and c == "genuine_drop")
        fp = sum(1 for c, s, lp in rows if rule(s, lp) and c != "genuine_drop")
        fn = sum(1 for c, s, lp in rows if not rule(s, lp) and c == "genuine_drop")
        by_cls = {c: sum(1 for cc, s, lp in rows if cc == c and rule(s, lp)) for c in
                  ("genuine_drop", "inflated_reference", "pre_sale_inflation", "steady")}
        out[name] = {"alerts": tp + fp, "true_positives": tp, "false_positives": fp, "missed": fn,
                     "precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3),
                     "alerts_by_class": by_cls}
    out["dataset"] = f"{4 * n_per_class} synthetic 90-day daily price series, {n_per_class} per class"
    return out


# --- Experiment F: source failure degrades gracefully ------------------------------

def experiment_f() -> dict:
    with tempfile.TemporaryDirectory() as d:
        db = open_db(Path(d) / "f.db")
        asin = "B0FAILTEST"
        coupon = {"kind": "product_coupon", "title": "₹700 coupon (source X)", "discount": "₹700", "asins": [asin], "basis": "price"}
        bank = {"kind": "bank_instant", "title": "5% HDFC (source Y)", "discount": "5%", "max_discount": "₹400",
                "payment": {"banks": ["hdfc"]}, "basis": "price", "stacks_with": {"product_coupon": "no"}}
        state = {"x_up": True}

        class X:
            adapter, requires_terms_review = "x", False

            def fetch(self, cfg):
                if not state["x_up"]:
                    raise SourceError("connection refused")
                return FetchResult([SourceRecord(dict(coupon), "official_page", Authenticity.OFFICIAL)], True, "synthetic")

        class Y:
            adapter, requires_terms_review = "y", False

            def fetch(self, cfg):
                return FetchResult([SourceRecord(dict(bank), "official_page", Authenticity.OFFICIAL)], True, "synthetic")

        ingest.register_source(db, "X", "file_import", "X", {}, NOW, min_interval_s=1800, stale_after_h=48)
        ingest.register_source(db, "Y", "file_import", "Y", {}, NOW, min_interval_s=1800, stale_after_h=48)
        repo.set_setting(db, "wallet", [{"id": "h", "label": "HDFC", "type": "credit_card", "bank": "hdfc"}])
        pid = repo.upsert_product(db, asin, NOW, title="Failure test")
        repo.watch(db, pid, NOW)

        class Sink:
            name, sent = "sink", []

            def send(self, t):
                Sink.sent.append(t)

        timeline = []
        attempts_x = 0
        t = NOW
        for step in range(7 * 24 * 6):  # 7 days, scheduler tick every 10 minutes
            if step == 6:
                state["x_up"] = False  # X goes down after one hour
            for sid, ad in (("X", X()), ("Y", Y())):
                st = ingest.run_source(db, sid, t, ad)
                if sid == "X" and st.status != "skipped":
                    attempts_x += 1
            if step % 6 == 0:
                repo.record_price(db, pid, rupees(10_000), t, "sim", "synthetic")
            if step % 36 == 0:  # every 6 hours
                ev = service.evaluate(db, asin, t)
                best = ev.result.best_confirmed
                x = db.one("SELECT enabled, consecutive_failures, disabled_reason FROM sources WHERE id='X'")
                timeline.append({"hours": round((t - NOW).total_seconds() / 3600), "x_enabled": x["enabled"],
                                 "x_failures": x["consecutive_failures"],
                                 "best_confirmed": [a.title for a in best.applied], "cash_cost_inr": best.cash_cost // 100})
                alerts.run_alerts(db, Sink(), t)
            t += timedelta(minutes=10)
        db.close()
    return {"x_fetch_attempts_over_7_days": attempts_x, "timeline_every_6h": timeline,
            "alerts_sent": len(Sink.sent), "alert_texts": [s.splitlines()[1] for s in Sink.sent]}


def main() -> None:
    out = {"B_parser_accuracy": experiment_b(), "C_ranking_strategies": experiment_c(), "D_deal_quality": experiment_d(),
           "F_source_failure": experiment_f()}
    dest = ROOT / "benchmarks" / "results"
    dest.mkdir(exist_ok=True)
    (dest / "experiments.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
