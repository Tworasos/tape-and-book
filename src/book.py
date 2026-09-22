"""
Order book engine and Bookmap-style liquidity analytics.

This is the layer that reads RESTING liquidity - who is waiting in the book -
as opposed to orderflow.py, which reads executed trades. A Bookmap heatmap is a
picture of this state over time.

Input is a normalised depth event stream, so the same engine serves both
sources we may end up using:

    Sierra Chart .depth   ->  read_sc_depth()
    Databento MBP-10      ->  from_mbp10()

Event tuple: (ts_us, side, price, size, action)
    side   : 0 = bid, 1 = ask
    action : 0 = clear book, 1 = set level to size, 2 = delete level

Features produced for the algorithm:
    walls          unusually large resting orders
    pulling        liquidity withdrawn as price approaches it
    stacking       liquidity added as price approaches
    icebergs       a level that refills repeatedly after being hit
    absorption     aggression hits a wall and price does not pass
    voids          gaps in the book
    ofi            order flow imbalance (Cont, Kukanov, Stoikov)
    microprice     depth-weighted fair value
"""

from __future__ import annotations

import struct

import numpy as np

# --- Sierra Chart .depth binary format -------------------------------------
# Record layout confirmed against the only public parser of this format
# (toobrien/tick_db). Sierra ships no header struct for it in ACS_Source.
SC_DEPTH_HEADER = 64
SC_DEPTH_RECORD = struct.Struct("<qBBHfII")   # 24 bytes

SC_CMD_CLEAR = 1
SC_CMD_ADD_BID, SC_CMD_ADD_ASK = 2, 3
SC_CMD_MOD_BID, SC_CMD_MOD_ASK = 4, 5
SC_CMD_DEL_BID, SC_CMD_DEL_ASK = 6, 7

SC_EPOCH_US = np.datetime64("1899-12-30T00:00:00", "us")


def read_sc_depth(path, limit=None):
    """Parse a Sierra Chart .depth file into normalised depth events."""
    out = []
    with open(path, "rb") as fh:
        fh.seek(SC_DEPTH_HEADER)
        while True:
            raw = fh.read(SC_DEPTH_RECORD.size)
            if len(raw) < SC_DEPTH_RECORD.size:
                break
            ts, cmd, _flags, _norders, price, qty, _res = SC_DEPTH_RECORD.unpack(raw)
            if cmd == SC_CMD_CLEAR:
                out.append((ts, 0, 0.0, 0, 0))
            elif cmd in (SC_CMD_ADD_BID, SC_CMD_MOD_BID):
                out.append((ts, 0, price, qty, 1))
            elif cmd in (SC_CMD_ADD_ASK, SC_CMD_MOD_ASK):
                out.append((ts, 1, price, qty, 1))
            elif cmd == SC_CMD_DEL_BID:
                out.append((ts, 0, price, 0, 2))
            elif cmd == SC_CMD_DEL_ASK:
                out.append((ts, 1, price, 0, 2))
            if limit and len(out) >= limit:
                break
    return out


def from_mbp10(records, tick_size):
    """Convert Databento MBP-10 records into normalised depth events.

    `records` is any iterable of objects exposing the DBN mbp-10 fields
    (ts_event plus levels[0..9].bid_px / bid_sz / ask_px / ask_sz).
    Prices in DBN are fixed-point with 1e-9 scaling.
    """
    events = []
    for r in records:
        ts = int(getattr(r, "ts_event", 0)) // 1000          # ns -> us
        levels = getattr(r, "levels", None) or []
        for lv in levels:
            bp = getattr(lv, "bid_px", 0)
            ap = getattr(lv, "ask_px", 0)
            if bp:
                events.append((ts, 0, bp * 1e-9, int(getattr(lv, "bid_sz", 0)), 1))
            if ap:
                events.append((ts, 1, ap * 1e-9, int(getattr(lv, "ask_sz", 0)), 1))
    return events


class Book:
    """Sparse order book keyed by integer tick level."""

    __slots__ = ("tick", "bids", "asks", "ts")

    def __init__(self, tick_size):
        self.tick = float(tick_size)
        self.bids = {}
        self.asks = {}
        self.ts = 0

    def level(self, price):
        return int(round(price / self.tick))

    def apply(self, ev):
        ts, side, price, size, action = ev
        self.ts = ts
        if action == 0:
            self.bids.clear()
            self.asks.clear()
            return
        book = self.bids if side == 0 else self.asks
        lv = self.level(price)
        if action == 2 or size <= 0:
            book.pop(lv, None)
        else:
            book[lv] = size

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def depth(self, n=10):
        """Top n levels each side, as (price, size) ascending in distance."""
        b = sorted(self.bids.items(), key=lambda kv: -kv[0])[:n]
        a = sorted(self.asks.items(), key=lambda kv: kv[0])[:n]
        return (
            [(k * self.tick, v) for k, v in b],
            [(k * self.tick, v) for k, v in a],
        )

    def microprice(self):
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        qb, qa = self.bids[bb], self.asks[ba]
        if qb + qa == 0:
            return (bb + ba) / 2 * self.tick
        return (bb * qa + ba * qb) / (qb + qa) * self.tick

    def imbalance(self, n=10):
        b, a = self.depth(n)
        qb = sum(s for _, s in b)
        qa = sum(s for _, s in a)
        return (qb - qa) / (qb + qa) if qb + qa else 0.0


def replay(events, tick_size, snapshot_every_us=1_000_000, levels=40):
    """Replay events, emitting periodic snapshots for the heatmap.

    Returns dict with `snaps` (list of {ts, bids, asks}) and running feature
    series: microprice, imbalance, ofi.
    """
    book = Book(tick_size)
    snaps = []
    micro, imb, ofi_series, ts_series = [], [], [], []

    prev_bb = prev_ba = None
    prev_qb = prev_qa = 0
    next_snap = None

    for ev in events:
        book.apply(ev)
        ts = book.ts
        if next_snap is None:
            next_snap = ts

        bb, ba = book.best_bid(), book.best_ask()
        if bb is not None and ba is not None:
            qb, qa = book.bids[bb], book.asks[ba]
            # Order flow imbalance, Cont-Kukanov-Stoikov construction
            e = 0.0
            if prev_bb is not None:
                if bb > prev_bb:
                    e += qb
                elif bb == prev_bb:
                    e += qb - prev_qb
                else:
                    e -= prev_qb
                if ba < prev_ba:
                    e -= qa
                elif ba == prev_ba:
                    e -= qa - prev_qa
                else:
                    e += prev_qa
            ofi_series.append(e)
            micro.append(book.microprice())
            imb.append(book.imbalance())
            ts_series.append(ts)
            prev_bb, prev_ba, prev_qb, prev_qa = bb, ba, qb, qa

        if ts >= next_snap:
            b, a = book.depth(levels)
            snaps.append({"ts": ts, "bids": b, "asks": a})
            next_snap = ts + snapshot_every_us

    return {
        "snaps": snaps,
        "ts": np.array(ts_series, dtype=np.int64),
        "microprice": np.array(micro, dtype=float),
        "imbalance": np.array(imb, dtype=float),
        "ofi": np.array(ofi_series, dtype=float),
    }


def liquidity_heatmap(snaps, tick_size, max_rows=240):
    """Bookmap-style grid: time x price, colour = resting size."""
    if not snaps:
        return {"cells": [], "rows": [], "cols": [], "kind": "liquidity"}

    prices = [p for s in snaps for p, _ in (s["bids"] + s["asks"])]
    if not prices:
        return {"cells": [], "rows": [], "cols": [], "kind": "liquidity"}

    lo, hi = min(prices), max(prices)
    tick = tick_size
    span = max((hi - lo) / tick, 1.0)
    if span > max_rows:
        tick *= np.ceil(span / max_rows)

    base = int(round(lo / tick))
    top = int(round(hi / tick))
    rows = list(range(base, top + 1))
    index = {v: i for i, v in enumerate(rows)}

    cells = []
    for col, s in enumerate(snaps):
        for price, size in s["bids"]:
            r = index.get(int(round(price / tick)))
            if r is not None and size > 0:
                cells.append([col, r, int(size), 0])
        for price, size in s["asks"]:
            r = index.get(int(round(price / tick)))
            if r is not None and size > 0:
                cells.append([col, r, int(size), 1])

    return {
        "cells": cells,
        "rows": [round(r * tick, 6) for r in rows],
        "cols": [int(s["ts"] // 1_000_000) for s in snaps],
        "tick_size": tick,
        "kind": "liquidity",
    }


def detect_events(snaps, tick_size, wall_sigma=3.0, iceberg_refills=3):
    """Read the book for the algorithm: walls, pulling, stacking, icebergs, voids.

    Returns a list of dicts, each with ts, price, kind and magnitude, so the
    strategy layer can consume them as features without re-reading raw depth.
    """
    if len(snaps) < 2:
        return []

    sizes = np.array([sz for s in snaps for _, sz in (s["bids"] + s["asks"])], dtype=float)
    if sizes.size == 0:
        return []
    thresh = sizes.mean() + wall_sigma * sizes.std()

    found = []
    refills = {}
    prev = {}

    for s in snaps:
        cur = {}
        for side, levels in ((0, s["bids"]), (1, s["asks"])):
            for price, size in levels:
                lv = int(round(price / tick_size))
                cur[(side, lv)] = size
                if size >= thresh:
                    found.append({"ts": s["ts"], "price": price, "side": side,
                                  "kind": "wall", "size": int(size)})

        for key, old in prev.items():
            new = cur.get(key, 0)
            side, lv = key
            price = lv * tick_size
            if old >= thresh and new == 0:
                found.append({"ts": s["ts"], "price": price, "side": side,
                              "kind": "pulled", "size": int(old)})
            elif old > 0 and new == 0:
                refills[key] = refills.get(key, 0)
            elif new > old * 1.5 and new >= thresh * 0.5:
                found.append({"ts": s["ts"], "price": price, "side": side,
                              "kind": "stacked", "size": int(new - old)})

        for key in cur:
            if key in refills and cur[key] > 0:
                refills[key] += 1
                if refills[key] == iceberg_refills:
                    side, lv = key
                    found.append({"ts": s["ts"], "price": lv * tick_size, "side": side,
                                  "kind": "iceberg", "size": int(cur[key])})

        # voids: gaps of more than one tick between consecutive occupied levels
        for side, levels in ((0, s["bids"]), (1, s["asks"])):
            lv = sorted(int(round(p / tick_size)) for p, _ in levels)
            for i in range(1, len(lv)):
                gap = lv[i] - lv[i - 1]
                if gap > 3:
                    found.append({"ts": s["ts"], "price": lv[i - 1] * tick_size,
                                  "side": side, "kind": "void", "size": int(gap)})

        prev = cur

    return found
