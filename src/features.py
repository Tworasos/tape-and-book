"""
Order flow features computable from Sierra Chart .scid records.

These are the L3 features that do NOT need a full order book: they come from
the trade tape alone, where Sierra records exchange-provided BidVolume and
AskVolume per record. Depth-based features (OFI, microprice, book imbalance)
need MBP-10 / .depth data and live in a separate module.

Convention used throughout, matching the footprint literature:
    ask_volume = volume that traded at the ask  = aggressive BUYING
    bid_volume = volume that traded at the bid  = aggressive SELLING
    delta      = ask_volume - bid_volume
"""

from __future__ import annotations

import numpy as np

import scid


def session_bars(path, freq="1min"):
    """Aggregate tick records into time bars carrying order flow columns.

    Returns a dict of arrays, one entry per bar:
        ts, price_last, volume, delta, cvd, imbalance, trades
    """
    t = scid.trades(path)
    ts = t["ts"]
    if ts.size == 0:
        return {}

    unit = np.timedelta64(1, "m") if freq == "1min" else np.timedelta64(1, "s")
    bucket = (ts - ts[0]) // unit
    edges = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [ts.size]))

    delta = np.add.reduceat(t["delta"], starts)
    volume = np.add.reduceat(t["volume"], starts)
    bid_v = np.add.reduceat(t["bid_volume"], starts)
    ask_v = np.add.reduceat(t["ask_volume"], starts)

    total = bid_v + ask_v
    with np.errstate(divide="ignore", invalid="ignore"):
        imbalance = np.where(total > 0, (ask_v - bid_v) / total, 0.0)

    return {
        "ts": ts[starts],
        "price_last": t["price"][ends - 1],
        "volume": volume,
        "delta": delta,
        "cvd": np.cumsum(delta),
        "imbalance": imbalance,
        "trades": ends - starts,
    }


def divergence(bars, window=20):
    """Flag bars where price and CVD disagree over `window` bars.

    Price up while CVD falls means aggressive buyers are being absorbed by a
    passive seller: a classic exhaustion signal. Returns +1 (bearish
    divergence), -1 (bullish divergence), 0 (agreement).
    """
    price = bars["price_last"]
    cvd = bars["cvd"]
    if price.size <= window:
        return np.zeros(price.size, dtype=np.int8)

    dp = np.zeros(price.size)
    dc = np.zeros(price.size)
    dp[window:] = price[window:] - price[:-window]
    dc[window:] = cvd[window:] - cvd[:-window]

    out = np.zeros(price.size, dtype=np.int8)
    out[(dp > 0) & (dc < 0)] = 1
    out[(dp < 0) & (dc > 0)] = -1
    return out


def describe(path, freq="1min"):
    bars = session_bars(path, freq)
    if not bars:
        return "empty file"
    div = divergence(bars)
    delta = bars["delta"]
    lines = [
        f"bars                : {bars['ts'].size} ({freq})",
        f"span                : {bars['ts'][0]} .. {bars['ts'][-1]}",
        f"volume              : {int(bars['volume'].sum()):,}",
        f"CVD close           : {int(bars['cvd'][-1]):,}",
        f"CVD min / max       : {int(bars['cvd'].min()):,} / {int(bars['cvd'].max()):,}",
        f"delta per bar (std) : {delta.std():.1f}",
        f"|delta| p95         : {np.percentile(np.abs(delta), 95):.0f}",
        f"imbalance |mean|    : {np.abs(bars['imbalance']).mean():.3f}",
        f"bearish divergences : {int((div == 1).sum())}",
        f"bullish divergences : {int((div == -1).sum())}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        print(f"=== {arg}")
        print(describe(arg))
        print()
