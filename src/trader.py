"""
Live paper trader.

Runs in the background against the live Binance feed: reads the book through
decision.py, opens and closes virtual positions, tracks equity. No real orders,
no API key, no money.

Defaults are the user's demo brief: 1000 USD of virtual capital, 10x leverage,
three entries per day, costs switched off.

Two things are simulated even in "no costs" mode, because leaving them out
would make the demo lie:

  LIQUIDATION  at 10x, a 10% move against the position wipes the account. The
               engine closes at the liquidation price rather than letting
               equity go negative.
  NEXT-PRICE   entries and exits fill at the price seen AFTER the decision, not
               the price that produced it.

Costs are off by default but fully wired: set fee_bps and every trade pays
entry and exit fees on notional, so the same run can be replayed honestly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

import decision
import journal as journalmod
import live as livemod

DEFAULTS = {
    "capital": 1000.0,
    "leverage": 10.0,
    "max_trades_per_day": 3,
    "fee_bps": 0.0,          # 0 = demo. Binance perp reality: 10 taker, 4 maker.
    "target_pct": 0.50,      # % move in our favour -> take profit
    "stop_pct": 0.30,        # % move against -> cut
    "max_hold_s": 1800,      # do not sit in a position forever
    "min_score": 1.2,        # decision score needed to act
    "maintenance_margin": 0.005,
    # Stability guards. Without these the bot flips side every tick and burns
    # the daily budget in half a minute - which is exactly what happened on the
    # first run.
    "confirm_ticks": 5,      # signal must hold the same side this many seconds
    "cooldown_s": 90,        # wait after closing before entering again
    "flip_factor": 1.8,      # reversal must be this much stronger than min_score
    # Position sizing. compound=1 sizes off current equity, so profits enlarge
    # the next position and losses shrink it. That is what the user asked for,
    # but it cuts both ways: the largest position always sits right before the
    # first big loss. max_notional caps it; 0 means no cap.
    "compound": 1,
    "max_notional": 0.0,
}


class LiveTrader:
    def __init__(self, symbol="btcusdt", **cfg):
        self.symbol = symbol.lower()
        self.cfg = {**DEFAULTS, **cfg}
        self.feed = livemod.get_feed(self.symbol)
        self.journal = journalmod.get_journal(self.symbol)

        self.equity = self.cfg["capital"]
        self.start_equity = self.cfg["capital"]
        self.position = None
        self.trades = deque(maxlen=300)
        self.curve = deque(maxlen=3000)
        self.day = None
        self.day_trades = 0
        self.last_decision = None
        self.liquidations = 0
        self._streak_side = 0
        self._streak = 0
        self._last_close = 0.0
        self.peak_equity = self.cfg["capital"]
        self.max_dd = 0.0

        self.running = False
        self.lock = threading.Lock()
        self._thread = None

    # ---------- lifecycle ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def reset(self):
        with self.lock:
            self.equity = self.cfg["capital"]
            self.start_equity = self.cfg["capital"]
            self.position = None
            self.trades.clear()
            self.curve.clear()
            self.day_trades = 0
            self.liquidations = 0
            self.peak_equity = self.cfg["capital"]
            self.max_dd = 0.0
        self.save()

    def configure(self, **kw):
        with self.lock:
            for k, v in kw.items():
                if k in self.cfg and v is not None:
                    self.cfg[k] = type(self.cfg[k])(v)

    # ---------- engine ----------
    def _price(self):
        st = self.feed.status()
        mid = st.get("microprice")
        if mid:
            return float(mid)
        bb, ba = st.get("best_bid"), st.get("best_ask")
        if bb and ba:
            return (bb + ba) / 2
        return None

    def _observe(self):
        """Build observations from the live book and tape."""
        st = self.feed.status()
        obs = []
        bi = st.get("imbalance") or 0.0
        if abs(bi) > 0.1:
            obs.append(decision.Observation(
                "book_imbalance", 1 if bi > 0 else -1,
                decision.WEIGHTS["book_imbalance"] * min(abs(bi), 1.0),
                f"book {bi:+.2f}", "book"))

        for e in self.feed.recent_events(12):
            side = 1 if e.get("side") == 0 else -1
            kind = e.get("kind")
            if kind == "wall":
                obs.append(decision.Observation("wall_ahead", side,
                    decision.WEIGHTS["wall_ahead"], f"wall @ {e.get('price')}", "book"))
            elif kind == "pulled":
                obs.append(decision.Observation("wall_pulled", -side,
                    decision.WEIGHTS["wall_pulled"], f"pulled @ {e.get('price')}", "book"))
            elif kind == "iceberg":
                obs.append(decision.Observation("iceberg", side,
                    decision.WEIGHTS["iceberg"], f"iceberg @ {e.get('price')}", "book"))

        tape = self.feed.tape_stats(400) or {}
        imb = tape.get("imbalance")
        if imb is not None and abs(imb) > 0.12:
            obs.append(decision.Observation("tape_imbalance", 1 if imb > 0 else -1,
                0.4 * min(abs(imb), 1.0), f"tape {imb:+.2f}", "tape"))

        for s in self.feed.sweeps()[:2]:
            obs.append(decision.Observation("sweep", 1 if s["side"] == "BUY" else -1,
                decision.WEIGHTS["sweep"], f"sweep {s['side']} / {s['levels']} lvls", "tape"))

        for t in self.feed.large()[:3]:
            obs.append(decision.Observation("large_print", 1 if t["side"] == "BUY" else -1,
                decision.WEIGHTS["large_print"], f"large {t['side']} x{t['multiple']}", "tape"))
        return obs

    def next_notional(self):
        """Size of the position that would be opened right now."""
        c = self.cfg
        base = self.equity if c["compound"] else c["capital"]
        notional = base * c["leverage"]
        if c["max_notional"] > 0:
            notional = min(notional, c["max_notional"])
        return notional

    def _open(self, side, price, why):
        c = self.cfg
        notional = self.next_notional()
        qty = notional / price
        fee = notional * c["fee_bps"] / 10_000.0
        # Liquidation: loss of (equity - maintenance) wipes the account.
        move = (1.0 / c["leverage"]) - c["maintenance_margin"]
        liq = price * (1 - move) if side > 0 else price * (1 + move)
        self.position = {
            "side": side, "entry": price, "qty": qty, "notional": notional,
            "opened": time.time(), "fee_paid": fee, "liq": liq, "why": why,
        }
        self.equity -= fee
        self.day_trades += 1

    def _close(self, price, reason):
        p = self.position
        if not p:
            return
        c = self.cfg
        gross = (price - p["entry"]) * p["side"] * p["qty"]
        fee = p["notional"] * c["fee_bps"] / 10_000.0
        net = gross - fee
        self.equity += net
        if self.equity < 0:
            self.equity = 0.0
        self.peak_equity = max(self.peak_equity, self.equity)
        dd = self.equity - self.peak_equity
        self.max_dd = min(self.max_dd, dd)
        self.trades.appendleft({
            "opened": p["opened"], "closed": time.time(),
            "side": "LONG" if p["side"] > 0 else "SHORT",
            "entry": round(p["entry"], 2), "exit": round(price, 2),
            "qty": round(p["qty"], 6), "notional": round(p["notional"], 2),
            "pct": round((price - p["entry"]) / p["entry"] * p["side"] * 100, 3),
            "gross": round(gross, 2),
            "fees": round(p["fee_paid"] + fee, 2),
            "net": round(net - p["fee_paid"], 2),
            "reason": reason,
            "equity": round(self.equity, 2),
            "why": p["why"],
        })
        if reason == "liquidation":
            self.liquidations += 1
        self.position = None
        self._last_close = time.time()
        self._streak_side, self._streak = 0, 0

    def _loop(self):
        last_save = 0.0
        while self.running:
            try:
                self._tick()
                now = time.time()
                if now - last_save >= 30:
                    last_save = now
                    self.save()
            except Exception:  # noqa: BLE001 - a demo must not die on one bad tick
                pass
            time.sleep(1.0)
        self.save()

    def _tick(self):
        price = self._price()
        if not price:
            return
        now = time.time()
        today = int(now // 86400)

        with self.lock:
            if self.day != today:
                self.day = today
                self.day_trades = 0

            obs = self._observe()
            d = decision.decide(obs, threshold=self.cfg["min_score"])
            d["explain"] = decision.explain(d)
            d["observations"] = obs
            self.last_decision = d
            # Learning memory: store what was seen, and let the journal fill in
            # what the price did next once the horizons elapse.
            self.journal.record(price, obs, d["score"], d["action"])
            self.journal.resolve(price)

            # signal stability: count how long one side has held
            act = d["action"]
            if act != 0 and act == self._streak_side:
                self._streak += 1
            elif act != 0:
                self._streak_side, self._streak = act, 1
            else:
                self._streak_side, self._streak = 0, 0
            d["streak"] = self._streak
            d["confirmed"] = self._streak >= self.cfg["confirm_ticks"]

            p = self.position
            if p:
                side = p["side"]
                move_pct = (price - p["entry"]) / p["entry"] * side * 100
                hit_liq = (price <= p["liq"]) if side > 0 else (price >= p["liq"])
                if hit_liq:
                    self._close(p["liq"], "liquidation")
                elif move_pct >= self.cfg["target_pct"]:
                    self._close(price, "target")
                elif move_pct <= -self.cfg["stop_pct"]:
                    self._close(price, "stop")
                elif now - p["opened"] > self.cfg["max_hold_s"]:
                    self._close(price, "time stop")
                elif (d["action"] != 0 and d["action"] != side
                      and abs(d["score"]) >= self.cfg["min_score"] * self.cfg["flip_factor"]
                      and self._streak >= self.cfg["confirm_ticks"]):
                    self._close(price, "signal flip")
            elif (d["action"] != 0
                  and d["confirmed"]
                  and now - self._last_close >= self.cfg["cooldown_s"]
                  and self.day_trades < self.cfg["max_trades_per_day"]
                  and self.equity > 1.0):
                self._open(d["action"], price, d["explain"])

            self.curve.append((now, round(self.equity + self._unreal(price), 2)))

    def _unreal(self, price):
        p = self.position
        if not p:
            return 0.0
        return (price - p["entry"]) * p["side"] * p["qty"]

    # ---------- persistence ----------
    @property
    def state_path(self):
        return os.path.join(STATE_DIR, f"trader_{self.symbol}.json")

    def save(self):
        """Persist equity and history so a restart does not reset the demo."""
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with self.lock:
                payload = {
                    "equity": self.equity,
                    "start_equity": self.start_equity,
                    "peak_equity": self.peak_equity,
                    "max_dd": self.max_dd,
                    "day": self.day,
                    "day_trades": self.day_trades,
                    "liquidations": self.liquidations,
                    "trades": list(self.trades)[:150],
                    "cfg": dict(self.cfg),
                    "saved_at": time.time(),
                }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.state_path)     # atomic: never a torn file
        except OSError:
            pass

    def restore(self):
        """Load a previous session, if one exists."""
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False
        with self.lock:
            self.equity = float(d.get("equity", self.cfg["capital"]))
            self.start_equity = float(d.get("start_equity", self.cfg["capital"]))
            self.peak_equity = float(d.get("peak_equity", self.equity))
            self.max_dd = float(d.get("max_dd", 0.0))
            self.day = d.get("day")
            self.day_trades = int(d.get("day_trades", 0))
            self.liquidations = int(d.get("liquidations", 0))
            for t in reversed(d.get("trades", [])):
                self.trades.appendleft(t)
            for k, v in (d.get("cfg") or {}).items():
                if k in self.cfg:
                    self.cfg[k] = v
            # An open position is deliberately NOT restored: while the server
            # was down nobody was watching the stop, so carrying it over would
            # invent a result that never happened.
            self.position = None
        return True

    # ---------- readers ----------
    def state(self):
        price = self._price()
        with self.lock:
            p = self.position
            unreal = self._unreal(price) if price else 0.0
            pos = None
            if p:
                pos = {
                    "side": "LONG" if p["side"] > 0 else "SHORT",
                    "entry": round(p["entry"], 2),
                    "price": round(price, 2) if price else None,
                    "qty": round(p["qty"], 6),
                    "notional": round(p["notional"], 2),
                    "liq": round(p["liq"], 2),
                    "pct": round((price - p["entry"]) / p["entry"] * p["side"] * 100, 3) if price else 0,
                    "unreal": round(unreal, 2),
                    "held_s": int(time.time() - p["opened"]),
                    "why": p["why"],
                }
            wins = [t for t in self.trades if t["net"] > 0]
            total_net = sum(t["net"] for t in self.trades)
            return {
                "symbol": self.symbol.upper(),
                "running": self.running,
                "config": dict(self.cfg),
                "equity": round(self.equity, 2),
                "equity_live": round(self.equity + unreal, 2),
                "start_equity": self.start_equity,
                "pnl": round(self.equity + unreal - self.start_equity, 2),
                "pnl_pct": round((self.equity + unreal - self.start_equity)
                                 / max(self.start_equity, 1e-9) * 100, 2),
                "position": pos,
                "day_trades": self.day_trades,
                "trades_left": max(0, self.cfg["max_trades_per_day"] - self.day_trades),
                "closed_trades": len(self.trades),
                "wins": len(wins),
                "win_rate": round(len(wins) / len(self.trades) * 100, 1) if self.trades else None,
                "total_net": round(total_net, 2),
                "liquidations": self.liquidations,
                "decision": self.last_decision,
                "costs_on": self.cfg["fee_bps"] > 0,
                "next_notional": round(self.next_notional(), 2),
                "peak_equity": round(self.peak_equity, 2),
                "max_drawdown": round(self.max_dd, 2),
                "max_drawdown_pct": round(self.max_dd / max(self.peak_equity, 1e-9) * 100, 2),
                "compound": bool(self.cfg["compound"]),
                "streak": self._streak,
                "cooldown_left": max(0, int(self.cfg["cooldown_s"]
                                            - (time.time() - self._last_close))),
                "journal": self.journal.stats(),
            }

    def history(self, n=60):
        with self.lock:
            return list(self.trades)[:n]

    def equity_curve(self, n=900):
        with self.lock:
            pts = list(self.curve)[-n:]
        if not pts:
            return {"points": []}
        t0 = pts[0][0]
        return {"points": [[round(t - t0, 1), v] for t, v in pts],
                "start": self.start_equity}


STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "state")


_TRADERS = {}
_LOCK = threading.Lock()


def get_trader(symbol="btcusdt", **cfg):
    symbol = symbol.lower()
    with _LOCK:
        t = _TRADERS.get(symbol)
        if t is None:
            t = LiveTrader(symbol, **cfg)
            t.restore()
            _TRADERS[symbol] = t
            t.start()
        return t
