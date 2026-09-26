"""Book event detection: each snapshot read once, and only real events reported."""

from __future__ import annotations

from collections import Counter

import book
from conftest import TICK, depth_msg


def snap(ts, top=100.0, sizes=None, levels=20, drop=()):
    """Snapshot in the shape LiveFeed stores: (price, size) per side.

    `drop` removes (side, index) levels to simulate an emptied price.
    """
    sizes = sizes or {}
    bids = [(round(top - i * TICK, 1), sizes.get(("b", i), 1.0))
            for i in range(levels) if ("b", i) not in drop]
    asks = [(round(top + TICK + i * TICK, 1), sizes.get(("a", i), 1.0))
            for i in range(levels) if ("a", i) not in drop]
    return {"ts": ts, "bids": bids, "asks": asks}


WALL = {("b", 5): 50.0}


def kinds(events, kind):
    return [e for e in events if e["kind"] == kind]


def test_live_feed_reports_a_standing_wall_once_per_snapshot(feed):
    # Regression: the feed re-scanned the last three snapshots every second,
    # so a wall standing still was reported up to three times per snapshot.
    for k in range(8):
        feed._handle(depth_msg(k * 1000, sizes={("b", 5): 50}))
    per_snap = Counter(e["snap"] for e in feed.events if e["kind"] == "wall")
    assert set(per_snap.values()) == {1}
    assert len(per_snap) == 8


def test_wall_leaving_the_visible_window_is_not_pulled():
    # Price rallies 3.0: the bid wall at 99.5 is now 35 levels deep, outside a
    # top-20 snapshot. It went out of sight; nobody pulled it.
    det = book.EventDetector(TICK)
    det.update(snap(0, sizes=WALL))
    found = det.update(snap(1, top=103.0))
    assert kinds(found, "pulled") == []


def test_wall_removed_inside_the_visible_range_is_pulled():
    det = book.EventDetector(TICK)
    det.update(snap(0, sizes=WALL))
    found = det.update(snap(1, drop={("b", 5)}))
    pulled = kinds(found, "pulled")
    assert len(pulled) == 1
    assert pulled[0]["side"] == 0
    assert abs(pulled[0]["price"] - 99.5) < 1e-9


def test_wall_that_price_traded_through_is_not_pulled():
    # Best bid falls from 100.0 to 99.3: the wall at 99.5 was consumed.
    det = book.EventDetector(TICK)
    det.update(snap(0, sizes=WALL))
    found = det.update(snap(1, top=99.3))
    assert kinds(found, "pulled") == []


def test_iceberg_fires_after_repeated_refills_and_only_once():
    # Regression: with a three-snapshot window icebergs could never fire live.
    # The best bid at 100.0 is hit empty and refills, over and over.
    det = book.EventDetector(TICK, iceberg_refills=3)
    found = []
    for k in range(12):
        gone = {("b", 0)} if k % 2 else set()
        found += det.update(snap(k, drop=gone))
    icebergs = kinds(found, "iceberg")
    assert len(icebergs) == 1
    assert icebergs[0]["side"] == 0
    assert abs(icebergs[0]["price"] - 100.0) < 1e-9


def test_a_level_simply_present_does_not_count_as_refills():
    # Regression: after one depletion every later snapshot counted as a refill.
    det = book.EventDetector(TICK, iceberg_refills=3)
    found = det.update(snap(0))
    found += det.update(snap(1, drop={("b", 0)}))
    for k in range(2, 10):
        found += det.update(snap(k))
    assert kinds(found, "iceberg") == []


def test_deeper_levels_flickering_are_not_icebergs():
    det = book.EventDetector(TICK, iceberg_refills=3)
    found = []
    for k in range(12):
        gone = {("b", 3)} if k % 2 else set()
        found += det.update(snap(k, drop=gone))
    assert kinds(found, "iceberg") == []


def test_price_crossing_back_and_forth_is_not_an_iceberg():
    # The ask at 100.1 is traded through and re-quoted as price chops.
    det = book.EventDetector(TICK, iceberg_refills=3)
    found = []
    for k in range(12):
        found += det.update(snap(k, top=100.0 if k % 2 == 0 else 100.5))
    assert kinds(found, "iceberg") == []


def test_batch_detection_has_no_look_ahead():
    # The wall threshold at a snapshot must not depend on later snapshots.
    base = [snap(k, sizes=WALL) for k in range(4)]
    early = book.detect_events(base, TICK)
    later = book.detect_events(base + [snap(9, sizes={("a", i): 500.0 for i in range(20)})], TICK)
    assert [e for e in later if e["ts"] < 9] == early


def test_refill_memory_is_pruned():
    det = book.EventDetector(TICK, memory=10)
    det.update(snap(0))
    det.update(snap(1, drop={("b", 0)}))
    det.update(snap(2))                              # one refill at 100.0
    assert (0, 1000) in det._refills
    for k in range(3, 130):
        det.update(snap(k, top=100.0 + k))          # price leaves the level behind
    assert (0, 1000) not in det._refills
    assert len(det._depleted) + len(det._refills) <= 4 * (10 + 60)
