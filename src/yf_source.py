"""
Yahoo Finance source — free OHLCV bars, including Nasdaq futures.

This exists so the terminal can show NQ today, before the Databento account is
set up. Be clear about what it is and is not:

    real      price, volume, VWAP, volume profile, session structure
    ESTIMATED delta and CVD - Yahoo gives no bid/ask split, so the aggressor
              side is inferred with the tick rule (uptick = buy, downtick =
              sell, unchanged = previous direction)
    missing   order book, market depth, resting liquidity, real footprint

The tick rule is a standard fallback in the microstructure literature, but it
is an approximation and misclassifies a meaningful share of trades. Everything
derived from it is labelled `estimated` all the way to the UI, so it never gets
mistaken for exchange-reported flow.

Yahoo intraday history limits (their side, not ours):
    1m            ~7 days per request, 30 days total
    2m/5m/15m     ~60 days
    30m/1h        ~730 days
    1d            decades
"""

from __future__ import annotations

import os
import time

import numpy as np

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "yf")
CACHE_TTL = 900  # seconds; intraday bars do not need re-fetching more often

# What we offer in the terminal. Nasdaq first - that is the instrument of record.
SYMBOLS = [
    {"symbol": "NQ=F", "label": "NQ — E-mini Nasdaq-100", "tick": 0.25},
    {"symbol": "MNQ=F", "label": "MNQ — Micro E-mini Nasdaq-100", "tick": 0.25},
    {"symbol": "ES=F", "label": "ES — E-mini S&P 500", "tick": 0.25},
    {"symbol": "MES=F", "label": "MES — Micro E-mini S&P 500", "tick": 0.25},
    {"symbol": "YM=F", "label": "YM — E-mini Dow", "tick": 1.0},
    {"symbol": "RTY=F", "label": "RTY — E-mini Russell 2000", "tick": 0.1},
    {"symbol": "QQQ", "label": "QQQ — ETF Nasdaq-100", "tick": 0.01},
]

PERIOD_FOR = {
    "1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d",
    "30m": "60d", "1h": "730d", "1d": "5y",
}


def _cache_path(symbol, interval):
    safe = symbol.replace("=", "_").replace("^", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{interval}.npz")


def fetch(symbol, interval="5m", force=False):
    """Download bars and cache them. Returns a tape-shaped dict.

    The returned dict matches scid.trades() so the rest of the stack -
    orderflow.bars, footprint, profile, heatmap - works unchanged.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, interval)

    if not force and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        z = np.load(path, allow_pickle=False)
        return _to_tape(z["ts"], z["close"], z["volume"], z["high"], z["low"], z["open"])

    import yfinance as yf

    df = yf.download(
        symbol,
        period=PERIOD_FOR.get(interval, "60d"),
        interval=interval,
        progress=False,
        auto_adjust=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo returned no data for {symbol} ({interval})")

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    ts = df.index.values.astype("datetime64[us]")
    close = df["Close"].to_numpy(dtype=float).ravel()
    high = df["High"].to_numpy(dtype=float).ravel()
    low = df["Low"].to_numpy(dtype=float).ravel()
    opn = df["Open"].to_numpy(dtype=float).ravel()
    vol = np.nan_to_num(df["Volume"].to_numpy(dtype=float).ravel()).astype(np.int64)

    ok = np.isfinite(close)
    ts, close, high, low, opn, vol = ts[ok], close[ok], high[ok], low[ok], opn[ok], vol[ok]

    np.savez_compressed(path, ts=ts.astype(np.int64), close=close,
                        volume=vol, high=high, low=low, open=opn)
    return _to_tape(ts.astype(np.int64), close, vol, high, low, opn)


def _tick_rule(price):
    """Infer aggressor direction: +1 buy, -1 sell, carry forward on no change."""
    d = np.sign(np.diff(price, prepend=price[0]))
    out = np.empty(d.size, dtype=np.int8)
    last = 1
    for i, v in enumerate(d):
        if v != 0:
            last = int(v)
        out[i] = last
    return out


def _to_tape(ts_raw, close, volume, high, low, opn):
    ts = np.asarray(ts_raw).astype("datetime64[us]")
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=np.int64)

    side = _tick_rule(close)
    ask_v = np.where(side > 0, volume, 0).astype(np.int64)   # aggressive buying
    bid_v = np.where(side < 0, volume, 0).astype(np.int64)   # aggressive selling

    return {
        "ts": ts,
        "price": close.astype(np.float32),
        "bid": np.full(close.size, np.nan),
        "ask": np.full(close.size, np.nan),
        "volume": volume,
        "bid_volume": bid_v,
        "ask_volume": ask_v,
        "delta": ask_v - bid_v,
        "is_trade": np.ones(close.size, dtype=bool),
        "_high": np.asarray(high, dtype=float),
        "_low": np.asarray(low, dtype=float),
        "_open": np.asarray(opn, dtype=float),
        "estimated_flow": True,
    }


def summary(symbol, interval="5m"):
    t = fetch(symbol, interval)
    price = t["price"]
    return {
        "symbol": symbol,
        "interval": interval,
        "records": int(price.size),
        "first_ts": str(t["ts"][0]),
        "last_ts": str(t["ts"][-1]),
        "price_min": float(price.min()),
        "price_max": float(price.max()),
        "last": float(price[-1]),
        "total_volume": int(t["volume"].sum()),
        "estimated_flow": True,
    }


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "NQ=F"
    iv = sys.argv[2] if len(sys.argv) > 2 else "5m"
    for k, v in summary(sym, iv).items():
        print(f"{k:>16}: {v}")
