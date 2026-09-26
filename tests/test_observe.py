"""The live observer: state read every tick, one-off events counted once."""

from __future__ import annotations

from collections import Counter

import decision
import trader
from conftest import depth_msg, trade_msg


def names(obs):
    return Counter(o["name"] for o in obs)


def run_ticks(feed, ticks, per_tick):
    """Drive `ticks` seconds of feed, calling per_tick(k) to add traffic."""
    seen, out = None, []
    for k in range(ticks):
        per_tick(k)
        obs, seen = trader.observe(feed, seen)
        out.append(obs)
    return out


def quiet_tape(feed, k, n=50, offset=0):
    for j in range(n):
        feed._handle(trade_msg(k * 1000 + offset + j, 100.0, 0.01, sell=j % 2 == 0))


def test_large_print_is_counted_once(feed):
    # Regression: a print stayed in the observations for as long as it sat in
    # the last 1200 trades - minutes on a quiet symbol.
    def traffic(k):
        feed._handle(depth_msg(k * 1000))
        quiet_tape(feed, k)
        if k == 2:
            feed._handle(trade_msg(k * 1000 + 60, 100.0, 5.0))

    per_tick = run_ticks(feed, 8, traffic)
    assert [names(o)["large_print"] for o in per_tick] == [0, 0, 1, 0, 0, 0, 0, 0]


def test_standing_wall_is_state_and_seen_every_tick(feed):
    def traffic(k):
        feed._handle(depth_msg(k * 1000, sizes={("b", 5): 50}))
        quiet_tape(feed, k)

    per_tick = run_ticks(feed, 5, traffic)
    assert [names(o)["wall_ahead"] for o in per_tick] == [1] * 5


def test_wall_still_visible_when_no_new_snapshot_arrived(feed):
    feed._handle(depth_msg(0, sizes={("b", 5): 50}))
    obs, seen = trader.observe(feed, None)
    obs, seen = trader.observe(feed, seen)          # nothing new since
    assert names(obs)["wall_ahead"] == 1


def test_pulled_wall_is_counted_once(feed):
    def traffic(k):
        sizes = {("b", 5): 50} if k < 3 else {("b", 5): 0}
        feed._handle(depth_msg(k * 1000, sizes=sizes))
        quiet_tape(feed, k)

    per_tick = run_ticks(feed, 7, traffic)
    assert sum(names(o)["wall_pulled"] for o in per_tick) == 1
    assert names(per_tick[3])["wall_pulled"] == 1


def test_sweep_is_counted_once_while_it_grows(feed):
    # An aggressive buyer lifts four levels just before a tick and four more
    # just after it: one sweep, seen partly on one tick and whole on the next.
    def lift(t0, first_level):
        for j in range(4):
            feed._handle(trade_msg(t0 + j * 10, round(100.1 + (first_level + j) * 0.1, 1), 0.5))

    def traffic(k):
        feed._handle(depth_msg(k * 1000))
        if k == 3:
            quiet_tape(feed, k)
            lift(3950, 0)
        elif k == 4:
            lift(4000, 4)
            quiet_tape(feed, k, offset=500)
        else:
            quiet_tape(feed, k)

    per_tick = run_ticks(feed, 8, traffic)
    assert sum(names(o)["sweep"] for o in per_tick) == 1


def test_print_arriving_mid_call_is_counted_once(feed):
    # A print landing between reading the cursor and reading the tape belongs
    # to the next call only - not to both.
    feed._handle(depth_msg(0))
    quiet_tape(feed, 0)
    real = feed.last_trade

    def racing_last_trade():
        t = real()
        feed._handle(trade_msg(900, 100.0, 5.0))
        feed.last_trade = real
        return t

    obs, seen = trader.observe(feed, None)
    feed.last_trade = racing_last_trade
    first, seen = trader.observe(feed, seen)
    second, _ = trader.observe(feed, seen)
    assert names(first)["large_print"] + names(second)["large_print"] == 1


def test_first_call_does_not_replay_old_events(feed):
    # A panel (no cursor) must not show a print from long ago as if it were new.
    feed._handle(depth_msg(0))
    feed._handle(trade_msg(0, 100.0, 5.0))
    for k in range(1, 6):
        quiet_tape(feed, k)
    obs, seen = trader.observe(feed, None)
    assert names(obs)["large_print"] == 0
    obs, _ = trader.observe(feed, seen)
    assert names(obs)["large_print"] == 0


def test_tape_imbalance_weight_is_learnable(feed, monkeypatch):
    # Regression: the bot used a hard-coded 0.4, so a learned weight for
    # tape_imbalance was stored and never used.
    monkeypatch.setitem(decision.WEIGHTS, "tape_imbalance", 0.1)
    feed._handle(depth_msg(0))
    for j in range(100):
        feed._handle(trade_msg(j, 100.0, 0.01))      # all buyers: imbalance +1
    obs, _ = trader.observe(feed, None)
    tape = [o for o in obs if o["name"] == "tape_imbalance"]
    assert len(tape) == 1 and abs(tape[0]["weight"] - 0.1) < 1e-9
