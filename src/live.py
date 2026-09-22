"""
Live depth feed.

Maintains a real order book from a streaming venue and exposes snapshots to the
terminal, so the Bookmap-style liquidity heatmap has something moving to draw.

Sources
-------
binance   free, public, no API key, full depth + tape. NOT Nasdaq - it is here
          so the engine can be proven end to end at zero cost before paying for
          CME data.
rithmic   the real target for NQ/MNQ. Not implemented yet: it needs a broker
          account and a CME depth subscription. The consumer side below is
          source-agnostic, so wiring it in means writing one feed class.

The book state, the liquidity heatmap and the event detection all live in
book.py and are shared with the historical path - deliberately, so that what is
validated live is the same code that runs in the backtest.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque

import book as bookmod

BINANCE_WS = "wss://fstream.binance.com/stream?streams={streams}"


class LiveFeed:
    """Background thread keeping a live order book plus rolling snapshots."""

    def __init__(self, symbol="btcusdt", tick_size=0.1, depth_levels=40,
                 snapshot_ms=1000, history=600):
        self.symbol = symbol.lower()
        self.tick_size = tick_size
        self.depth_levels = depth_levels
        self.snapshot_ms = snapshot_ms
        self.book = bookmod.Book(tick_size)
        self.snaps = deque(maxlen=history)
        self.trades = deque(maxlen=6000)
        self.cvd = 0.0
        self.cvd_series = deque(maxlen=900)
        self.events = deque(maxlen=400)
        self.lock = threading.Lock()
        self.running = False
        self.connected = False
        self.error = None
        self.messages = 0
        self._thread = None
        self._last_snap = 0.0
        self._tick_checked = False

    # ---------- lifecycle ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    # ---------- feed ----------
    def _run(self):
        try:
            import asyncio

            import websockets
        except ImportError as exc:
            self.error = f"missing library: {exc}"
            self.running = False
            return

        async def consume():
            # Binance USD-M futures publishes single prints as @trade; @aggTrade
            # exists on spot but stays silent here, which cost us a debug round.
            streams = f"{self.symbol}@depth20@100ms/{self.symbol}@trade"
            url = BINANCE_WS.format(streams=streams)
            backoff = 1
            while self.running:
                try:
                    async with websockets.connect(url, ping_interval=15,
                                                  close_timeout=5) as ws:
                        self.connected = True
                        self.error = None
                        backoff = 1
                        while self.running:
                            raw = await ws.recv()
                            self._handle(json.loads(raw))
                except Exception as exc:  # noqa: BLE001 - keep the feed alive
                    self.connected = False
                    self.error = f"{type(exc).__name__}: {exc}"
                    if not self.running:
                        break
                    time.sleep(min(backoff, 20))
                    backoff *= 2
            self.connected = False

        asyncio.new_event_loop().run_until_complete(consume())

    def _handle(self, msg):
        data = msg.get("data") or msg
        stream = msg.get("stream", "")
        now = time.time()
        self.messages += 1

        if "depth" in stream:
            ts_us = int(data.get("E", now * 1000)) * 1000
            if not self._tick_checked:
                self._refine_tick(data)
            with self.lock:
                # depth20 is a full top-20 snapshot, so rebuild rather than patch
                self.book.bids.clear()
                self.book.asks.clear()
                for px, qty in data.get("b", []):
                    q = float(qty)
                    if q > 0:
                        self.book.apply((ts_us, 0, float(px), q, 1))
                for px, qty in data.get("a", []):
                    q = float(qty)
                    if q > 0:
                        self.book.apply((ts_us, 1, float(px), q, 1))

                if (now - self._last_snap) * 1000 >= self.snapshot_ms:
                    self._last_snap = now
                    b, a = self.book.depth(self.depth_levels)
                    self.snaps.append({"ts": ts_us, "bids": b, "asks": a})
                    if len(self.snaps) >= 3:
                        found = bookmod.detect_events(list(self.snaps)[-3:], self.tick_size)
                        for e in found:
                            self.events.append(e)

        elif "@trade" in stream or data.get("e") in ("trade", "aggTrade"):
            with self.lock:
                qty = float(data.get("q", 0))
                # Binance 'm' = buyer was the maker, so the aggressor sold
                sell = bool(data.get("m"))
                self.cvd += -qty if sell else qty
                self.trades.append({
                    "ts": int(data.get("T", now * 1000)),
                    "price": float(data.get("p", 0)),
                    "qty": qty,
                    "side": "SELL" if sell else "BUY",
                })
                if not self.cvd_series or (now - self.cvd_series[-1][0]) >= 1.0:
                    self.cvd_series.append((now, self.cvd))

    def _refine_tick(self, data):
        """Derive the real tick size from consecutive book levels.

        A wrong tick collapses every price into one heatmap row, so this runs
        before the first snapshot is taken.
        """
        px = []
        for side in ("b", "a"):
            for entry in data.get(side, [])[:12]:
                try:
                    px.append(float(entry[0]))
                except (TypeError, ValueError, IndexError):
                    pass
        if len(px) < 4:
            return
        px = sorted(set(px))
        diffs = [round(b - a, 10) for a, b in zip(px, px[1:]) if b - a > 1e-12]
        if not diffs:
            return
        tick = min(diffs)
        if tick > 0 and abs(tick - self.tick_size) / max(tick, 1e-12) > 0.01:
            self.tick_size = tick
            self.book = bookmod.Book(tick)
            self.snaps.clear()
            self.events.clear()
        self._tick_checked = True

    # ---------- readers ----------
    def status(self):
        with self.lock:
            bb, ba = self.book.best_bid(), self.book.best_ask()
            micro = self.book.microprice()
            return {
                "symbol": self.symbol.upper(),
                "running": self.running,
                "connected": self.connected,
                "error": self.error,
                "messages": self.messages,
                "snapshots": len(self.snaps),
                "trades": len(self.trades),
                "events": len(self.events),
                "best_bid": bb * self.tick_size if bb is not None else None,
                "best_ask": ba * self.tick_size if ba is not None else None,
                "microprice": micro,
                "imbalance": round(self.book.imbalance(), 4),
                "tick_size": self.tick_size,
            }

    def heatmap(self):
        with self.lock:
            snaps = list(self.snaps)
        return bookmod.liquidity_heatmap(snaps, self.tick_size)

    def dom(self, levels=20):
        with self.lock:
            b, a = self.book.depth(levels)
        return {"bids": [{"price": p, "size": s} for p, s in b],
                "asks": [{"price": p, "size": s} for p, s in a]}

    def recent_trades(self, n=60):
        with self.lock:
            return list(self.trades)[-n:][::-1]

    def recent_events(self, n=40):
        with self.lock:
            return list(self.events)[-n:][::-1]


_FEEDS = {}
_FEEDS_LOCK = threading.Lock()

# Tick sizes for the venues we can stream for free.
# Starting tick sizes. The feed refines these from the live book, so a wrong
# guess self-corrects within the first second instead of wrecking the heatmap.
TICKS = {
    "btcusdt": 0.1, "ethusdt": 0.01, "solusdt": 0.01, "bnbusdt": 0.01,
    "xrpusdt": 0.0001, "dogeusdt": 0.00001, "adausdt": 0.0001,
    "avaxusdt": 0.001, "linkusdt": 0.001, "suiusdt": 0.0001,
}

SYMBOLS = [
    {"symbol": "btcusdt", "label": "BTC/USDT"},
    {"symbol": "ethusdt", "label": "ETH/USDT"},
    {"symbol": "solusdt", "label": "SOL/USDT"},
    {"symbol": "bnbusdt", "label": "BNB/USDT"},
    {"symbol": "xrpusdt", "label": "XRP/USDT"},
    {"symbol": "dogeusdt", "label": "DOGE/USDT"},
    {"symbol": "adausdt", "label": "ADA/USDT"},
    {"symbol": "avaxusdt", "label": "AVAX/USDT"},
    {"symbol": "linkusdt", "label": "LINK/USDT"},
    {"symbol": "suiusdt", "label": "SUI/USDT"},
]


def get_feed(symbol="btcusdt"):
    symbol = symbol.lower()
    with _FEEDS_LOCK:
        f = _FEEDS.get(symbol)
        if f is None:
            f = LiveFeed(symbol, tick_size=TICKS.get(symbol, 0.1))
            _FEEDS[symbol] = f
            f.start()
        return f


def stop_all():
    with _FEEDS_LOCK:
        for f in _FEEDS.values():
            f.stop()
        _FEEDS.clear()


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "btcusdt"
    f = get_feed(sym)
    print(f"lacze z Binance: {sym} ... (Ctrl+C konczy)")
    try:
        for _ in range(12):
            time.sleep(2)
            s = f.status()
            print(f"  polaczony={s['connected']} wiadomosci={s['messages']:>6} "
                  f"migawek={s['snapshots']:>4} bid={s['best_bid']} ask={s['best_ask']} "
                  f"imb={s['imbalance']:+.3f} zdarzen={s['events']}")
            if s["error"]:
                print(f"  blad: {s['error']}")
    except KeyboardInterrupt:
        pass
    f.stop()


# --------------------------------------------------------------------------
# Live tape analytics: the order flow mechanics, computed on the rolling
# window of prints rather than on a file. Same definitions as orderflow.py so
# what is seen live matches what a backtest would have seen.
# --------------------------------------------------------------------------

def _tape_stats(trades, tick_size):
    if not trades:
        return None
    qty = [t["qty"] for t in trades]
    med = sorted(qty)[len(qty) // 2] or 1e-9
    buy = sum(t["qty"] for t in trades if t["side"] == "BUY")
    sell = sum(t["qty"] for t in trades if t["side"] == "SELL")
    total = buy + sell
    return {
        "prints": len(trades),
        "buy_volume": round(buy, 4),
        "sell_volume": round(sell, 4),
        "delta": round(buy - sell, 4),
        "imbalance": round((buy - sell) / total, 4) if total else 0.0,
        "median_print": round(med, 6),
    }


def _live_footprint(trades, tick_size, max_levels=60):
    """Bid/ask volume per price level over the rolling window."""
    if not trades:
        return {"levels": [], "tick_size": tick_size}
    agg = {}
    for t in trades:
        lv = round(t["price"] / tick_size)
        cell = agg.setdefault(lv, [0.0, 0.0])
        if t["side"] == "BUY":
            cell[1] += t["qty"]
        else:
            cell[0] += t["qty"]
    rows = sorted(agg.items())
    if len(rows) > max_levels:
        rows = rows[-max_levels:]
    peak_lv = max(agg.items(), key=lambda kv: kv[1][0] + kv[1][1])[0]
    return {
        "levels": [
            {"price": round(lv * tick_size, 6), "bid": round(b, 4), "ask": round(a, 4),
             "delta": round(a - b, 4), "poc": lv == peak_lv}
            for lv, (b, a) in rows
        ],
        "tick_size": tick_size,
    }


def _live_large(trades, min_multiple=6.0, top=40):
    if not trades:
        return []
    qty = sorted(t["qty"] for t in trades)
    med = qty[len(qty) // 2] or 1e-9
    out = [
        {**t, "multiple": round(t["qty"] / med, 1)}
        for t in trades if t["qty"] >= med * min_multiple
    ]
    return out[-top:][::-1]


def _live_sweeps(trades, window_ms=400, min_levels=3, top=25):
    """Aggressor clearing several price levels inside a short window."""
    if len(trades) < min_levels:
        return []
    out = []
    i = 0
    n = len(trades)
    while i < n:
        j = i
        end = trades[i]["ts"] + window_ms
        while j + 1 < n and trades[j + 1]["ts"] <= end:
            j += 1
        if j - i + 1 >= min_levels:
            seg = trades[i:j + 1]
            sides = {t["side"] for t in seg}
            prices = {t["price"] for t in seg}
            if len(sides) == 1 and len(prices) >= min_levels:
                out.append({
                    "ts": seg[0]["ts"],
                    "side": seg[0]["side"],
                    "levels": len(prices),
                    "volume": round(sum(t["qty"] for t in seg), 4),
                    "price_from": seg[0]["price"],
                    "price_to": seg[-1]["price"],
                })
                i = j + 1
                continue
        i += 1
    return out[-top:][::-1]


def _attach(cls):
    def tape_stats(self, n=600):
        with self.lock:
            tr = list(self.trades)[-n:]
        return _tape_stats(tr, self.tick_size)

    def footprint(self, n=1200):
        with self.lock:
            tr = list(self.trades)[-n:]
        return _live_footprint(tr, self.tick_size)

    def large(self, n=1200):
        with self.lock:
            tr = list(self.trades)[-n:]
        return _live_large(tr)

    def sweeps(self, n=1200):
        with self.lock:
            tr = list(self.trades)[-n:]
        return _live_sweeps(tr)

    def cvd_curve(self):
        with self.lock:
            pts = list(self.cvd_series)
        if not pts:
            return {"points": [], "cvd": 0.0}
        t0 = pts[0][0]
        return {"points": [[round(t - t0, 1), round(v, 4)] for t, v in pts],
                "cvd": round(pts[-1][1], 4)}

    for fn in (tape_stats, footprint, large, sweeps, cvd_curve):
        setattr(cls, fn.__name__, fn)


_attach(LiveFeed)
