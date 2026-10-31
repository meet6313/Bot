#!/usr/bin/env python3
"""Crypto trading bot with a small password-protected dashboard API.

Standard library only (Python 3.9+). Runs in PAPER mode (fake money) by default.
Live trading needs three deliberate steps, see README.md.

    DASH_TOKEN=your-long-password python3 bot.py --config config.json
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "mode": "paper",              # "paper" (fake money) or "live" (real money)
    "price_source": "coindcx",    # "coindcx" (real prices) or "sim" (made-up prices, for testing)
    "market": "BTCINR",
    "fast": 9,                    # fast average, in candles
    "slow": 21,                   # slow average, in candles
    "candle_seconds": 300,        # one candle = this many seconds (300 = 5 minutes)
    "poll_seconds": 5,            # how often the price is checked (stop-loss runs every check)
    "size_pct": 50,               # % of cash used per buy
    "stop_pct": 1.5,              # sell if price falls this % below the buy price
    "daily_loss_pct": 5,          # stop trading for the day if the account falls this % from the day's start
    "start_cash": 100000,         # paper mode only
    "fee_pct": 0.1,               # assumed fee per trade (paper mode)
    "max_order_inr": 500,         # live mode only: hard cap per order
    "min_order_inr": 100,         # never place an order smaller than this
    "max_consecutive_errors": 5,  # halt after this many failed price checks in a row
    "host": "127.0.0.1",          # keep this unless you run behind an HTTPS proxy on the same machine
    "port": 8080,
    "tz_offset_minutes": 330,     # India time, used for the "day" in the daily loss limit
    "state_file": "state.json",
    "sim_start_price": 7200000,   # sim source only
    "sim_seed": None,             # sim source only
    "sim_shock_at_poll": None,    # sim source only: one-time jump, to test the safety rules
    "sim_shock_pct": -8,
}

PARAM_LIMITS = {
    "fast": (2, 50, int),
    "slow": (3, 200, int),
    "size_pct": (1, 100, float),
    "stop_pct": (0.1, 30, float),
    "daily_loss_pct": (0.5, 50, float),
}


def validate_params(p):
    """Return a cleaned copy of the strategy settings or raise ValueError."""
    out = {}
    for key, (lo, hi, typ) in PARAM_LIMITS.items():
        if key not in p:
            raise ValueError("missing setting: " + key)
        try:
            v = typ(p[key])
        except (TypeError, ValueError):
            raise ValueError(key + " must be a number")
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            raise ValueError(key + " must be a number")
        if not (lo <= v <= hi):
            raise ValueError("%s must be between %s and %s" % (key, lo, hi))
        out[key] = v
    if out["slow"] <= out["fast"]:
        raise ValueError("slow average must be larger than the fast average")
    return out


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError("unknown config keys: " + ", ".join(sorted(unknown)))
        cfg.update(user)
    if cfg["mode"] not in ("paper", "live"):
        raise ValueError('mode must be "paper" or "live"')
    if cfg["price_source"] not in ("coindcx", "sim"):
        raise ValueError('price_source must be "coindcx" or "sim"')
    validate_params(cfg)
    if cfg["mode"] == "live":
        if cfg["price_source"] != "coindcx":
            raise ValueError("live mode needs price_source coindcx")
        if cfg["poll_seconds"] < 3:
            raise ValueError("live mode needs poll_seconds of at least 3")
    return cfg


def sma(a, n, end=None):
    if end is None:
        end = len(a) - 1
    if end + 1 < n or end >= len(a):
        return None
    return sum(a[end - n + 1:end + 1]) / n


def http_json(url, data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def claude_message(prompt, system=None, model=None, max_tokens=512):
    """Send a prompt to Anthropic's Claude API.

    Optional: set ANTHROPIC_API_KEY and optionally CLAUDE_MODEL in the environment.
    Returns the text reply string or raises RuntimeError on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = model or os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20240620")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    data = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        result = http_json("https://api.anthropic.com/v1/messages", data, headers, timeout=30)
    except Exception as exc:
        raise RuntimeError("Claude API request failed: %s" % exc) from exc
    try:
        blocks = result["content"]
        text = "".join(part.get("text", "") for part in blocks if isinstance(part, dict))
        if not text:
            raise RuntimeError("Claude returned no content")
        return text.strip()
    except Exception as exc:
        raise RuntimeError("Could not parse Claude response: %s" % exc) from exc


def summarize_state(engine):
    """Return a concise AI summary of the current bot state if Claude is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "Claude is not configured. Set ANTHROPIC_API_KEY to enable AI analysis."
    state = engine.snapshot()
    safe_state = {
        "mode": state["mode"],
        "market": state["market"],
        "running": state["running"],
        "halted": state["halted"],
        "halt_reason": state["halt_reason"],
        "price": state["price"],
        "equity": state["equity"],
        "cash": state["cash"],
        "qty": state["qty"],
        "entry": state["entry"],
        "candle_no": state["candle_no"],
        "errors": state["errors"],
        "stats": state["stats"],
        "day": state["day"],
        "params": state["params"],
    }
    prompt = (
        "You are a careful crypto trading assistant. Read the current trading bot state below "
        "and give a brief, useful assessment in 4-6 bullet points. Keep it practical, note any risk "
        "or operational issues, and do not give direct financial advice.\n\n"
        + json.dumps(safe_state, sort_keys=True, default=str)
    )
    system = os.environ.get(
        "CLAUDE_SYSTEM_PROMPT",
        "You are a concise and data-focused trading assistant. Explain what the bot is doing, "
        "spot obvious operational issues, and stay brief.",
    )
    try:
        return claude_message(prompt, system=system, max_tokens=300)
    except Exception as exc:
        return "Claude summary unavailable: %s" % exc


# ---------------------------------------------------------------- price feeds

class SimFeed:
    """Made-up prices. For testing only."""

    def __init__(self, start, seed=None, shock_at=None, shock_pct=-8):
        self.p = float(start)
        self.trend = 0.0
        self.rng = random.Random(seed)
        self.n = 0
        self.shock_at = shock_at
        self.shock_pct = shock_pct

    def price(self):
        self.n += 1
        self.trend = self.trend * 0.97 + self.rng.gauss(0, 0.00025)
        self.p = max(1.0, self.p * (1 + self.trend + self.rng.gauss(0, 0.0012)))
        if self.shock_at is not None and self.n == self.shock_at:
            self.p *= 1 + self.shock_pct / 100.0
        return self.p


class CoinDCXFeed:
    """Real last price from CoinDCX's public ticker (no keys needed)."""
    URL = "https://api.coindcx.com/exchange/ticker"

    def __init__(self, market):
        self.market = market

    def price(self):
        for t in http_json(self.URL):
            if t.get("market") == self.market:
                return float(t["last_price"])
        raise RuntimeError("market %s not found in ticker" % self.market)


# ------------------------------------------------------------------ executors

class PaperExec:
    """Pretend orders at the last price, minus an assumed fee."""
    live = False

    def __init__(self, fee_pct):
        self.fee = fee_pct / 100.0

    def buy(self, spend, price):
        fee = spend * self.fee
        return (spend - fee) / price, price, fee

    def sell(self, qty, price):
        gross = qty * price
        fee = gross * self.fee
        return qty, price, fee


class LiveExec:
    """Real market orders on CoinDCX.

    NOT TESTED against a real account in the build session. Check the request
    format against https://docs.coindcx.com before trusting it, and start with
    a very small max_order_inr.
    """
    live = True
    BASE = "https://api.coindcx.com"

    def __init__(self, key, secret, market, max_order_inr):
        self.key, self.secret = key, secret
        self.market = market
        self.max_order = max_order_inr
        self.base_ccy = market[:-3] if market.endswith("INR") else None
        if not self.base_ccy:
            raise ValueError("live mode supports INR markets only (for example BTCINR)")
        self.precision = None

    def _post(self, path, body):
        body = dict(body)
        body["timestamp"] = int(time.time() * 1000)
        raw = json.dumps(body, separators=(",", ":"))
        sig = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json",
                   "X-AUTH-APIKEY": self.key, "X-AUTH-SIGNATURE": sig}
        return http_json(self.BASE + path, raw.encode(), headers)

    def balances(self):
        return {b["currency"]: float(b["balance"])
                for b in self._post("/exchange/v1/users/balances", {})}

    def inr_balance(self):
        return self.balances().get("INR", 0.0)

    def _qty_precision(self):
        if self.precision is None:
            for m in http_json(self.BASE + "/exchange/v1/markets_details"):
                if m.get("coindcx_name") == self.market:
                    self.precision = int(m["target_currency_precision"])
                    break
            if self.precision is None:
                raise RuntimeError("could not read quantity precision for " + self.market)
        return self.precision

    def _floor(self, qty):
        f = 10 ** self._qty_precision()
        return math.floor(qty * f) / f

    def buy(self, spend, price):
        spend = min(spend, self.max_order)
        qty = self._floor(spend / price)
        if qty <= 0:
            raise RuntimeError("order too small for this market")
        self._post("/exchange/v1/orders/create", {
            "side": "buy", "order_type": "market_order",
            "market": self.market, "total_quantity": qty})
        return qty, price, spend * 0.001

    def sell(self, qty, price):
        have = self.balances().get(self.base_ccy, 0.0)
        qty = self._floor(min(qty, have))
        if qty <= 0:
            raise RuntimeError("nothing to sell")
        self._post("/exchange/v1/orders/create", {
            "side": "sell", "order_type": "market_order",
            "market": self.market, "total_quantity": qty})
        return qty, price, qty * price * 0.001


# --------------------------------------------------------------------- alerts

def send_alert(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return

    def go():
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token,
                                   data, timeout=10).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


# --------------------------------------------------------------------- engine

class Engine:
    def __init__(self, cfg, feed, ex, state_path):
        self.cfg, self.feed, self.ex = cfg, feed, ex
        self.p = validate_params(cfg)
        self.state_path = state_path
        self.lock = threading.RLock()
        self.next_candle_at = 0.0
        self.stop_event = threading.Event()
        self.s = self._load()

    # state
    def _fresh(self):
        cash = float(self.cfg["start_cash"]) if not self.ex.live else 0.0
        return {"cash": cash, "qty": 0.0, "entry": 0.0, "entry_fee": 0.0,
                "trades": [], "closes": [], "candle_no": 0,
                "day_key": "", "day_start": cash, "start_equity": cash,
                "halted": False, "halt_reason": "", "running": True,
                "errors": 0, "last_price": 0.0, "notes": []}

    def _load(self):
        s = self._fresh()
        try:
            with open(self.state_path) as f:
                saved = json.load(f)
            if saved.get("mode") == self.cfg["mode"] and saved.get("market") == self.cfg["market"]:
                s.update(saved["state"])
                self.p = validate_params(saved.get("params", self.p))
        except (OSError, ValueError, KeyError):
            pass
        return s

    def save(self):
        data = {"mode": self.cfg["mode"], "market": self.cfg["market"],
                "params": self.p, "state": self.s}
        d = os.path.dirname(os.path.abspath(self.state_path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.state_path)

    # helpers
    def note(self, text):
        self.s["notes"] = (self.s["notes"] + ["%s  %s" % (time.strftime("%H:%M:%S"), text)])[-20:]

    def equity(self):
        return self.s["cash"] + self.s["qty"] * self.s["last_price"]

    def _today(self):
        tz = timezone(timedelta(minutes=self.cfg["tz_offset_minutes"]))
        return datetime.now(tz).strftime("%Y-%m-%d")

    def _roll_day(self):
        if self.s["day_key"] != self._today():
            self.s["day_key"] = self._today()
            self.s["day_start"] = self.equity()
            if self.s["halt_reason"].startswith("Daily loss"):
                self.s["halted"] = False
                self.s["halt_reason"] = ""

    def _sync_cash(self):
        if self.ex.live:
            self.s["cash"] = self.ex.inr_balance()

    def halt(self, reason):
        self.s["halted"] = True
        self.s["halt_reason"] = reason
        self.note("HALTED: " + reason)
        send_alert("Bot halted on %s: %s" % (self.cfg["market"], reason))

    # trading
    def _record(self, side, qty, price, reason, pnl=None):
        t = {"candle": self.s["candle_no"], "side": side, "price": price, "qty": qty,
             "reason": reason, "t": time.time(), "mode": self.cfg["mode"]}
        if pnl is not None:
            t["pnl"] = pnl
        self.s["trades"] = (self.s["trades"] + [t])[-200:]
        send_alert("%s %s %.6f @ %.2f (%s)%s" % (side, self.cfg["market"], qty, price, reason,
                                                 "" if pnl is None else "  P/L %.2f" % pnl))

    def _buy(self, price, reason):
        spend = self.s["cash"] * self.p["size_pct"] / 100.0
        if self.ex.live:
            spend = min(spend, self.cfg["max_order_inr"])
        if spend < self.cfg["min_order_inr"]:
            self.note("skipped buy: order below minimum")
            return
        try:
            qty, fill, fee = self.ex.buy(spend, price)
        except Exception as e:
            self.halt("Buy failed: %s" % e)
            return
        self.s["cash"] -= spend
        self.s["qty"], self.s["entry"], self.s["entry_fee"] = qty, fill, fee
        self._record("BUY", qty, fill, reason)
        try:
            self._sync_cash()
        except Exception:
            pass

    def _sell(self, price, reason):
        if self.s["qty"] <= 0:
            return
        try:
            qty, fill, fee = self.ex.sell(self.s["qty"], price)
        except Exception as e:
            self.halt("Sell failed, check your account now: %s" % e)
            return
        gross = qty * fill
        pnl = gross - fee - (qty * self.s["entry"] + self.s["entry_fee"])
        self.s["cash"] += gross - fee
        self.s["qty"] = max(0.0, self.s["qty"] - qty)
        if self.s["qty"] < 1e-12:
            self.s["qty"], self.s["entry"], self.s["entry_fee"] = 0.0, 0.0, 0.0
        self._record("SELL", qty, fill, reason, pnl)
        try:
            self._sync_cash()
        except Exception:
            pass

    def step(self):
        """One price check. Called every poll_seconds."""
        with self.lock:
            try:
                price = self.feed.price()
            except Exception as e:
                self.s["errors"] += 1
                self.note("price error: %s" % e)
                if self.s["errors"] >= self.cfg["max_consecutive_errors"] and not self.s["halted"]:
                    self.halt("Too many price errors in a row")
                self.save()
                return
            self.s["errors"] = 0
            self.s["last_price"] = price
            if self.s["start_equity"] == 0 and self.ex.live:
                self._sync_cash()
                self.s["start_equity"] = self.equity()
            self._roll_day()

            now = time.time()
            new_candle = now >= self.next_candle_at
            if new_candle:
                self.s["closes"] = (self.s["closes"] + [price])[-300:]
                self.s["candle_no"] += 1
                self.next_candle_at = now + self.cfg["candle_seconds"]

            if self.s["running"] and not self.s["halted"]:
                closes, f, sl = self.s["closes"], self.p["fast"], self.p["slow"]
                if self.s["qty"] > 0 and price <= self.s["entry"] * (1 - self.p["stop_pct"] / 100.0):
                    self._sell(price, "Stop-loss hit")
                elif new_candle and len(closes) >= sl + 1:
                    f1, s1 = sma(closes, f), sma(closes, sl)
                    f0, s0 = sma(closes, f, len(closes) - 2), sma(closes, sl, len(closes) - 2)
                    if self.s["qty"] == 0 and f0 <= s0 and f1 > s1:
                        self._buy(price, "Fast average crossed above slow")
                    elif self.s["qty"] > 0 and f0 >= s0 and f1 < s1:
                        self._sell(price, "Fast average crossed below slow")
                if not self.s["halted"] and self.equity() <= self.s["day_start"] * (1 - self.p["daily_loss_pct"] / 100.0):
                    self._sell(price, "Daily loss limit reached")
                    self.halt("Daily loss limit reached")
            self.save()

    # controls
    def start(self):
        with self.lock:
            if self.s["halted"]:
                self.s["halted"], self.s["halt_reason"] = False, ""
                self.s["day_start"] = self.equity()
            self.s["running"] = True
            self.save()

    def stop(self):
        with self.lock:
            self.s["running"] = False
            self.save()

    def kill(self):
        """Sell everything and stop."""
        with self.lock:
            self._sell(self.s["last_price"], "Sell all and stop")
            self.s["running"] = False
            self.save()

    def set_params(self, new):
        with self.lock:
            merged = dict(self.p)
            merged.update(new)
            self.p = validate_params(merged)
            self.save()

    def snapshot(self):
        with self.lock:
            s = self.s
            sells = [t for t in s["trades"] if t["side"] == "SELL"]
            eq = self.equity()
            return {
                "mode": self.cfg["mode"], "market": self.cfg["market"],
                "source": self.cfg["price_source"],
                "running": s["running"], "halted": s["halted"], "halt_reason": s["halt_reason"],
                "price": s["last_price"], "equity": eq, "start_equity": s["start_equity"],
                "cash": s["cash"], "qty": s["qty"], "entry": s["entry"],
                "closes": s["closes"][-110:], "candle_no": s["candle_no"],
                "candle_seconds": self.cfg["candle_seconds"], "params": dict(self.p),
                "trades": s["trades"][-30:],
                "stats": {"closed": len(sells),
                          "wins": sum(1 for t in sells if t["pnl"] > 0),
                          "realised": sum(t["pnl"] for t in sells)},
                "day": {"start": s["day_start"], "pnl": eq - s["day_start"],
                        "limit": s["day_start"] * self.p["daily_loss_pct"] / 100.0},
                "errors": s["errors"], "notes": s["notes"][-5:], "server_time": time.time(),
            }

    def run_forever(self):
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception as e:  # never let the loop die silently
                with self.lock:
                    self.note("internal error: %s" % e)
            self.stop_event.wait(self.cfg["poll_seconds"])


# ------------------------------------------------------------------ http api

def make_handler(engine, token):
    page_path = os.path.join(HERE, "dashboard.html")

    class H(BaseHTTPRequestHandler):
        server_version = "bot"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self):
            got = self.headers.get("Authorization", "")
            ok = got.startswith("Bearer ") and hmac.compare_digest(
                got[7:].encode(), token.encode())
            if not ok:
                time.sleep(1)  # slow down guessing
                self._send(401, {"error": "wrong password"})
            return ok

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                if self._authed():
                    self._send(200, engine.snapshot())
                return
            if self.path == "/api/ai-summary":
                if self._authed():
                    self._send(200, {"summary": summarize_state(engine)})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/api/") or not self._authed():
                if not self.path.startswith("/api/"):
                    self._send(404, {"error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                if self.path == "/api/start":
                    engine.start()
                elif self.path == "/api/stop":
                    engine.stop()
                elif self.path == "/api/kill":
                    engine.kill()
                elif self.path == "/api/params":
                    engine.set_params(body)
                elif self.path == "/api/ai-summary":
                    self._send(200, {"summary": summarize_state(engine)})
                else:
                    return self._send(404, {"error": "not found"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            self._send(200, engine.snapshot())

    return H


def build_engine(cfg, state_path=None):
    if cfg["price_source"] == "sim":
        feed = SimFeed(cfg["sim_start_price"], cfg["sim_seed"],
                      cfg["sim_shock_at_poll"], cfg["sim_shock_pct"])
    else:
        feed = CoinDCXFeed(cfg["market"])
    if cfg["mode"] == "live":
        key, secret = os.environ.get("COINDCX_KEY"), os.environ.get("COINDCX_SECRET")
        if not key or not secret:
            raise SystemExit("Live mode needs COINDCX_KEY and COINDCX_SECRET in the environment.")
        if os.environ.get("LIVE_CONFIRM") != "I-UNDERSTAND-THIS-USES-REAL-MONEY":
            raise SystemExit("Live mode also needs LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY")
        ex = LiveExec(key, secret, cfg["market"], cfg["max_order_inr"])
        try:
            print("Live check passed. INR balance:", ex.inr_balance())
        except Exception as e:
            raise SystemExit("Could not read your CoinDCX balance, not starting: %s" % e)
    else:
        ex = PaperExec(cfg["fee_pct"])
    path = state_path or os.path.join(HERE, cfg["state_file"])
    return Engine(cfg, feed, ex, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    token = os.environ.get("DASH_TOKEN", "")
    if len(token) < 12:
        raise SystemExit("Set DASH_TOKEN to a password of at least 12 characters.")
    engine = build_engine(cfg)
    threading.Thread(target=engine.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(engine, token))
    print("%s mode, %s, prices from %s. Dashboard on http://%s:%d" % (
        cfg["mode"].upper(), cfg["market"], cfg["price_source"], cfg["host"], cfg["port"]))
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("Claude AI summary enabled. Use /api/ai-summary with the dashboard token.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop_event.set()
        engine.save()


if __name__ == "__main__":
    main()
