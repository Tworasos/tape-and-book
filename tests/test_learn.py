"""The measurement loop: what counts as a sample, and what counts as a hit."""

from __future__ import annotations

import json
import os

import journal
import learn


def row(obs, fwd30, v=journal.VERSION):
    r = {"ts": 0, "price": 100.0, "score": 0, "action": 0,
         "obs": [{"n": n, "s": s, "w": 0.5, "src": "book"} for n, s in obs],
         "fwd": {"30": fwd30}}
    if v:
        r["v"] = v
    return r


def test_flat_outcome_is_neither_hit_nor_miss():
    # Regression: fwd == 0 counted as a miss for both directions, dragging a
    # coin flip on a coarse-tick symbol well below 50%.
    rows = [row([("f", 1)], 1.0), row([("f", 1)], -1.0)] + [row([("f", 1)], 0.0)] * 8
    st = learn._tally(rows, "30")["f"]
    assert st["n"] == 10
    assert st["decided"] == 2
    assert st["hits"] == 1


def test_copies_of_a_feature_in_one_row_are_one_sample():
    # Regression: twelve walls in one row were twelve samples of one outcome.
    rows = [row([("wall_ahead", 1)] * 6, 2.0)]
    st = learn._tally(rows, "30")["wall_ahead"]
    assert st["n"] == 1 and st["hits"] == 1


def test_opposite_copies_cancel_out():
    rows = [row([("wall_ahead", 1), ("wall_ahead", -1)], 2.0)]
    assert "wall_ahead" not in learn._tally(rows, "30")


def test_wilson_punishes_small_samples():
    assert learn.wilson_lower(3, 3) < learn.wilson_lower(600, 1000)
    assert learn.wilson_lower(0, 0) == 0.0


def write_journal(rows, symbol="btcusdt"):
    os.makedirs(journal.DATA_DIR, exist_ok=True)
    with open(os.path.join(journal.DATA_DIR, f"{symbol}_2026-01-01.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_report_counts_legacy_rows_and_uses_decided_samples(sandbox):
    rows = [row([("f", 1)], 1.0, v=None) for _ in range(10)]
    rows += [row([("f", 1)], 1.0 if i % 3 else 0.0) for i in range(90)]
    write_journal(rows)
    rep = learn.analyse("btcusdt", horizon="30")
    assert rep["legacy_rows"] == 10
    f = rep["features"][0]
    assert f["samples"] == f["decided"] + f["flat"]
    assert f["hit_rate"] == 100.0          # every move went the predicted way


def test_journal_rows_carry_a_version(sandbox):
    j = journal.Journal("btcusdt", horizons=(30,))
    j.record(100.0, [{"name": "f", "side": 1, "weight": 0.5, "source": "book"}], 0.5, 0)
    assert j.pending[0]["v"] == journal.VERSION
