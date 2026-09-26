"""
Market reader and decision layer.

This is deliberately NOT a fixed strategy. It does two separate jobs:

    read()    build a full picture of what is happening right now - the book,
              the tape, the session context, resting liquidity, recent large
              prints - as a list of named observations
    decide()  weigh those observations and return a decision together with the
              reasoning that produced it

Why a weighted reader instead of one rule: a single condition ("imbalance >
0.35") throws away everything else the market is showing. A weighted reader
lets contradictory evidence cancel out, lets weak evidence accumulate, and -
crucially - can explain itself afterwards, which a black box cannot.

Every observation carries:
    name      what was seen
    side      +1 bullish, -1 bearish, 0 informational
    weight    how much it should count (0..1)
    detail    human-readable, shown in the terminal
    source    'book' (needs depth data) or 'tape' (works today)

Observations sourced from 'book' stay dormant until depth data is connected,
and the reader says so explicitly rather than silently scoring zero.
"""

from __future__ import annotations

import numpy as np

# Weights are a starting point, not a result. They should be fitted once there
# is real depth data and a validation harness - see gate 2 in the plan.
WEIGHTS = {
    "ofi": 1.00,             # order flow imbalance - strongest documented signal
    "book_imbalance": 0.70,  # resting depth lopsided
    "wall_ahead": 0.85,      # large resting order blocking the path
    "wall_pulled": 0.75,     # that block just vanished
    "absorption": 0.80,      # aggression hitting a level that will not move
    "iceberg": 0.65,         # someone working a big order quietly
    "sweep": 0.70,           # aggressor paying through several levels
    "large_print": 0.45,     # single oversized trade
    "tape_imbalance": 0.40,  # aggressors lopsided over the recent prints
    "cvd_divergence": 0.60,  # price and cumulative delta disagree
    "delta_divergence": 0.40,
    "vwap_side": 0.30,       # context, not a trigger
    "poc_distance": 0.25,
    "session_phase": 0.20,
}

BOOK_SOURCED = {"book_imbalance", "wall_ahead", "wall_pulled", "iceberg", "ofi"}

# Features whose edge has been MEASURED out-of-sample, not assumed.
# Measured on 200k live observations (BTCUSDT, 30s horizon):
#   book_imbalance  52.9% lower bound, 53.9% out-of-sample, rising
#                   monotonically with strength to 57.6% when |imb| > 0.8
#   everything else 49-51%, i.e. indistinguishable from a coin flip
# Caveat: those runs counted a flat outcome as a miss, and the event features
# (walls, pulls, sweeps, large prints) were duplicated and stale in the journal
# at the time. Book imbalance was read fresh each second and is the least
# affected; the rest need re-measuring on journal rows with "v": 2.
# Update this set from the /learn report as evidence accumulates.
CONFIRMED = {"book_imbalance"}


class Observation(dict):
    def __init__(self, name, side, weight, detail, source="tape"):
        super().__init__(name=name, side=int(side), weight=float(weight),
                         detail=detail, source=source,
                         score=round(float(side) * float(weight), 3))


def read(bars, i, ctx=None):
    """Build the list of observations visible at bar `i`.

    `ctx` optionally carries book state and event feeds:
        ctx["book"]      dict with 'imbalance', 'ofi', 'microprice', 'best_bid/ask'
        ctx["events"]    list from book.detect_events()
        ctx["sweeps"]    list from orderflow.sweeps()
        ctx["large"]     list from orderflow.large_trades()
    """
    ctx = ctx or {}
    obs = []

    close = float(bars["close"][i])
    vwap = float(bars["vwap"][i])
    delta = float(bars["delta"][i])
    imb = float(bars["imbalance"][i])

    # ---------- tape: available today ----------
    if bars.get("absorption") is not None and bars["absorption"][i]:
        # absorption at a high is bearish, at a low is bullish
        side = -1 if close >= float(bars["high"][i]) - 1e-9 else 1
        obs.append(Observation("absorption", side, WEIGHTS["absorption"],
                               "heavy volume with no price move — someone is absorbing"))

    cd = bars.get("cvd_div")
    if cd is not None and cd[i] != 0:
        side = -1 if cd[i] == 1 else 1
        obs.append(Observation("cvd_divergence", side, WEIGHTS["cvd_divergence"],
                               "price and CVD are diverging"))

    dd = bars.get("delta_div")
    if dd is not None and dd[i] != 0:
        side = -1 if dd[i] == 1 else 1
        obs.append(Observation("delta_divergence", side, WEIGHTS["delta_divergence"],
                               "bar closed against its own delta"))

    if abs(imb) > 0.15:
        obs.append(Observation("tape_imbalance", np.sign(imb),
                               WEIGHTS["tape_imbalance"] * min(abs(imb), 1.0),
                               f"aggressor edge {imb:+.2f}"))

    obs.append(Observation("vwap_side", 1 if close > vwap else -1, WEIGHTS["vwap_side"],
                           f"price {'above' if close > vwap else 'below'} VWAP"))

    ts = np.asarray(bars["ts"])
    t = ts[i].astype("datetime64[s]").astype(np.int64) if ts.dtype.kind == "M" else int(ts[i])
    hour_utc = (t % 86400) // 3600
    if 13 <= hour_utc < 14:
        obs.append(Observation("session_phase", 0, WEIGHTS["session_phase"],
                               "US session open — order flow at its most volatile"))
    elif hour_utc >= 19:
        obs.append(Observation("session_phase", 0, WEIGHTS["session_phase"],
                               "session close — positioning into the bell"))

    for s in (ctx.get("sweeps") or [])[:3]:
        obs.append(Observation("sweep", 1 if s.get("side") == "BUY" else -1,
                               WEIGHTS["sweep"],
                               f"sweep {s.get('side')} through {s.get('levels')} levels"))

    for p in (ctx.get("large") or [])[:3]:
        if p.get("side") in ("BUY", "SELL"):
            obs.append(Observation("large_print", 1 if p["side"] == "BUY" else -1,
                                   WEIGHTS["large_print"],
                                   f"large {p['side']} print x{p.get('multiple')}"))

    # ---------- book: dormant until depth data is connected ----------
    book = ctx.get("book")
    if book:
        bi = float(book.get("imbalance", 0.0))
        if abs(bi) > 0.1:
            obs.append(Observation("book_imbalance", np.sign(bi),
                                   WEIGHTS["book_imbalance"] * min(abs(bi), 1.0),
                                   f"book tilted {bi:+.2f}", "book"))
        ofi = float(book.get("ofi", 0.0))
        if abs(ofi) > 0:
            obs.append(Observation("ofi", np.sign(ofi), WEIGHTS["ofi"] * min(abs(ofi) / 100, 1.0),
                                   f"OFI {ofi:+.0f}", "book"))

    for e in (ctx.get("events") or [])[:6]:
        kind = e.get("kind")
        side = 1 if e.get("side") == 0 else -1     # bid-side wall supports price
        if kind == "wall":
            obs.append(Observation("wall_ahead", side, WEIGHTS["wall_ahead"],
                                   f"wall {e.get('size')} @ {e.get('price')}", "book"))
        elif kind == "pulled":
            obs.append(Observation("wall_pulled", -side, WEIGHTS["wall_pulled"],
                                   f"wall pulled @ {e.get('price')}", "book"))
        elif kind == "iceberg":
            obs.append(Observation("iceberg", side, WEIGHTS["iceberg"],
                                   f"iceberg @ {e.get('price')}", "book"))

    return obs


def decide(obs, threshold=0.9, conflict_ratio=0.55, require_confirmed=True):
    """Weigh observations into a decision, and explain it.

    Returns dict with: action (-1/0/+1), score, confidence, reasons, blocked_by.

    Two guards matter more than the score itself:
      - conflict: when evidence is close to evenly split, stand aside. Mixed
        signal is information, not a reason to trade.
      - no book: decisions resting only on tape evidence are marked, so it is
        visible when the book half of the picture is missing.
    """
    if not obs:
        return {"action": 0, "score": 0.0, "confidence": 0.0,
                "reasons": [], "blocked_by": "no observations", "has_book": False}

    bull = sum(o["score"] for o in obs if o["score"] > 0)
    bear = -sum(o["score"] for o in obs if o["score"] < 0)
    score = bull - bear
    total = bull + bear
    has_book = any(o["source"] == "book" for o in obs)

    blocked = None

    # A crowd of weak features must not outvote the one that actually works.
    # Six walls at 0.06 each outweigh one book reading at 0.10, yet only the
    # book reading has a measured edge - so require it to be present and
    # pointing the same way as the net score.
    if require_confirmed:
        direction = 1 if score > 0 else -1 if score < 0 else 0
        confirmed_agrees = any(
            o["name"] in CONFIRMED and o["side"] == direction and o["weight"] > 0
            for o in obs
        )
        if direction != 0 and not confirmed_agrees:
            blocked = "no confirmed-edge feature backing this direction"

    if blocked is None and total > 0 and min(bull, bear) / total > conflict_ratio / 2:
        if abs(score) < threshold * 1.5:
            blocked = "conflicting signals — market undecided"

    action = 0
    if blocked is None and abs(score) >= threshold:
        action = int(np.sign(score))

    reasons = sorted([o for o in obs if o["score"] != 0],
                     key=lambda o: -abs(o["score"]))[:6]

    return {
        "action": action,
        "score": round(float(score), 3),
        "bull": round(float(bull), 3),
        "bear": round(float(bear), 3),
        "confidence": round(float(min(abs(score) / max(threshold * 2, 1e-9), 1.0)), 3),
        "reasons": reasons,
        "context": [o for o in obs if o["score"] == 0],
        "blocked_by": blocked,
        "has_book": has_book,
    }


def explain(decision):
    """One-line human summary, for the terminal and the trade log."""
    if decision["action"] == 0:
        why = decision["blocked_by"] or f"signal too weak ({decision['score']:+.2f})"
        return f"WAIT — {why}"
    word = "BUY" if decision["action"] > 0 else "SELL"
    top = ", ".join(o["name"] for o in decision["reasons"][:3])
    tag = "" if decision["has_book"] else " [no book]"
    return f"{word} ({decision['score']:+.2f}) — {top}{tag}"
