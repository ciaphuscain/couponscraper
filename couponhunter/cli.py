"""Command-line interface. `couponhunter --help` (or `python -m couponhunter --help`)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import alerts, config, ingest, logs, repo, service
from .amazon import extract_asin
from .db import backup, integrity_check, open_db, utcnow
from .money import fmt, parse_inr
from .notify import ConsoleNotifier, FileNotifier, TelegramNotifier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _db(cfg: config.Config):
    db = open_db(cfg.db_path)
    ingest.ensure_builtin_sources(db, utcnow())
    return db


def _notifier(cfg: config.Config, channel: str):
    if channel == "telegram":
        t = TelegramNotifier.from_env()
        if t is None:
            sys.exit("Set COUPONHUNTER_TELEGRAM_TOKEN and COUPONHUNTER_TELEGRAM_CHAT_ID first.")
        return t
    if channel == "file":
        return FileNotifier(cfg.home / "alerts.jsonl")
    if channel == "auto":
        return TelegramNotifier.from_env() or FileNotifier(cfg.home / "alerts.jsonl")
    return ConsoleNotifier()


def cmd_init(cfg, a):
    db = _db(cfg)
    cfg.api_token()
    print(f"Data folder : {cfg.home}")
    print(f"Database    : {cfg.db_path} (integrity: {integrity_check(db)})")
    if a.demo:
        now = utcnow()
        path = FIXTURES / "demo_offers.json"
        ingest.register_source(db, "demo", "file_import", "Demo offers (SYNTHETIC)", {"path": str(path)}, now,
                               min_interval_s=0, stale_after_h=24 * 365)
        st = ingest.run_source(db, "demo", now, force=True)
        pid = repo.upsert_product(db, "B0DEMO0001", now, title="Demo Headphones (SYNTHETIC)", data_origin="synthetic")
        repo.watch(db, pid, now, target_price=9_000_00)
        repo.record_price(db, pid, 10_000_00, now, "demo", "synthetic", list_price=14_990_00)
        print(f"Demo data   : {st.created} synthetic offers, 1 synthetic product (B0DEMO0001). "
              "Everything demo is labelled SYNTHETIC in the UI.")
    print("Next        : couponhunter serve   (then open http://127.0.0.1:%d)" % cfg.port)


def cmd_token(cfg, a):
    print(cfg.api_token())


def cmd_add(cfg, a):
    db, now = _db(cfg), utcnow()
    asin = extract_asin(a.url)
    pid = repo.upsert_product(db, asin, now, title=a.title or "")
    repo.watch(db, pid, now, target_price=parse_inr(a.target) if a.target else None)
    if a.price:
        repo.record_price(db, pid, parse_inr(a.price), now, "manual", "manual")
    print(f"Watching {asin}")


def cmd_price(cfg, a):
    db, now = _db(cfg), utcnow()
    p = repo.get_product(db, a.asin)
    repo.record_price(db, p["id"], parse_inr(a.price), now, "manual", "manual",
                      list_price=parse_inr(a.list_price) if a.list_price else None)
    print(f"Recorded {fmt(parse_inr(a.price))} for {p['asin']}")


def cmd_import(cfg, a):
    db, now = _db(cfg), utcnow()
    path = Path(a.file).resolve()
    sid = a.source_id or f"file:{path.stem}"
    ingest.register_source(db, sid, "file_import", a.name or path.name,
                           {"path": str(path), "evidence": a.evidence, "snapshot": not a.append}, now, min_interval_s=0)
    st = ingest.run_source(db, sid, now, force=True)
    print(json.dumps(st.__dict__, indent=1, ensure_ascii=False))


def cmd_add_feed(cfg, a):
    db, now = _db(cfg), utcnow()
    if not a.terms_note:
        sys.exit("Pass --terms-note describing where the publisher permits feed use (e.g. their feed page / ToS).")
    ingest.register_source(db, a.id, "rss", a.name or a.id, {"url": a.url}, now, terms_reviewed=True,
                           terms_note=a.terms_note, min_interval_s=max(a.interval_minutes, 30) * 60)
    print(f"Registered feed {a.id}; it will be fetched at most every {max(a.interval_minutes, 30)} minutes.")


def cmd_sources(cfg, a):
    db, now = _db(cfg), utcnow()
    for r in db.query("SELECT * FROM sources ORDER BY id"):
        due, why = ingest.source_due(r, now)
        print(f"{r['id']:<24} {r['adapter']:<12} enabled={r['enabled']} failures={r['consecutive_failures']} "
              f"last_ok={r['last_success_at'] or '-'} [{why}]")
        if r["disabled_reason"]:
            print(f"{'':<24} disabled: {r['disabled_reason']}")


def cmd_run_sources(cfg, a):
    db, now = _db(cfg), utcnow()
    for r in db.query("SELECT id FROM sources WHERE adapter NOT IN ('extension','manual')"):
        st = ingest.run_source(db, r["id"], now, force=a.force)
        print(f"{r['id']}: {st.status} new={st.created} updated={st.updated} dup={st.duplicates} "
              f"rejected={st.rejected} {'; '.join(st.errors + st.warnings)[:200]}")


def cmd_evaluate(cfg, a):
    db, now = _db(cfg), utcnow()
    ev = service.evaluate(db, a.asin, now)
    if a.json:
        print(json.dumps(service.evaluation_json(ev, db, now), indent=1, ensure_ascii=False, default=str))
        return
    if ev.result is None:
        print("No price observation yet for", ev.asin)
        return
    r = ev.result
    print(f"{ev.title or ev.asin}  price {fmt(r.product.price)}{'  [STALE]' if r.product.price_is_stale else ''}")
    best = r.best_confirmed
    if best:
        print(f"Lowest realistic price: {fmt(best.cash_cost)}  (offers apply with {best.success_probability_bp // 100}% est.)")
        for a_ in best.applied:
            print(f"  - {a_.title}: {fmt(a_.amount)}{' (cashback later, not guaranteed)' if a_.deferred else ''}")
        for st in best.steps:
            print(f"  > {st}")
    if r.best_possible is not best:
        print(f"Possibly {fmt(r.best_possible.cash_cost)} if: " + "; ".join(r.best_possible.assumptions[:3]))
    for e in r.excluded:
        print(f"  x {e.title}: {e.reason}")
    if ev.verdict:
        for x in ev.verdict.reasons + ev.verdict.caveats:
            print(f"  * {x}")


def cmd_alerts(cfg, a):
    db, now = _db(cfg), utcnow()
    if a.dry_run:
        for d in alerts.decide(db, now):
            print(d.text, end="\n\n")
        return
    print(alerts.run_alerts(db, _notifier(cfg, a.channel), now))


def cmd_serve(cfg, a):
    from .scheduler import Scheduler, default_jobs
    from .web.app import App
    from .web.server import serve

    logs.setup(cfg.log_dir)
    db = _db(cfg)
    app = App(db, cfg.api_token(), cfg.port)
    httpd = serve(app, cfg.host, cfg.port)
    sched = None
    if not a.no_scheduler:
        sched = Scheduler(db, default_jobs(db, _notifier(cfg, a.channel), cfg.backup_dir))
        sched.start()
    print(f"CouponHunter on http://{cfg.host}:{cfg.port}  (data: {cfg.home})  Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if sched:
            sched.stop()
        db.close()


def cmd_backup(cfg, a):
    db = _db(cfg)
    print(backup(db, Path(a.dest) if a.dest else cfg.backup_dir))


def cmd_restore(cfg, a):
    src = Path(a.file)
    import sqlite3
    con = sqlite3.connect(src)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        sys.exit(f"Backup failed integrity check: {ok}")
    if not a.yes:
        sys.exit(f"This replaces {cfg.db_path}. Stop the server first, then re-run with --yes.")
    if cfg.db_path.exists():
        safety = cfg.db_path.with_suffix(f".before-restore-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.db")
        shutil.copy2(cfg.db_path, safety)
        print(f"Current database saved to {safety}")
    for suffix in ("-wal", "-shm"):
        p = Path(str(cfg.db_path) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(src, cfg.db_path)
    print(f"Restored {src} -> {cfg.db_path}")


def cmd_settings(cfg, a):
    db = _db(cfg)
    if a.key and a.value is not None:
        repo.set_setting(db, a.key, json.loads(a.value))
    print(json.dumps(repo.get_settings(db) if not a.key else {a.key: repo.get_settings(db)[a.key]}, indent=1))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="couponhunter", description="Personal Amazon India savings engine")
    p.add_argument("--home", help="data folder (default: %%LOCALAPPDATA%%\\CouponHunter or ~/.local/share/couponhunter)")
    p.add_argument("--port", type=int)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the database and API token")
    s.add_argument("--demo", action="store_true", help="also load clearly-labelled synthetic demo data")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("token", help="print the API token for the extension/dashboard").set_defaults(fn=cmd_token)

    s = sub.add_parser("add", help="watch an amazon.in product")
    s.add_argument("url")
    s.add_argument("--target", help="alert when you can pay this much or less, e.g. 9000")
    s.add_argument("--price", help="current price if you know it")
    s.add_argument("--title")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("price", help="record an observed price")
    s.add_argument("asin")
    s.add_argument("price")
    s.add_argument("--list-price")
    s.set_defaults(fn=cmd_price)

    s = sub.add_parser("import", help="import offers from a JSON/CSV file")
    s.add_argument("file")
    s.add_argument("--source-id")
    s.add_argument("--name")
    s.add_argument("--evidence", default="user_entered", choices=["user_entered", "official_page", "feed_listing"])
    s.add_argument("--append", action="store_true", help="don't treat the file as the complete list for this source")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("add-feed", help="register an RSS/Atom deal feed you are permitted to read")
    s.add_argument("id")
    s.add_argument("url")
    s.add_argument("--name")
    s.add_argument("--terms-note", required=False, default="")
    s.add_argument("--interval-minutes", type=int, default=120)
    s.set_defaults(fn=cmd_add_feed)

    sub.add_parser("sources", help="show source health").set_defaults(fn=cmd_sources)
    s = sub.add_parser("run-sources", help="refresh due sources now")
    s.add_argument("--force", action="store_true", help="ignore min interval (never ignores disabled/terms)")
    s.set_defaults(fn=cmd_run_sources)

    s = sub.add_parser("evaluate", help="best realistic price for a product")
    s.add_argument("asin")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("alerts", help="evaluate the watchlist and send alerts")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--channel", default="auto", choices=["auto", "console", "file", "telegram"])
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("serve", help="run dashboard + API + scheduler")
    s.add_argument("--no-scheduler", action="store_true")
    s.add_argument("--channel", default="auto", choices=["auto", "console", "file", "telegram"])
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("backup", help="consistent online backup")
    s.add_argument("--dest")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore", help="restore a backup (server must be stopped)")
    s.add_argument("file")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("settings", help="show or set a setting (value is JSON)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_settings)
    return p


def main(argv: list[str] | None = None) -> None:
    # ₹ must print even when Windows redirects output to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    cfg = config.load(args.home, args.port)
    try:
        args.fn(cfg, args)
    except (repo.NotFound,) as e:
        sys.exit(f"Not found: {e}")
    except ValueError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
