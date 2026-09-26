"""
Binance historical bars — with REAL aggressor volume.

This matters more than it looks. Yahoo gives OHLCV only, so delta has to be
guessed with the tick rule, and that guess made the imbalance filter useless
(every bar scored +/-1.0). Binance klines carry `taker_buy_base_volume`: the
volume that traded against resting asks, i.e. aggressive buying, reported by
the exchange.

So:
    ask_volume = taker_buy_base_volume          (aggressive buying)
    bid_volume = volume - taker_buy_base_volume (aggressive selling)
    delta      = 2 * taker_buy - volume

That is exchange truth, not inference — the same quality as the .scid files,
and free. It means backtests and paper trading on crypto measure real order
flow rather than a proxy for candle direction.

No API key needed. Public REST endpoint, paginated.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np

FAPI = "https://fapi.binance.com/fapi/v1/klines"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "binance")
CACHE_TTL = 600

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def _get(url, params, retries=3):
    q = urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "tape-and-book/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001 - transient network, retry
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Binance REST not responding: {last}")


def fetch_klines(symbol="BTCUSDT", interval="5m", bars=1500, force=False):
    """Download up to `bars` klines, paginating backwards. Cached on disk."""
    symbol = symbol.upper()
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{bars}.npz")

    if not force and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        z = np.load(path, allow_pickle=False)
        return _to_tape(z["ts"], z["o"], z["h"], z["l"], z["c"], z["v"], z["tb"])

    step = INTERVAL_MS.get(interval, 300_000)
    rows = []
    end = None
    while len(rows) < bars:
        params = {"symbol": symbol, "interval": interval,
                  "limit": min(1500, bars - len(rows))}
        if end is not None:
            params["endTime"] = end
        chunk = _get(FAPI, params)
        if not chunk:
            break
        rows = chunk + rows
        end = int(chunk[0][0]) - 1
        if len(chunk) < params["limit"]:
            break
        time.sleep(0.25)          # stay well inside the public rate limit

    if not rows:
        raise RuntimeError(f"Binance returned no data for {symbol} {interval}")

    arr = np.array([[float(x) for x in (r[0], r[1], r[2], r[3], r[4], r[5], r[9])]
                    for r in rows], dtype=float)
    ts = (arr[:, 0] / 1000.0).astype(np.int64)
    o, h, l, c, v, tb = (arr[:, i] for i in range(1, 7))

    np.savez_compressed(path, ts=ts, o=o, h=h, l=l, c=c, v=v, tb=tb)
    return _to_tape(ts, o, h, l, c, v, tb)


def _to_tape(ts, o, h, l, c, v, tb):
    """Shape it like scid.trades() so the whole stack works unchanged."""
    ts = np.asarray(ts, dtype=np.int64).astype("datetime64[s]").astype("datetime64[us]")
    v = np.asarray(v, dtype=float)
    tb = np.asarray(tb, dtype=float)           # taker BUY base volume
    ask_v = tb                                  # aggressive buying
    bid_v = np.maximum(v - tb, 0.0)             # aggressive selling
    return {
        "ts": ts,
        "price": np.asarray(c, dtype=np.float32),
        "bid": np.full(v.size, np.nan),
        "ask": np.full(v.size, np.nan),
        "volume": v,
        "bid_volume": bid_v,
        "ask_volume": ask_v,
        "delta": ask_v - bid_v,
        "is_trade": np.ones(v.size, dtype=bool),
        "_open": np.asarray(o, dtype=float),
        "_high": np.asarray(h, dtype=float),
        "_low": np.asarray(l, dtype=float),
        "estimated_flow": False,                # exchange-reported, not inferred
    }


def bars_from_klines(symbol="BTCUSDT", interval="5m", bars=1500):
    """Build order flow bars directly from klines, keeping true OHLC.

    orderflow.bars() derives OHLC from the tape, which is right for tick data
    but wrong here: each kline already has its own high and low, and rebuilding
    them from closes would understate every bar's range - and with it every
    stop and target in a backtest.
    """
    t = fetch_klines(symbol, interval, bars)
    v = t["volume"]
    bid_v, ask_v = t["bid_volume"], t["ask_volume"]
    delta = ask_v - bid_v
    total = bid_v + ask_v
    with np.errstate(divide="ignore", invalid="ignore"):
        imb = np.where(total > 0, delta / total, 0.0)

    c = np.asarray(t["price"], dtype=float)
    h, l, o = t["_high"], t["_low"], t["_open"]
    rng = np.maximum(h - l, 1e-9)
    eff = v / rng
    absorption = eff > (np.median(eff) * 3.0) if v.size > 5 else np.zeros(v.size, bool)

    dd = np.zeros(v.size, dtype=np.int8)
    dd[(delta > 0) & (c < o)] = 1
    dd[(delta < 0) & (c > o)] = -1

    typical = (h + l + c) / 3.0
    cum_v = np.cumsum(v)
    vwap = np.where(cum_v > 0, np.cumsum(typical * v) / np.maximum(cum_v, 1e-9), c)

    return {
        "ts": t["ts"], "open": o, "high": h, "low": l, "close": c,
        "volume": v, "bid_volume": bid_v, "ask_volume": ask_v,
        "delta": delta, "cvd": np.cumsum(delta), "imbalance": imb,
        "absorption": absorption, "delta_div": dd, "vwap": vwap,
        "max_delta": delta, "min_delta": delta,
    }


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    iv = sys.argv[2] if len(sys.argv) > 2 else "5m"
    b = bars_from_klines(sym, iv, 1500)
    print(f"  {sym} {iv}: {b['ts'].size} bars")
    print(f"  range     : {b['ts'][0]} .. {b['ts'][-1]}")
    print(f"  price     : {b['close'].min():.2f} .. {b['close'].max():.2f}")
    print(f"  delta     : sum {b['delta'].sum():+.2f}, mean |d| {np.abs(b['delta']).mean():.2f}")
    print(f"  imbalance : mean |i| {np.abs(b['imbalance']).mean():.3f}  (yfinance gave 1.000)")
    print(f"  CVD       : {b['cvd'][-1]:+.2f}")
