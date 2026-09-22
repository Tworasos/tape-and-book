"""
Order flow mechanics computed from Sierra Chart tick records.

Everything here works on the tape alone (price + exchange-reported bid/ask
volume). Book-based features (OFI, microprice) need MBP-10 and arrive with the
Databento layer.

Implemented:
    footprint          bid x ask volume per price level per bar
    diagonal imbalance the classic footprint signal (ask[p] vs bid[p-1])
    volume profile     POC, value area high/low
    vwap               session volume weighted average price
    absorption         heavy volume that fails to move price
    delta divergence   bar closes against its own delta
"""

from __future__ import annotations

import numpy as np

import scid

TF_SECONDS = {
    "1s": 1, "5s": 5, "15s": 15, "30s": 30,
    "1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def detect_tick_size(price):
    """Infer the instrument tick size from observed price differences."""
    uniq = np.unique(price[np.isfinite(price)])
    if uniq.size < 3:
        return 0.01
    d = np.diff(uniq)
    d = d[d > 1e-9]
    if d.size == 0:
        return 0.01
    tick = float(np.min(d))
    # guard against float32 noise producing absurdly small ticks
    for candidate in (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, 10.0):
        if abs(tick - candidate) / candidate < 0.02:
            return candidate
    return max(tick, 1e-6)


def load(path, start_ts=None, end_ts=None, max_records=4_000_000):
    """Load tape records, optionally windowed by timestamp, newest-limited."""
    t = scid.trades(path)
    ts = t["ts"]
    if ts.size == 0:
        return t
    mask = np.ones(ts.size, dtype=bool)
    if start_ts is not None:
        mask &= ts >= np.datetime64(start_ts)
    if end_ts is not None:
        mask &= ts <= np.datetime64(end_ts)
    idx = np.flatnonzero(mask)
    if idx.size > max_records:
        idx = idx[-max_records:]
    return {k: v[idx] for k, v in t.items()}


def _buckets(ts, tf):
    step = np.timedelta64(TF_SECONDS.get(tf, 60), "s")
    anchor = ts[0].astype("datetime64[D]").astype("datetime64[us]")
    b = (ts - anchor) // step
    edges = np.flatnonzero(np.diff(b)) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [ts.size]))
    bar_ts = anchor + b[starts] * step
    return starts, ends, bar_ts


def bars(path_or_tape, tf="1m", start_ts=None, end_ts=None):
    """OHLCV bars enriched with order flow columns."""
    t = load(path_or_tape, start_ts, end_ts) if isinstance(path_or_tape, str) else path_or_tape
    ts, price = t["ts"], t["price"]
    if ts.size == 0:
        return {}

    starts, ends, bar_ts = _buckets(ts, tf)
    n = starts.size

    o = price[starts]
    c = price[ends - 1]
    h = np.array([price[a:b].max() for a, b in zip(starts, ends)])
    lo = np.array([price[a:b].min() for a, b in zip(starts, ends)])

    vol = np.add.reduceat(t["volume"], starts)
    bid_v = np.add.reduceat(t["bid_volume"], starts)
    ask_v = np.add.reduceat(t["ask_volume"], starts)
    delta = ask_v - bid_v
    cvd = np.cumsum(delta)

    # delta extremes within the bar - shows whether the bar was fought over
    run = np.cumsum(t["delta"])
    pad = np.concatenate(([0], run))
    max_d = np.array([run[a:b].max() - (pad[a] if a else 0) for a, b in zip(starts, ends)])
    min_d = np.array([run[a:b].min() - (pad[a] if a else 0) for a, b in zip(starts, ends)])

    total = bid_v + ask_v
    with np.errstate(divide="ignore", invalid="ignore"):
        imb = np.where(total > 0, delta / total, 0.0)

    rng = np.maximum(h - lo, 1e-9)
    # absorption: lots of volume, little range, relative to the window median
    eff = vol / rng
    absorption = eff > (np.median(eff) * 3.0) if n > 5 else np.zeros(n, bool)

    # delta divergence: bar closes down on positive delta, or up on negative
    dd = np.zeros(n, dtype=np.int8)
    dd[(delta > 0) & (c < o)] = 1
    dd[(delta < 0) & (c > o)] = -1

    typical = (h + lo + c) / 3.0
    cum_v = np.cumsum(vol)
    vwap = np.where(cum_v > 0, np.cumsum(typical * vol) / np.maximum(cum_v, 1), c)

    return {
        "ts": bar_ts,
        "open": o, "high": h, "low": lo, "close": c,
        "volume": vol,
        "bid_volume": bid_v, "ask_volume": ask_v,
        "delta": delta, "cvd": cvd,
        "max_delta": max_d, "min_delta": min_d,
        "imbalance": imb,
        "absorption": absorption,
        "delta_div": dd,
        "vwap": vwap,
        "_starts": starts, "_ends": ends,
    }


def footprint(path_or_tape, tf="5m", start_ts=None, end_ts=None,
              tick_size=None, max_bars=120, imbalance_ratio=3.0):
    """Per-bar, per-price-level bid/ask volume plus diagonal imbalance flags.

    Diagonal imbalance is the standard footprint reading: buying at price p is
    compared against selling one tick below, because those are the two sides
    that actually met each other.
    """
    t = load(path_or_tape, start_ts, end_ts) if isinstance(path_or_tape, str) else path_or_tape
    if t["ts"].size == 0:
        return {"bars": [], "tick_size": 0.01}

    tick = tick_size or detect_tick_size(t["price"])
    starts, ends, bar_ts = _buckets(t["ts"], tf)
    if starts.size > max_bars:
        starts, ends, bar_ts = starts[-max_bars:], ends[-max_bars:], bar_ts[-max_bars:]

    price, bid_v, ask_v = t["price"], t["bid_volume"], t["ask_volume"]
    out = []
    for a, b, bts in zip(starts, ends, bar_ts):
        p = price[a:b]
        lvl = np.round(p / tick).astype(np.int64)
        uniq, inv = np.unique(lvl, return_inverse=True)
        bv = np.bincount(inv, weights=bid_v[a:b], minlength=uniq.size)
        av = np.bincount(inv, weights=ask_v[a:b], minlength=uniq.size)

        # diagonal imbalance, ascending price order
        buy_imb = np.zeros(uniq.size, bool)
        sell_imb = np.zeros(uniq.size, bool)
        for i in range(uniq.size):
            below = i - 1
            if below >= 0 and uniq[i] - uniq[below] == 1:
                if av[i] > bv[below] * imbalance_ratio and bv[below] > 0:
                    buy_imb[i] = True
                if bv[below] > av[i] * imbalance_ratio and av[i] > 0:
                    sell_imb[below] = True

        tot = bv + av
        poc_i = int(np.argmax(tot)) if tot.size else 0
        out.append({
            "ts": int(bts.astype("datetime64[s]").astype(np.int64)),
            "levels": [
                {
                    "price": round(float(u * tick), 6),
                    "bid": int(bv[i]), "ask": int(av[i]),
                    "delta": int(av[i] - bv[i]),
                    "buy_imb": bool(buy_imb[i]), "sell_imb": bool(sell_imb[i]),
                    "poc": i == poc_i,
                }
                for i, u in enumerate(uniq)
            ],
            "delta": int(av.sum() - bv.sum()),
            "volume": int(tot.sum()),
        })
    return {"bars": out, "tick_size": tick}


def volume_profile(path_or_tape, start_ts=None, end_ts=None,
                   tick_size=None, value_area=0.70, max_levels=400):
    """Volume at price, point of control and value area boundaries."""
    t = load(path_or_tape, start_ts, end_ts) if isinstance(path_or_tape, str) else path_or_tape
    price = t["price"]
    if price.size == 0:
        return {"levels": [], "poc": None, "vah": None, "val": None}

    tick = tick_size or detect_tick_size(price)
    span = (np.nanmax(price) - np.nanmin(price)) / tick
    if span > max_levels:                      # coarsen so the profile stays readable
        tick *= np.ceil(span / max_levels)

    lvl = np.round(price / tick).astype(np.int64)
    uniq, inv = np.unique(lvl, return_inverse=True)
    bv = np.bincount(inv, weights=t["bid_volume"], minlength=uniq.size)
    av = np.bincount(inv, weights=t["ask_volume"], minlength=uniq.size)
    tot = bv + av

    poc_i = int(np.argmax(tot))
    target = tot.sum() * value_area
    lo = hi = poc_i
    acc = tot[poc_i]
    while acc < target and (lo > 0 or hi < uniq.size - 1):
        take_low = tot[lo - 1] if lo > 0 else -1
        take_high = tot[hi + 1] if hi < uniq.size - 1 else -1
        if take_high >= take_low:
            hi += 1
            acc += tot[hi]
        else:
            lo -= 1
            acc += tot[lo]

    return {
        "levels": [
            {"price": round(float(u * tick), 6), "bid": int(bv[i]),
             "ask": int(av[i]), "total": int(tot[i])}
            for i, u in enumerate(uniq)
        ],
        "poc": round(float(uniq[poc_i] * tick), 6),
        "vah": round(float(uniq[hi] * tick), 6),
        "val": round(float(uniq[lo] * tick), 6),
        "tick_size": tick,
    }


def cvd_divergence(b, window=14):
    """Price/CVD disagreement over a rolling window. +1 bearish, -1 bullish."""
    c, cvd = b["close"], b["cvd"]
    out = np.zeros(c.size, dtype=np.int8)
    if c.size <= window:
        return out
    dp = c[window:] - c[:-window]
    dc = cvd[window:] - cvd[:-window]
    out[window:][(dp > 0) & (dc < 0)] = 1
    out[window:][(dp < 0) & (dc > 0)] = -1
    return out


def heatmap(path_or_tape, tf="1m", start_ts=None, end_ts=None,
            tick_size=None, max_cols=400, max_rows=220):
    """Time x price grid of TRADED volume, split by aggressor side.

    This is the tape-based cousin of a Bookmap heatmap. It shows where trades
    happened, not where liquidity rested - resting liquidity needs depth data
    and lives in book.py. Returns column-major cells so the UI can draw it
    directly onto a canvas.
    """
    t = load(path_or_tape, start_ts, end_ts) if isinstance(path_or_tape, str) else path_or_tape
    ts, price = t["ts"], t["price"]
    if ts.size == 0:
        return {"cells": [], "rows": [], "cols": []}

    tick = tick_size or detect_tick_size(price)
    lo, hi = float(np.nanmin(price)), float(np.nanmax(price))
    span = max((hi - lo) / tick, 1.0)
    if span > max_rows:
        tick *= np.ceil(span / max_rows)

    starts, ends, bar_ts = _buckets(ts, tf)
    if starts.size > max_cols:
        starts, ends, bar_ts = starts[-max_cols:], ends[-max_cols:], bar_ts[-max_cols:]

    lvl = np.round(price / tick).astype(np.int64)
    base = int(np.round(lo / tick))
    top = int(np.round(hi / tick))
    rows = np.arange(base, top + 1)
    row_index = {int(v): i for i, v in enumerate(rows)}

    bid_v, ask_v = t["bid_volume"], t["ask_volume"]
    cells = []
    for col, (a, b) in enumerate(zip(starts, ends)):
        sl = lvl[a:b]
        if sl.size == 0:
            continue
        uniq, inv = np.unique(sl, return_inverse=True)
        bv = np.bincount(inv, weights=bid_v[a:b], minlength=uniq.size)
        av = np.bincount(inv, weights=ask_v[a:b], minlength=uniq.size)
        for i, u in enumerate(uniq):
            r = row_index.get(int(u))
            if r is None:
                continue
            total = int(bv[i] + av[i])
            if total <= 0:
                continue
            cells.append([col, r, total, int(av[i] - bv[i])])

    return {
        "cells": cells,
        "rows": [round(float(r * tick), 6) for r in rows],
        "cols": [int(x.astype("datetime64[s]").astype(np.int64)) for x in bar_ts],
        "tick_size": tick,
        "kind": "trades",
    }


def large_trades(path_or_tape, start_ts=None, end_ts=None,
                 percentile=99.0, min_multiple=5.0, top=500):
    """Detect unusually large aggressive prints on the tape.

    Two filters must both pass, so the result survives regime changes in
    overall activity:
      - volume above the given percentile of the window
      - volume at least `min_multiple` times the median print

    Returns newest-first, capped at `top`.
    """
    t = load(path_or_tape, start_ts, end_ts) if isinstance(path_or_tape, str) else path_or_tape
    vol = t["volume"]
    if vol.size == 0:
        return {"trades": [], "threshold": 0}

    pos = vol[vol > 0]
    med = float(np.median(pos)) if pos.size else 1.0
    # The percentile is the real filter; the median multiple only guards against
    # flat distributions where p99 sits right on top of the median. Taking the
    # max of both was too strict and returned nothing on low-variance files.
    p_hi = float(np.percentile(vol, percentile))
    thresh = max(p_hi, med * 1.5, 1.0)
    idx = np.flatnonzero(vol >= thresh)
    if idx.size == 0 and pos.size:          # fall back to the top 1% by rank
        k = max(1, int(pos.size * 0.01))
        idx = np.argsort(vol)[-k:]
        idx = np.sort(idx)
        thresh = float(vol[idx].min())
    if idx.size > top:
        idx = idx[-top:]

    ask_v, bid_v = t["ask_volume"], t["bid_volume"]
    out = []
    for i in idx[::-1]:
        d = int(ask_v[i] - bid_v[i])
        out.append({
            "ts": int(t["ts"][i].astype("datetime64[s]").astype(np.int64)),
            "price": float(t["price"][i]),
            "volume": int(vol[i]),
            "delta": d,
            "side": "BUY" if d > 0 else ("SELL" if d < 0 else "MIXED"),
            "multiple": round(float(vol[i]) / med, 1) if med else 0.0,
        })
    return {"trades": out, "threshold": int(thresh), "median": med}


def sweeps(path_or_tape, start_ts=None, end_ts=None, window_ms=None,
           min_levels=3, min_multiple=3.0, top=300):
    """Detect sweeps: one aggressor clearing several price levels fast.

    A sweep is a far stronger signal than a single large print, because it
    shows someone willing to pay through the book instead of waiting in it.

    The window adapts to the data: on tick data 250 ms is right, but Sierra
    often stores one aggregated record per second, where a fixed 250 ms window
    can never span more than a single record. When `window_ms` is None the
    window is derived from the median gap between records.
    """
    t = load(path_or_tape, start_ts, end_ts) if isinstance(path_or_tape, str) else path_or_tape
    ts, price, vol = t["ts"], t["price"], t["volume"]
    n = ts.size
    if n < 3:
        return {"sweeps": [], "window_ms": 0}

    gaps_ms = np.diff(ts).astype("timedelta64[ms]").astype(np.int64)
    median_gap = float(np.median(gaps_ms)) if gaps_ms.size else 0.0
    if window_ms is None:
        window_ms = 250 if median_gap < 100 else int(max(median_gap * 5, 1000))

    tick = detect_tick_size(price)
    med = float(np.median(vol[vol > 0])) if np.any(vol > 0) else 1.0

    # Vectorised window ends, then filter candidates before any Python loop.
    win = np.timedelta64(int(window_ms), "ms")
    ends = np.searchsorted(ts, ts + win, side="right")

    cv = np.concatenate(([0], np.cumsum(vol)))
    cd = np.concatenate(([0], np.cumsum(t["delta"])))
    idx = np.arange(n)
    seg_vol = cv[ends] - cv[idx]
    seg_delta = cd[ends] - cd[idx]
    span = ends - idx

    cand = np.flatnonzero(
        (span >= min_levels)
        & (np.abs(seg_delta) > seg_vol * 0.6)
        & (seg_vol >= med * min_multiple * min_levels)
    )

    out = []
    last_end = -1
    for i in cand:
        if i < last_end:                      # do not report overlapping sweeps
            continue
        j = int(ends[i])
        lv = np.unique(np.round(price[i:j] / tick).astype(np.int64))
        if lv.size < min_levels:
            continue
        out.append({
            "ts": int(ts[i].astype("datetime64[s]").astype(np.int64)),
            "price_from": float(price[i]),
            "price_to": float(price[j - 1]),
            "levels": int(lv.size),
            "volume": int(seg_vol[i]),
            "delta": int(seg_delta[i]),
            "side": "BUY" if seg_delta[i] > 0 else "SELL",
            "ticks": int(lv.max() - lv.min()),
        })
        last_end = j

    return {"sweeps": out[-top:][::-1], "tick_size": tick,
            "window_ms": int(window_ms), "median_gap_ms": median_gap}
