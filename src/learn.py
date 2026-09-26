"""
Learning layer — turns the journal into measured weights.

This is the honest kind of learning: for every observation type we count how
often it was followed by a move in the direction it predicted, and by how much.
A weight then comes from that measurement instead of from my guess.

Why not a neural network. On tick data a flexible model memorises noise almost
immediately, and its confidence tells you nothing about whether the edge is
real. A per-feature hit rate is weaker, but it is:

    interpretable  you can see which reads work and which do not
    auditable      every weight traces back to a count you can check
    honest         a feature with no signal lands near 0.5 and gets downweighted

Guards that keep it from fooling us:

    MIN_SAMPLES    a feature seen a handful of times gets no weight change
    SHRINKAGE      weights move toward the measurement, never jump to it
    WILSON         hit rates use a lower confidence bound, so a 3/3 record does
                   not outrank a 600/1000 one
    HOLDOUT        score() reports in-sample and out-of-sample separately; if
                   they disagree, the "edge" is fitting noise
    ONE PER ROW    a feature counts once per journal row, by its net side, so
                   twelve copies of it in one moment are one sample, not twelve
    FLAT EXCLUDED  a horizon where price did not move is neither a hit nor a
                   miss; counting it as a miss drags every feature below 50%
"""

from __future__ import annotations

import json
import math
import os

import journal

MIN_SAMPLES = 40
SHRINKAGE = 0.35          # how far a weight moves toward the evidence per update
BASE_HORIZON = "30"       # measured: book imbalance is strongest at 30s
                          # and decays with horizon, as microstructure
                          # information should
WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "weights.json")


def wilson_lower(hits, n, z=1.96):
    """Lower bound of the hit rate. Small samples are punished automatically."""
    if n == 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _tally(rows, horizon=BASE_HORIZON):
    """Per-observation-type counts of correct direction and average move.

    `n` counts rows in which the feature had a net direction; `decided` only
    those where price actually moved, which is the hit-rate denominator.
    """
    stats = {}
    for row in rows:
        fwd = row.get("fwd", {}).get(horizon)
        if fwd is None:
            continue
        # One vote per feature per row. Several copies of the same observation
        # in one moment (three walls, two sweeps) share one outcome; counting
        # them separately multiplied the sample size without adding evidence.
        net, src = {}, {}
        for o in row.get("obs", []):
            net[o["n"]] = net.get(o["n"], 0) + o["s"]
            src.setdefault(o["n"], o.get("src", "tape"))
        for name, total in net.items():
            side = (total > 0) - (total < 0)
            if side == 0:
                continue
            st = stats.setdefault(name, {
                "n": 0, "decided": 0, "hits": 0, "sum_bps": 0.0, "sum_abs": 0.0,
                "src": src[name],
            })
            st["n"] += 1
            # signed move in the direction the observation predicted
            aligned = fwd * side
            if aligned != 0:
                st["decided"] += 1
                if aligned > 0:
                    st["hits"] += 1
            st["sum_bps"] += aligned
            st["sum_abs"] += abs(fwd)
    return stats


def analyse(symbol=None, days=7, horizon=BASE_HORIZON, holdout=0.3):
    """Measure each observation type, in-sample and out-of-sample."""
    rows = journal.load(symbol, days)
    if not rows:
        return {"rows": 0, "features": [], "note": "Journal empty — the bot has to run first."}

    cut = int(len(rows) * (1 - holdout))
    train, test = rows[:cut], rows[cut:]

    tr = _tally(train, horizon)
    te = _tally(test, horizon)

    features = []
    for name, st in sorted(tr.items(), key=lambda kv: -kv[1]["n"]):
        n, decided, hits = st["n"], st["decided"], st["hits"]
        rate = hits / decided if decided else 0.0
        lower = wilson_lower(hits, decided)
        avg = st["sum_bps"] / n if n else 0.0
        t = te.get(name)
        oos_rate = (t["hits"] / t["decided"]) if t and t["decided"] else None
        oos_avg = (t["sum_bps"] / t["n"]) if t and t["n"] else None
        features.append({
            "name": name,
            "source": st["src"],
            "samples": n,
            "decided": decided,
            "flat": n - decided,
            "hit_rate": round(rate * 100, 1),
            "hit_lower": round(lower * 100, 1),
            "avg_bps": round(avg, 2),
            "oos_samples": t["decided"] if t else 0,
            "oos_hit_rate": round(oos_rate * 100, 1) if oos_rate is not None else None,
            "oos_avg_bps": round(oos_avg, 2) if oos_avg is not None else None,
            "agrees": (None if oos_rate is None
                       else bool((rate > 0.5) == (oos_rate > 0.5))),
            "enough": decided >= MIN_SAMPLES,
        })

    return {
        "rows": len(rows),
        "legacy_rows": sum(1 for r in rows if not r.get("v")),
        "train_rows": len(train),
        "test_rows": len(test),
        "horizon_s": int(horizon),
        "features": features,
    }


def propose_weights(symbol=None, days=7, horizon=BASE_HORIZON, current=None):
    """Move weights toward measured skill. Returns proposals with reasons."""
    import decision

    current = dict(current or decision.WEIGHTS)
    report = analyse(symbol, days, horizon)
    out = []

    for f in report.get("features", []):
        name = f["name"]
        base = current.get(name, 0.4)
        if not f["enough"]:
            out.append({"name": name, "old": base, "new": base,
                        "reason": f"not enough samples ({f['decided']}/{MIN_SAMPLES})",
                        "changed": False, **f})
            continue

        # Skill maps 50% hit rate -> 0 weight, 100% -> 1. Use the lower bound so
        # a lucky streak cannot inflate a weight.
        skill = max(0.0, (f["hit_lower"] / 100.0 - 0.5) * 2.0)
        target = round(min(1.0, skill), 3)

        if f["agrees"] is False:
            target = round(target * 0.4, 3)      # train and test disagree: distrust
            reason = "test sample contradicts training — weight cut"
        elif skill <= 0.02:
            reason = "no measurable edge"
        else:
            reason = f"hit rate {f['hit_rate']}% (lower bound {f['hit_lower']}%)"

        new = round(base + (target - base) * SHRINKAGE, 3)
        out.append({"name": name, "old": round(base, 3), "new": new,
                    "target": target, "reason": reason,
                    "changed": abs(new - base) > 0.005, **f})

    out.sort(key=lambda x: -x["samples"])
    return {"report": report, "proposals": out}


def apply_weights(proposals):
    """Persist accepted weights and update the running decision layer."""
    import decision

    changed = {}
    for p in proposals:
        if p.get("changed"):
            decision.WEIGHTS[p["name"]] = p["new"]
            changed[p["name"]] = p["new"]
    if changed:
        try:
            os.makedirs(os.path.dirname(WEIGHTS_FILE), exist_ok=True)
            merged = dict(decision.WEIGHTS)
            with open(WEIGHTS_FILE, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass
    return {"applied": changed, "count": len(changed)}


def load_saved():
    """Restore learned weights on startup, if any were saved."""
    import decision

    try:
        with open(WEIGHTS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    applied = {}
    for k, v in saved.items():
        if isinstance(v, (int, float)) and 0 <= v <= 2:
            decision.WEIGHTS[k] = float(v)
            applied[k] = float(v)
    return applied


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else None
    r = propose_weights(sym)
    rep = r["report"]
    if not rep.get("features"):
        print(" ", rep.get("note", "no data"))
        raise SystemExit(0)
    print("")
    print(f"  rows: {rep['rows']}  (train {rep['train_rows']} / test {rep['test_rows']})"
          f"  horizon {rep['horizon_s']}s")
    print("")
    print(f"  {'feature':<18}{'samples':>8}{'hit%':>7}{'lower':>7}{'OOS%':>7}"
          f"{'avg bps':>9}{'weight':>8}{'->':>4}{'new':>7}  reason")
    print("  " + "-" * 104)
    for p in r["proposals"]:
        oos = f"{p['oos_hit_rate']:.1f}" if p["oos_hit_rate"] is not None else "-"
        print(f"  {p['name']:<18}{p['samples']:>8}{p['hit_rate']:>7.1f}"
              f"{p['hit_lower']:>7.1f}{oos:>7}{p['avg_bps']:>9.2f}"
              f"{p['old']:>8.2f}{'->':>4}{p['new']:>7.2f}  {p['reason']}")
