"""
Paper trading engine.

Simulates a full trading loop on bar data: signal, order, position, exit, P&L,
with real MNQ/NQ contract economics and hard risk limits.

Two rules matter more than the strategy itself:

1. NO LOOK-AHEAD. A signal computed on bar i can only be filled at the OPEN of
   bar i+1. Every backtest that fills on the signal bar's close is lying.
2. COSTS ARE NOT OPTIONAL. Every round trip pays commission plus exchange fees.
   On MNQ that is about 2.8 ticks, which kills most naive strategies - and it
   should kill them here rather than on a live account.

The strategy is deliberately simple and readable: order flow imbalance
confirmed by position against VWAP. Complexity gets added only after a simple
rule proves it has an edge.
"""

from __future__ import annotations

import numpy as np

CONTRACTS = {
    "MNQ": {"point_value": 2.0, "tick": 0.25, "rt_cost": 1.40, "name": "Micro E-mini Nasdaq-100"},
    "NQ": {"point_value": 20.0, "tick": 0.25, "rt_cost": 3.70, "name": "E-mini Nasdaq-100"},
    "MES": {"point_value": 5.0, "tick": 0.25, "rt_cost": 1.35, "name": "Micro E-mini S&P 500"},
    "ES": {"point_value": 50.0, "tick": 0.25, "rt_cost": 3.50, "name": "E-mini S&P 500"},
    # Percentage-fee venues. Sized by notional rather than contracts, so costs
    # scale with position value instead of being a flat per-round figure.
    "BINANCE_TAKER": {"pct": True, "fee_bps": 10.0, "notional": 1000.0,
                      "name": "Binance perp, wejscie i wyjscie rynkiem"},
    "BINANCE_MAKER": {"pct": True, "fee_bps": 4.0, "notional": 1000.0,
                      "name": "Binance perp, obie strony pasywnie"},
}

DEFAULTS = {
    "contract": "MNQ",
    "imbalance_entry": 0.35,   # minimum order flow imbalance to act on
    "target_ticks": 8,
    "stop_ticks": 6,
    "max_bars_in_trade": 12,   # time stop: do not sit in a dead trade
    "daily_loss_limit": 150.0,  # USD; hits -> no more trades that day
    "max_trades_per_day": 12,
    "start_equity": 2000.0,
    "use_vwap_filter": True,
    "block_on_divergence": True,
}


def _epoch_seconds(ts):
    """Normalise a timestamp column to integer epoch seconds.

    bars["ts"] arrives as datetime64[us] from orderflow, but as plain integer
    seconds when it has been through the JSON layer. Treating microseconds as
    seconds silently made every bar look like a new session.
    """
    arr = np.asarray(ts)
    if arr.dtype.kind == "M":
        return arr.astype("datetime64[s]").astype(np.int64)
    return arr.astype(np.int64)


def _signals(bars, cfg):
    """Return +1 long, -1 short, 0 flat, per bar. Uses only that bar's data."""
    imb = np.asarray(bars["imbalance"], dtype=float)
    close = np.asarray(bars["close"], dtype=float)
    vwap = np.asarray(bars["vwap"], dtype=float)
    div = np.asarray(bars.get("cvd_div", np.zeros(close.size)), dtype=int)

    long_ok = imb > cfg["imbalance_entry"]
    short_ok = imb < -cfg["imbalance_entry"]

    if cfg["use_vwap_filter"]:
        long_ok &= close > vwap
        short_ok &= close < vwap

    if cfg["block_on_divergence"]:
        long_ok &= div != 1      # bearish divergence blocks longs
        short_ok &= div != -1    # bullish divergence blocks shorts

    sig = np.zeros(close.size, dtype=np.int8)
    sig[long_ok] = 1
    sig[short_ok] = -1
    return sig


def run(bars, **overrides):
    """Run the paper simulation over bars. Returns trades, equity curve, stats."""
    cfg = {**DEFAULTS, **overrides}
    spec = CONTRACTS[cfg["contract"]]
    pct_mode = bool(spec.get("pct"))
    if pct_mode:
        # On a percentage venue there is no tick: express the target and stop
        # in basis points of price, and the cost as bps of notional.
        tick = None
        pv = spec["notional"]
        rt = spec["notional"] * spec["fee_bps"] / 10_000.0
    else:
        tick, pv, rt = spec["tick"], spec["point_value"], spec["rt_cost"]

    ts = _epoch_seconds(bars["ts"])
    o = np.asarray(bars["open"], dtype=float)
    h = np.asarray(bars["high"], dtype=float)
    lo = np.asarray(bars["low"], dtype=float)
    c = np.asarray(bars["close"], dtype=float)
    n = c.size
    if n < 5:
        return {"trades": [], "equity": [], "stats": {}, "config": cfg}

    sig = _signals(bars, cfg)
    day = ts // 86400

    equity = cfg["start_equity"]
    curve = []
    trades = []

    pos = 0            # 0 flat, +1 long, -1 short
    entry_px = 0.0
    entry_i = 0
    day_pnl = 0.0
    day_trades = 0
    cur_day = day[0]
    halted = False

    for i in range(n - 1):
        if day[i] != cur_day:            # new session resets the risk budget
            cur_day = day[i]
            day_pnl = 0.0
            day_trades = 0
            halted = False

        # ---- manage an open position on THIS bar
        if pos != 0:
            if pct_mode:
                tgt = entry_px * (1 + pos * cfg["target_ticks"] / 10_000.0)
                stp = entry_px * (1 - pos * cfg["stop_ticks"] / 10_000.0)
            else:
                tgt = entry_px + pos * cfg["target_ticks"] * tick
                stp = entry_px - pos * cfg["stop_ticks"] * tick
            exit_px = None
            reason = None

            # Pessimistic ordering: if both touched in one bar, assume the stop
            # filled first. Bar data cannot tell us which came first, and
            # assuming the good one is how backtests flatter themselves.
            if pos == 1:
                if lo[i] <= stp:
                    exit_px, reason = stp, "stop"
                elif h[i] >= tgt:
                    exit_px, reason = tgt, "target"
            else:
                if h[i] >= stp:
                    exit_px, reason = stp, "stop"
                elif lo[i] <= tgt:
                    exit_px, reason = tgt, "target"

            if exit_px is None and (i - entry_i) >= cfg["max_bars_in_trade"]:
                exit_px, reason = c[i], "time stop"
            if exit_px is None and day[i + 1] != day[i]:
                exit_px, reason = c[i], "session end"

            if exit_px is not None:
                if pct_mode:
                    gross = (exit_px - entry_px) / entry_px * pos * pv
                else:
                    gross = (exit_px - entry_px) * pos * pv
                net = gross - rt
                equity += net
                day_pnl += net
                trades.append({
                    "ts": int(ts[entry_i]),
                    "exit_ts": int(ts[i]),
                    "side": "LONG" if pos == 1 else "SHORT",
                    "entry": round(float(entry_px), 2),
                    "exit": round(float(exit_px), 2),
                    "ticks": round(float((exit_px - entry_px) / entry_px * pos * 10_000.0), 1)
                              if pct_mode else
                              round(float((exit_px - entry_px) * pos / tick), 1),
                    "gross": round(float(gross), 2),
                    "net": round(float(net), 2),
                    "reason": reason,
                    "bars": int(i - entry_i),
                    "equity": round(float(equity), 2),
                })
                pos = 0
                if day_pnl <= -abs(cfg["daily_loss_limit"]):
                    halted = True      # kill switch

        # ---- open a new position at the NEXT bar's open (no look-ahead)
        if (pos == 0 and not halted and sig[i] != 0
                and day_trades < cfg["max_trades_per_day"]):
            pos = int(sig[i])
            entry_px = float(o[i + 1])
            entry_i = i + 1
            day_trades += 1

        curve.append({"ts": int(ts[i]),
                      "equity": round(float(equity), 2)})

    return {"trades": trades, "equity": curve,
            "stats": _stats(trades, cfg, spec), "config": cfg}


def _stats(trades, cfg, spec):
    if not trades:
        return {"trades": 0, "note": "No trades — thresholds too tight or not enough data."}

    net = np.array([t["net"] for t in trades], dtype=float)
    wins = net[net > 0]
    losses = net[net <= 0]
    equity_series = np.array([t["equity"] for t in trades], dtype=float)
    peak = np.maximum.accumulate(equity_series)
    dd = equity_series - peak

    gross_win = wins.sum()
    gross_loss = abs(losses.sum())
    total = net.sum()

    # per-trade Sharpe, annualised on the observed trade frequency
    sharpe = float(net.mean() / net.std() * np.sqrt(len(net))) if net.std() > 0 else 0.0

    return {
        "trades": len(trades),
        "wins": int((net > 0).sum()),
        "losses": int((net <= 0).sum()),
        "win_rate": round(float((net > 0).mean() * 100), 1),
        "net_pnl": round(float(total), 2),
        "avg_trade": round(float(net.mean()), 2),
        "best": round(float(net.max()), 2),
        "worst": round(float(net.min()), 2),
        "profit_factor": round(float(gross_win / gross_loss), 2) if gross_loss > 0 else None,
        "max_drawdown": round(float(dd.min()), 2),
        "sharpe": round(sharpe, 2),
        "total_cost": round(len(trades) * (spec["notional"] * spec["fee_bps"] / 10_000.0
                                          if spec.get("pct") else spec["rt_cost"]), 2),
        "gross_pnl": round(float(sum(t["gross"] for t in trades)), 2),
        "final_equity": round(float(equity_series[-1]), 2),
        "return_pct": round(float(total / cfg["start_equity"] * 100), 2),
        "contract": cfg["contract"],
    }


if __name__ == "__main__":
    import sys
    import orderflow as of
    import yf_source

    sym = sys.argv[1] if len(sys.argv) > 1 else "NQ=F"
    tf = sys.argv[2] if len(sys.argv) > 2 else "5m"

    tape = yf_source.fetch(sym, tf)
    bars = of.bars(tape, tf)
    bars["cvd_div"] = of.cvd_divergence(bars)
    r = run(bars)

    print(f"\n  {sym} {tf} — paper trading on {r['config']['contract']}")
    print("  " + "-" * 52)
    s = r["stats"]
    if not s.get("trades"):
        print("  " + s.get("note", "no results"))
    else:
        for k, v in s.items():
            print(f"  {k:>16}: {v}")
        print("\n  last trades:")
        for t in r["trades"][-6:]:
            print(f"    {t['side']:<5} {t['entry']:>10.2f} -> {t['exit']:>10.2f}  "
                  f"{t['ticks']:>6.1f}t  {t['net']:>8.2f} USD  {t['reason']}")
