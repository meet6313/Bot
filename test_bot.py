import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import bot


class Scripted:
    def __init__(self, prices):
        self.prices = list(prices)
        self.i = 0

    def price(self):
        p = self.prices[min(self.i, len(self.prices) - 1)]
        self.i += 1
        return p


class Broken:
    def price(self):
        raise RuntimeError("network down")


def make_engine(feed, **over):
    cfg = dict(bot.DEFAULTS)
    cfg.update({"price_source": "sim", "candle_seconds": 0, "fast": 2, "slow": 3,
                "size_pct": 50, "stop_pct": 2, "daily_loss_pct": 5, "start_cash": 100000})
    cfg.update(over)
    d = tempfile.mkdtemp()
    return bot.Engine(cfg, feed, bot.PaperExec(cfg["fee_pct"]), os.path.join(d, "s.json"))


def run(engine, n):
    for _ in range(n):
        engine.step()


# a dip, then a rally (fast average crosses above slow), then a fall (crosses below)
CYCLE = [100, 99, 98, 97, 98, 100, 103, 106, 108, 107, 104, 100, 96, 92]


class Params(unittest.TestCase):
    def test_good(self):
        p = bot.validate_params(bot.DEFAULTS)
        self.assertEqual(p["fast"], 9)

    def test_slow_must_exceed_fast(self):
        with self.assertRaises(ValueError):
            bot.validate_params(dict(bot.DEFAULTS, fast=10, slow=10))

    def test_out_of_range(self):
        for k, v in (("size_pct", 0), ("size_pct", 101), ("stop_pct", 0), ("daily_loss_pct", 99), ("fast", 1)):
            with self.assertRaises(ValueError):
                bot.validate_params(dict(bot.DEFAULTS, **{k: v}))

    def test_not_a_number(self):
        with self.assertRaises(ValueError):
            bot.validate_params(dict(bot.DEFAULTS, size_pct="abc"))
        with self.assertRaises(ValueError):
            bot.validate_params(dict(bot.DEFAULTS, size_pct=float("nan")))

    def test_live_needs_real_prices(self):
        with self.assertRaises(ValueError):
            d = tempfile.mkdtemp()
            p = os.path.join(d, "c.json")
            with open(p, "w") as f:
                json.dump({"mode": "live", "price_source": "sim"}, f)
            bot.load_config(p)

    def test_unknown_key_rejected(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c.json")
        with open(p, "w") as f:
            json.dump({"modee": "live"}, f)
        with self.assertRaises(ValueError):
            bot.load_config(p)


class Trading(unittest.TestCase):
    def test_buys_on_cross_up_sells_on_cross_down(self):
        e = make_engine(Scripted(CYCLE), stop_pct=30, daily_loss_pct=50)
        run(e, len(CYCLE))
        sides = [t["side"] for t in e.s["trades"]]
        self.assertEqual(sides, ["BUY", "SELL"])
        self.assertEqual(e.s["qty"], 0)

    def test_fees_are_charged(self):
        e = make_engine(Scripted(CYCLE), stop_pct=30, daily_loss_pct=50)
        run(e, len(CYCLE))
        buy, sell = e.s["trades"]
        spend = 50000.0
        self.assertAlmostEqual(buy["qty"] * buy["price"], spend * 0.999, places=4)
        gross = sell["qty"] * sell["price"]
        self.assertAlmostEqual(e.s["cash"], 50000 + gross * 0.999, places=4)
        expected = gross * 0.999 - spend
        self.assertAlmostEqual(sell["pnl"], expected, places=4)

    def test_only_one_position_at_a_time(self):
        up = [100, 99, 98, 99, 101, 103, 105, 107, 109, 111, 113]
        e = make_engine(Scripted(up), stop_pct=30, daily_loss_pct=50)
        run(e, len(up))
        self.assertEqual(len([t for t in e.s["trades"] if t["side"] == "BUY"]), 1)

    def test_stop_loss(self):
        prices = [100, 99, 98, 97, 98, 100, 103, 106, 90, 90]
        e = make_engine(Scripted(prices), stop_pct=2, daily_loss_pct=50)
        run(e, len(prices))
        reasons = [t["reason"] for t in e.s["trades"]]
        self.assertIn("Stop-loss hit", reasons)
        self.assertEqual(e.s["qty"], 0)

    def test_stop_loss_checked_between_candles(self):
        cfg_over = dict(candle_seconds=10 ** 6, fast=2, slow=3, stop_pct=2, daily_loss_pct=50)
        e = make_engine(Scripted([90]), **cfg_over)
        e.s.update(qty=10.0, entry=100.0, cash=0.0, last_price=100.0)
        e.next_candle_at = 10 ** 12  # no new candle will form
        run(e, 1)
        self.assertEqual(e.s["qty"], 0)
        self.assertEqual(e.s["trades"][0]["reason"], "Stop-loss hit")

    def test_daily_loss_limit_halts_and_sells(self):
        prices = [100, 99, 98, 97, 98, 100, 103, 106, 80, 80]
        e = make_engine(Scripted(prices), stop_pct=30, daily_loss_pct=2)
        run(e, len(prices))
        self.assertTrue(e.s["halted"])
        self.assertEqual(e.s["qty"], 0)
        self.assertIn("Daily loss", e.s["halt_reason"])
        n = len(e.s["trades"])
        e.feed = Scripted([50, 60, 70, 80, 90, 100, 110, 120])
        run(e, 8)
        self.assertEqual(len(e.s["trades"]), n)  # no more trades while halted

    def test_manual_restart_clears_halt(self):
        e = make_engine(Scripted([100]))
        e.s["halted"], e.s["halt_reason"] = True, "Daily loss limit reached"
        e.start()
        self.assertFalse(e.s["halted"])

    def test_halts_after_repeated_price_errors(self):
        e = make_engine(Broken(), max_consecutive_errors=3)
        run(e, 3)
        self.assertTrue(e.s["halted"])

    def test_error_streak_resets_on_success(self):
        e = make_engine(Scripted([100]), max_consecutive_errors=3)
        e.s["errors"] = 2
        run(e, 1)
        self.assertEqual(e.s["errors"], 0)
        self.assertFalse(e.s["halted"])

    def test_stopped_bot_does_not_trade(self):
        e = make_engine(Scripted(CYCLE), stop_pct=30, daily_loss_pct=50)
        e.stop()
        run(e, len(CYCLE))
        self.assertEqual(e.s["trades"], [])

    def test_kill_sells_and_stops(self):
        e = make_engine(Scripted([100]))
        e.s.update(qty=10.0, entry=100.0, cash=0.0, last_price=100.0)
        e.kill()
        self.assertEqual(e.s["qty"], 0)
        self.assertFalse(e.s["running"])

    def test_order_below_minimum_skipped(self):
        e = make_engine(Scripted(CYCLE), start_cash=150, stop_pct=30, daily_loss_pct=50)
        run(e, len(CYCLE))
        self.assertEqual(e.s["trades"], [])

    def test_state_survives_restart(self):
        e = make_engine(Scripted(CYCLE[:9]), stop_pct=30, daily_loss_pct=50)
        run(e, 9)
        e2 = bot.Engine(e.cfg, Scripted([100]), bot.PaperExec(0.1), e.state_path)
        self.assertEqual(e2.s["qty"], e.s["qty"])
        self.assertEqual(e2.s["closes"], e.s["closes"])

    def test_sim_shock(self):
        f = bot.SimFeed(100, seed=1, shock_at=3, shock_pct=-50)
        a = [f.price() for _ in range(4)]
        self.assertLess(a[2], a[1] * 0.6)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = make_engine(Scripted([100, 101, 102]))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), bot.make_handler(cls.e, "correct-horse-battery"))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, method="GET", token="correct-horse-battery", body=None):
        h = {"Authorization": "Bearer " + token} if token else {}
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.url + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_page_loads_without_password(self):
        code, body = self.call("/", token=None)
        self.assertEqual(code, 200)
        self.assertIn(b"<html", body.lower())

    def test_state_needs_password(self):
        self.assertEqual(self.call("/api/state", token=None)[0], 401)
        self.assertEqual(self.call("/api/state", token="wrong")[0], 401)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_controls_need_password(self):
        for path in ("/api/start", "/api/stop", "/api/kill", "/api/params"):
            self.assertEqual(self.call(path, "POST", token="wrong")[0], 401)
            self.assertEqual(self.call(path, "POST", token=None)[0], 401)

    def test_bad_params_rejected(self):
        code, body = self.call("/api/params", "POST", body={"fast": 50, "slow": 10})
        self.assertEqual(code, 400)
        self.assertEqual(self.e.p["fast"], 2)

    def test_good_params_applied(self):
        code, _ = self.call("/api/params", "POST", body={"fast": 5, "slow": 12})
        self.assertEqual(code, 200)
        self.assertEqual(self.e.p["slow"], 12)

    def test_cannot_switch_to_live_over_api(self):
        self.call("/api/params", "POST", body={"mode": "live"})
        self.assertEqual(self.e.cfg["mode"], "paper")


if __name__ == "__main__":
    unittest.main()
