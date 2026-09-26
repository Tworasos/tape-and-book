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
from collections import deque

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


class EventDetector:
    """Read the book for the algorithm: walls, pulling, stacking, icebergs, voids.

    Fed one snapshot at a time, and each snapshot is read exactly once. The
    live feed used to re-scan a sliding window of the last three snapshots
    every second instead, which had two effects on the measurement loop:
    every wall was reported up to three times, and icebergs - which need
    memory across more than three snapshots - could never fire at all.

    Walls are STATE: reported on every snapshot they stand in, the same way
    book imbalance is sampled every second. Pulled, stacked and iceberg are
    CHANGES between two snapshots.

    An iceberg is read at the touch: the best bid or ask empties and comes back
    at the same price, again and again. Deeper levels coming and going, or a
    price that is crossed and later re-quoted, are not refills.

    A top-N snapshot only shows a window of the book. A level that leaves that
    window because price moved away has not been pulled - it is just out of
    sight - and a level that price traded through was consumed, not pulled.
    Only a level that vanished while still inside the visible range counts.
    """

    def __init__(self, tick_size, wall_sigma=3.0, iceberg_refills=3, window=3,
                 memory=600):
        self.tick = float(tick_size)
        self.wall_sigma = wall_sigma
        self.iceberg_refills = iceberg_refills
        self.memory = memory               # snapshots a refill count survives
        self._sizes = deque(maxlen=window)  # wall threshold: trailing snapshots only
        self._prev = {}
        self._prev_best = {}               # side -> best level of the last snapshot
        self._depleted = {}                # (side, lv) -> snapshot index it emptied at
        self._refills = {}                 # (side, lv) -> [count, last snapshot index]
        self._n = 0

    def _lv(self, price):
        return int(round(price / self.tick))

    def update(self, snap):
        """Read one snapshot; return the events it produced."""
        self._n += 1
        ts = snap["ts"]
        levels = ((0, snap["bids"]), (1, snap["asks"]))
        self._sizes.append([sz for _, lvls in levels for _, sz in lvls])
        sizes = np.array([x for s in self._sizes for x in s], dtype=float)
        if sizes.size == 0:
            self._prev, self._prev_best = {}, {}
            return []
        # Causal: the threshold never sees a snapshot that has not happened yet.
        thresh = sizes.mean() + self.wall_sigma * sizes.std()

        found = []
        cur = {}
        visible = {}
        best = {}
        for side, lvls in levels:
            idx = [self._lv(p) for p, _ in lvls]
            if idx:
                visible[side] = (min(idx), max(idx))
                best[side] = max(idx) if side == 0 else min(idx)
            for (price, size), lv in zip(lvls, idx):
                cur[(side, lv)] = size
                if size >= thresh:
                    found.append({"ts": ts, "price": price, "side": side,
                                  "kind": "wall", "size": int(size)})

        for key, old in self._prev.items():
            side, lv = key
            new = cur.get(key, 0)
            price = round(lv * self.tick, 10)
            if new == 0:
                rng = visible.get(side)
                if rng is None:
                    continue
                lo, hi = rng
                # bids scroll out below the window, asks above it
                scrolled_out = lv < lo if side == 0 else lv > hi
                if scrolled_out:
                    continue
                # bids above the best bid / asks below the best ask were traded through
                crossed = lv > hi if side == 0 else lv < lo
                if old >= thresh and not crossed:
                    found.append({"ts": ts, "price": price, "side": side,
                                  "kind": "pulled", "size": int(old)})
                # Hit empty at the touch, with the touch moving by at most a
                # tick. A jump of several ticks is the market moving away.
                if lv == self._prev_best.get(side) and abs(best.get(side, lv) - lv) <= 1:
                    self._depleted[key] = self._n
            elif new > old * 1.5 and new >= thresh * 0.5:
                found.append({"ts": ts, "price": price, "side": side,
                              "kind": "stacked", "size": int(new - old)})

        # iceberg: the same level empties and comes back, again and again
        for key, size in cur.items():
            if key in self._prev or key not in self._depleted:
                continue
            del self._depleted[key]
            if key[1] != best.get(key[0]):
                continue                   # came back, but not at the touch
            rec = self._refills.setdefault(key, [0, self._n])
            rec[0] += 1
            rec[1] = self._n
            if rec[0] == self.iceberg_refills:
                side, lv = key
                found.append({"ts": ts, "price": round(lv * self.tick, 10), "side": side,
                              "kind": "iceberg", "size": int(size)})

        # voids: gaps of more than three ticks between consecutive occupied levels
        for side, lvls in levels:
            lv = sorted(self._lv(p) for p, _ in lvls)
            for i in range(1, len(lv)):
                gap = lv[i] - lv[i - 1]
                if gap > 3:
                    found.append({"ts": ts, "price": round(lv[i - 1] * self.tick, 10),
                                  "side": side, "kind": "void", "size": int(gap)})

        self._prev = cur
        self._prev_best = best
        if self._n % 60 == 0:
            self._forget()
        return found

    def _forget(self):
        """Drop refill memory for levels price has long left behind."""
        cutoff = self._n - self.memory
        self._depleted = {k: n for k, n in self._depleted.items() if n > cutoff}
        self._refills = {k: r for k, r in self._refills.items() if r[1] > cutoff}


def detect_events(snaps, tick_size, wall_sigma=3.0, iceberg_refills=3):
    """Batch form of EventDetector over a list of snapshots.

    The wall threshold at each snapshot uses only the snapshots up to it, so a
    backtest run through here has no look-ahead.
    """
    det = EventDetector(tick_size, wall_sigma, iceberg_refills, window=max(len(snaps), 1))
    found = []
    for s in snaps:
        found.extend(det.update(s))
    return found
