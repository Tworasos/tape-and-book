"""
Reader for Sierra Chart .scid intraday tick files.

Record layout is taken verbatim from C:\\SierraChart\\ACS_Source\\IntradayRecord.h
(struct s_IntradayRecord, 40 bytes) rather than from any third-party description:

    SCDateTimeMS DateTime;   // int64, microseconds since 1899-12-30 00:00:00
    float Open, High, Low, Close;
    uint32 NumTrades, TotalVolume, BidVolume, AskVolume;

The header (struct s_IntradayFileHeader) is 56 bytes and starts with the ASCII
magic "SCID".

The key detail for order flow: when Open == 0.0 and High != 0.0 and Low != 0.0,
the record is a SINGLE TRADE WITH BID/ASK. In that case the accessors in the
header file resolve to:

    bid price   = Low
    ask price   = High
    trade price = Close

so the aggressor side is recorded by the exchange, not inferred with a tick rule
or Lee-Ready. That is what makes these files usable for delta and CVD directly.
"""

from __future__ import annotations

import numpy as np

HEADER_SIZE = 56
RECORD_SIZE = 40
MAGIC = b"SCID"

# Sierra Chart epoch. SCDateTimeMS counts microseconds from this instant.
SC_EPOCH = np.datetime64("1899-12-30T00:00:00", "us")

RECORD_DTYPE = np.dtype(
    [
        ("datetime", "<i8"),
        ("open", "<f4"),
        ("high", "<f4"),
        ("low", "<f4"),
        ("close", "<f4"),
        ("num_trades", "<u4"),
        ("total_volume", "<u4"),
        ("bid_volume", "<u4"),
        ("ask_volume", "<u4"),
    ]
)
assert RECORD_DTYPE.itemsize == RECORD_SIZE, RECORD_DTYPE.itemsize


class ScidError(Exception):
    pass


def read_header(path):
    """Return (magic, header_size, record_size, version) and validate them."""
    with open(path, "rb") as fh:
        raw = fh.read(HEADER_SIZE)
    if len(raw) < HEADER_SIZE:
        raise ScidError(f"{path}: file shorter than a header ({len(raw)} bytes)")
    magic = raw[:4]
    if magic != MAGIC:
        raise ScidError(f"{path}: bad magic {magic!r}, expected {MAGIC!r}")
    header_size, record_size = np.frombuffer(raw, dtype="<u4", count=2, offset=4)
    version = int(np.frombuffer(raw, dtype="<u2", count=1, offset=12)[0])
    if record_size != RECORD_SIZE:
        raise ScidError(f"{path}: record size {record_size}, expected {RECORD_SIZE}")
    return magic, int(header_size), int(record_size), version


def memmap(path):
    """Memory-map the records. Does not read the file into RAM."""
    magic, header_size, record_size, _ = read_header(path)
    return np.memmap(path, dtype=RECORD_DTYPE, mode="r", offset=header_size)


def to_datetime64(raw_datetime):
    """Convert the raw SCDateTimeMS column to numpy datetime64[us] (UTC)."""
    return SC_EPOCH + raw_datetime.astype("timedelta64[us]")


def is_single_trade(rec):
    """Boolean mask: record is a single trade carrying bid/ask context.

    Mirrors s_IntradayRecord::IsSingleTradeWithBidAsk().
    """
    return (rec["open"] == 0.0) & (rec["high"] != 0.0) & (rec["low"] != 0.0)


def trades(path, start=None, stop=None):
    """Return a dict of arrays for the tick records in [start, stop).

    Keys: ts, price, bid, ask, volume, bid_volume, ask_volume, delta, is_trade.
    `delta` is ask_volume - bid_volume, i.e. aggressive buys minus aggressive
    sells, straight from the exchange feed.
    """
    rec = memmap(path)[start:stop]
    single = is_single_trade(rec)
    bid_v = rec["bid_volume"].astype(np.int64)
    ask_v = rec["ask_volume"].astype(np.int64)
    return {
        "ts": to_datetime64(np.asarray(rec["datetime"])),
        "price": np.asarray(rec["close"]),
        # bid/ask are only meaningful where `single` is True
        "bid": np.where(single, rec["low"], np.nan),
        "ask": np.where(single, rec["high"], np.nan),
        "volume": rec["total_volume"].astype(np.int64),
        "bid_volume": bid_v,
        "ask_volume": ask_v,
        "delta": ask_v - bid_v,
        "is_trade": single,
    }


def summary(path):
    """Cheap diagnostic pass over a whole file. Returns a plain dict."""
    rec = memmap(path)
    n = len(rec)
    if n == 0:
        return {"path": str(path), "records": 0}
    single = is_single_trade(rec)
    ts = to_datetime64(np.asarray(rec[["datetime"]]["datetime"]))
    bid_v = rec["bid_volume"].astype(np.int64)
    ask_v = rec["ask_volume"].astype(np.int64)
    spread = np.where(single, rec["high"] - rec["low"], np.nan)
    finite = spread[np.isfinite(spread)]
    return {
        "path": str(path),
        "records": int(n),
        "single_trade_records": int(single.sum()),
        "first_ts": str(ts[0]),
        "last_ts": str(ts[-1]),
        "price_min": float(np.min(rec["close"])),
        "price_max": float(np.max(rec["close"])),
        "total_volume": int(rec["total_volume"].astype(np.int64).sum()),
        "bid_volume": int(bid_v.sum()),
        "ask_volume": int(ask_v.sum()),
        "cvd_close": int((ask_v - bid_v).sum()),
        "median_spread": float(np.median(finite)) if finite.size else float("nan"),
    }


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        try:
            info = summary(arg)
        except ScidError as exc:
            print(f"SKIP {exc}")
            continue
        for key, value in info.items():
            print(f"{key:>22}: {value}")
        print()
