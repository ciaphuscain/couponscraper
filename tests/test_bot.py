import io
import unittest
import urllib.error


from couponhunter import repo
from couponhunter.amazon import InvalidAmazonURL
from couponhunter.bot import Bot, resolve_short_link
from couponhunter.db import open_db
from tests.test_alerts import NOW

CHAT = "4242"


class FakeApi:
    chat_id = CHAT

    def __init__(self):
        self.queue, self.sent, self.offsets = [], [], []

    def get_updates(self, offset, timeout=25):
        self.offsets.append(offset)
        return [u for u in self.queue if u["update_id"] >= offset]

    def send(self, text, chat_id=None):
        self.sent.append((chat_id, text))


def msg(uid, text, chat=CHAT):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.api = FakeApi()
        self.resolved = []

        def resolver(url):
            self.resolved.append(url)
            return "B0SHORT001"

        self.bot = Bot(self.db, self.api, clock=lambda: NOW, resolver=resolver)

    def say(self, text, uid=1, chat=CHAT):
        return self.bot.handle(msg(uid, text, chat))

    def test_strangers_are_ignored_silently(self):
        self.assertIsNone(self.say("https://www.amazon.in/dp/B0TESTASIN", chat="999"))
        self.assertIsNone(self.db.one("SELECT 1 FROM products"))

    def test_share_from_app_tracks_product(self):
        r = self.say("Sony WH-1000XM5 Wireless Headphones https://www.amazon.in/dp/B0TESTASIN?ref=share")
        self.assertIn("Tracking ✓", r)
        self.assertIn("No price yet", r)
        row = self.db.one("SELECT p.title FROM watchlist w JOIN products p ON p.id=w.product_id")
        self.assertEqual(row["title"], "Sony WH-1000XM5 Wireless Headphones")

    def test_short_link_goes_through_resolver(self):
        r = self.say("Check this out https://amzn.in/d/abc123")
        self.assertEqual(self.resolved, ["https://amzn.in/d/abc123"])
        self.assertIn("Tracking ✓", r)

    def test_non_amazon_link_rejected(self):
        self.assertIn("Couldn't use that link", self.say("https://evil.example/dp/B0TESTASIN"))

    def test_price_offer_best_and_feedback(self):
        self.say("https://www.amazon.in/dp/B0TESTASIN")
        self.assertIn("Price recorded", self.say("/price B0TESTASIN ₹10,000", uid=2))
        r = self.say("/offer B0TESTASIN Save ₹500 with coupon", uid=3)
        self.assertIn("Offer saved", r)
        best = self.say("/best B0TESTASIN", uid=4)
        self.assertIn("Lowest realistic: ₹9,500", best)
        oid = self.db.one("SELECT id FROM offers")[0]
        self.assertIn(f"#{oid}", best)
        self.assertIn("recorded", self.say(f"/failed #{oid}", uid=5))
        self.say(f"/failed #{oid}", uid=5)  # replayed update: idempotent
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM redemption_attempts")[0], 1)
        self.assertIn("Usage", self.say("/applied abc", uid=6))

    def test_target_list_stop_status_cards(self):
        self.say("https://www.amazon.in/dp/B0TESTASIN")
        self.say("/price B0TESTASIN 1000", uid=2)
        self.assertIn("₹900", self.say("/target B0TESTASIN 900", uid=3))
        self.assertEqual(self.db.one("SELECT target_price FROM watchlist")[0], 900_00)
        self.say("https://www.amazon.in/dp/B0TESTASIN", uid=4)  # re-sharing keeps the target
        self.assertEqual(self.db.one("SELECT target_price FROM watchlist")[0], 900_00)
        self.assertIn("B0TESTASIN", self.say("/list", uid=5))
        self.assertIn("OK: 1 watched", self.say("/status", uid=6))
        self.assertIn("Across", self.say("/cards", uid=7))
        self.assertIn("Stopped", self.say("/stop B0TESTASIN", uid=8))
        self.assertIn("empty", self.say("/list", uid=9))

    def test_unknown_product_and_help(self):
        self.assertIn("not tracking", self.say("/best B0UNKNOWN1"))
        self.assertIn("/best", self.say("hello"))
        self.assertIn("usage", self.say("/price B0TESTASIN", uid=2).lower())

    def test_poll_persists_offset_and_survives_handler_crash(self):
        self.api.queue = [msg(10, "/help"), msg(11, "/status"), msg(12, "/help")]
        orig = self.bot.handle

        def flaky(u):
            if u["update_id"] == 11:
                raise RuntimeError("boom")
            return orig(u)

        self.bot.handle = flaky
        self.assertEqual(self.bot.poll_once(timeout=0), 3)
        self.assertEqual(len(self.api.sent), 2)
        self.assertEqual(repo.get_settings(self.db)["telegram_offset"], 13)
        self.bot.poll_once(timeout=0)  # nothing re-processed after "restart"
        self.assertEqual(self.api.offsets[-1], 13)
        self.assertEqual(len(self.api.sent), 2)


class ShortLinkTests(unittest.TestCase):
    class Opener:
        def __init__(self, responses):
            self.responses, self.urls = list(responses), []

        def open(self, req, timeout):
            self.urls.append((req.get_method(), req.full_url))
            r = self.responses.pop(0)
            raise r

    def redirect(self, loc, code=301):
        return urllib.error.HTTPError("x", code, "moved", {"Location": loc}, io.BytesIO(b""))

    def test_reads_location_without_following(self):
        op = self.Opener([self.redirect("https://www.amazon.in/dp/B09XS7JWHH?ref_=share")])
        self.assertEqual(resolve_short_link("https://amzn.in/d/abc", opener=op), "B09XS7JWHH")
        self.assertEqual(op.urls, [("HEAD", "https://amzn.in/d/abc")])  # amazon.in itself never requested

    def test_rejects_non_product_targets_and_errors(self):
        with self.assertRaises(InvalidAmazonURL):
            resolve_short_link("https://amzn.in/d/x", opener=self.Opener([self.redirect("https://evil.example/")]))
        with self.assertRaises(InvalidAmazonURL):
            resolve_short_link("https://amzn.in/d/x", opener=self.Opener([urllib.error.HTTPError("x", 404, "nf", {}, io.BytesIO())]))
        with self.assertRaises(InvalidAmazonURL):
            resolve_short_link("https://amzn.in/d/x", opener=self.Opener([urllib.error.URLError("down")]))

    def test_shortener_chain_limited(self):
        op = self.Opener([self.redirect("https://amzn.to/y"), self.redirect("https://amzn.to/z")])
        with self.assertRaises(InvalidAmazonURL):
            resolve_short_link("https://amzn.in/d/x", opener=op, max_hops=2)
        self.assertEqual(len(op.urls), 2)


if __name__ == "__main__":
    unittest.main()



class WireTests(unittest.TestCase):
    """Real TelegramNotifier + Bot against a local fake Bot API over HTTP."""

    def test_round_trip(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from couponhunter.notify import TelegramNotifier

        calls = []

        class FakeTelegram(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = self.path.rsplit("/", 1)[-1]
                calls.append((self.path, method, body))
                if method == "getUpdates":
                    res = [msg(7, "https://www.amazon.in/dp/B0TESTASIN")] if body["offset"] <= 7 else []
                    out = {"ok": True, "result": res}
                else:
                    out = {"ok": True, "result": {"message_id": 1}}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        db = open_db(":memory:")
        self.addCleanup(db.close)
        tg = TelegramNotifier("123456:" + "A" * 35, CHAT)
        tg.API = f"http://127.0.0.1:{srv.server_address[1]}"
        b = Bot(db, tg, clock=lambda: NOW)
        self.assertEqual(b.poll_once(timeout=0), 1)
        self.assertEqual(b.poll_once(timeout=0), 0)
        methods = [c[1] for c in calls]
        self.assertEqual(methods, ["getUpdates", "sendMessage", "getUpdates"])
        self.assertEqual(calls[2][2]["offset"], 8)
        self.assertEqual(calls[1][2]["chat_id"], CHAT)
        self.assertIn("Tracking", calls[1][2]["text"])
        self.assertNotIn("parse_mode", calls[1][2])
