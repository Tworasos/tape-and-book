"""Shared fixtures. Nothing here touches the network: feeds are built but never
started, and synthetic Binance messages are pushed straight into _handle()."""

from __future__ import annotations

import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

import decision  # noqa: E402
import journal  # noqa: E402
import learn  # noqa: E402
import live  # noqa: E402
import trader  # noqa: E402

TICK = 0.1


def depth_msg(E, top=100.0, sizes=None, levels=20, symbol="btcusdt"):
    """A depth20 message: bids from `top` down, asks from top+tick up.

    `sizes` maps ("b"|"a", index) to a size; every other level holds 1.
    """
    sizes = sizes or {}
    b = [[f"{top - i * TICK:.1f}", str(sizes.get(("b", i), 1))] for i in range(levels)]
    a = [[f"{top + TICK + i * TICK:.1f}", str(sizes.get(("a", i), 1))] for i in range(levels)]
    return {"stream": f"{symbol}@depth20@100ms", "data": {"E": E, "b": b, "a": a}}


def trade_msg(T, price, qty, sell=False, symbol="btcusdt"):
    return {"stream": f"{symbol}@trade",
            "data": {"e": "trade", "T": T, "p": str(price), "q": str(qty), "m": sell}}


@pytest.fixture
def feed():
    """A live feed that never connects; every depth message becomes a snapshot."""
    return live.LiveFeed("btcusdt", tick_size=TICK, snapshot_ms=0)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate everything that writes to disk or opens a socket."""
    monkeypatch.setattr(journal, "DATA_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(trader, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(learn, "WEIGHTS_FILE", str(tmp_path / "weights.json"))
    monkeypatch.setattr(decision, "WEIGHTS", dict(decision.WEIGHTS))

    feeds = {}

    def offline_feed(symbol="btcusdt"):
        symbol = symbol.lower()
        if symbol not in feeds:
            feeds[symbol] = live.LiveFeed(symbol, tick_size=TICK, snapshot_ms=0)
        return feeds[symbol]

    monkeypatch.setattr(live, "get_feed", offline_feed)
    monkeypatch.setattr(trader, "_TRADERS", {})
    monkeypatch.setattr(journal, "_JOURNALS", {})
    yield feeds
    for t in list(trader._TRADERS.values()):
        t.stop()
