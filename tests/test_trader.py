"""Paper trader settings: bad values are refused, not silently fatal."""

from __future__ import annotations

import math

import pytest

import trader


@pytest.mark.parametrize("key,value", [
    ("leverage", 0),            # used to raise ZeroDivisionError on every tick
    ("leverage", -5),
    ("capital", math.nan),
    ("capital", math.inf),
    ("fee_bps", "ten"),
    ("fee_bps", [1]),
    ("nonsense", 1),
])
def test_bad_settings_are_refused(key, value):
    with pytest.raises(ValueError):
        trader.validate(key, value)


def test_settings_keep_their_type():
    assert trader.validate("max_trades_per_day", "4") == 4
    assert trader.validate("compound", True) == 1
    assert trader.validate("fee_bps", 10) == 10.0


def test_configure_is_all_or_nothing(sandbox):
    t = trader.LiveTrader("btcusdt")
    before = dict(t.cfg)
    with pytest.raises(ValueError):
        t.configure(fee_bps=10, leverage=0)
    assert t.cfg == before
    t.configure(fee_bps=10, leverage=20)
    assert t.cfg["fee_bps"] == 10.0 and t.cfg["leverage"] == 20.0


def test_restore_ignores_bad_saved_settings(sandbox):
    t = trader.LiveTrader("btcusdt")
    t.cfg["leverage"] = 0.0             # as if hand-edited into the state file
    t.save()
    fresh = trader.LiveTrader("btcusdt")
    assert fresh.restore()
    assert fresh.cfg["leverage"] == trader.DEFAULTS["leverage"]


def test_sub_cent_prices_survive_in_trade_history(sandbox):
    t = trader.LiveTrader("dogeusdt")
    t._open(1, 0.21345, "test")
    t._close(0.21401, "target")
    rec = t.trades[0]
    assert rec["entry"] == 0.21345 and rec["exit"] == 0.21401
