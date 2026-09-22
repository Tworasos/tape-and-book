"""
Observation journal — the memory the bot learns from.

Every second the trader sees a market state and forms observations ("wall on
the bid", "sweep BUY", "book tilted +0.4"). On its own that is worthless for
learning, because nothing records whether the observation was RIGHT.

This module closes that loop:

    1. record()   stores each observation set with the price at that moment
    2. a pending queue holds it until the horizon elapses
    3. resolve()  writes the finished row: what was seen, and what price did
                  next over 30s / 120s / 300s

The result is a plain JSONL file of labelled examples. `learn.py` turns those
into measured hit rates per observation type, and from there into weights that
come from evidence instead of my guesswork.

Deliberately simple format: one JSON object per line, append-only, no database.
It has to survive a hard kill of the server without corrupting anything.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "journal")

# Horizons, in seconds, at which we score the observation. Short ones match the
# bot's holding time; the long one shows whether a read had lasting value.
HORIZONS = (30, 120, 300)


class Journal:
    def __init__(self, symbol, horizons=HORIZONS, flush_every=20):
        self.symbol = symbol.lower()
        self.horizons = tuple(sorted(horizons))
        self.max_h = self.horizons[-1]
        self.pending = deque()
        self.buffer = []
        self.flush_every = flush_every
        self.lock = threading.Lock()
        self.written = 0
        os.makedirs(DATA_DIR, exist_ok=True)

    @property
    def path(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return os.path.join(DATA_DIR, f"{self.symbol}_{day}.jsonl")

    def record(self, price, observations, score, action):
        """Queue a snapshot to be scored once the horizons elapse."""
        if not price or not observations:
            return
        with self.lock:
            self.pending.append({
                "ts": time.time(),
                "price": float(price),
                "score": round(float(score), 3),
                "action": int(action),
                "obs": [{"n": o["name"], "s": int(o["side"]),
                         "w": round(float(o["weight"]), 3), "src": o["source"]}
                        for o in observations],
                "fwd": {},
            })

    def resolve(self, price):
        """Fill in forward returns for rows whose horizons have passed."""
        if not price:
            return
        now = time.time()
        done = []
        with self.lock:
            for row in self.pending:
                age = now - row["ts"]
                for h in self.horizons:
                    key = str(h)
                    if key not in row["fwd"] and age >= h:
                        # basis points of move from the observation price
                        row["fwd"][key] = round(
                            (price - row["price"]) / row["price"] * 10_000.0, 2)
            while self.pending and now - self.pending[0]["ts"] >= self.max_h:
                done.append(self.pending.popleft())
            if done:
                self.buffer.extend(done)
            should_flush = len(self.buffer) >= self.flush_every
        if should_flush:
            self.flush()

    def flush(self):
        with self.lock:
            rows, self.buffer = self.buffer, []
        if not rows:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.written += len(rows)
        except OSError:
            # Never let disk trouble kill the trading loop; drop the batch.
            pass

    def close(self):
        """Flush on shutdown, keeping rows whose horizons never completed.

        A partial row is still useful: the 30s label may be filled even when
        300s is not, and learn.py simply skips the missing horizons.
        """
        with self.lock:
            leftover = [r for r in self.pending if r["fwd"]]
            self.pending.clear()
            self.buffer.extend(leftover)
        self.flush()

    def stats(self):
        with self.lock:
            return {
                "symbol": self.symbol.upper(),
                "pending": len(self.pending),
                "buffered": len(self.buffer),
                "written": self.written,
                "file": self.path,
                "horizons": list(self.horizons),
            }


def load(symbol=None, days=7, limit=200_000):
    """Read journal rows back. Returns a list of dicts, newest last."""
    if not os.path.isdir(DATA_DIR):
        return []
    names = sorted(os.listdir(DATA_DIR))
    if symbol:
        names = [n for n in names if n.startswith(symbol.lower() + "_")]
    names = names[-days:]
    rows = []
    for name in names:
        try:
            with open(os.path.join(DATA_DIR, name), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue      # tolerate a torn last line after a kill
        except OSError:
            continue
    return rows[-limit:]


_JOURNALS = {}
_LOCK = threading.Lock()


def get_journal(symbol):
    symbol = symbol.lower()
    with _LOCK:
        j = _JOURNALS.get(symbol)
        if j is None:
            j = Journal(symbol)
            _JOURNALS[symbol] = j
        return j
