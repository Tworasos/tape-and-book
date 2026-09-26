"""Scan the Sierra Chart data folder and report order flow stats per file.

Entry point for the desktop launcher. Prints a readable report in Polish and
writes a CSV of per-file summaries to out/.
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import features
import scid

SC_DATA = r"C:\SierraChart\Data"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


def find_files(folder):
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".scid"):
            continue
        path = os.path.join(folder, name)
        if os.path.getsize(path) > scid.HEADER_SIZE:
            out.append(path)
    return out


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else SC_DATA
    files = find_files(folder)

    print("=" * 66)
    print("  TAPE & BOOK - Sierra Chart file analysis")
    print("=" * 66)
    print(f"  folder: {folder}")

    if not files:
        print()
        print("  No .scid files with data were found.")
        print("  Files of exactly 56 bytes are empty headers - Sierra")
        print("  created them but never downloaded any data.")
        print()
        return 1

    print(f"  files with data: {len(files)}")
    print()

    rows = []
    for path in files:
        name = os.path.basename(path)
        try:
            info = scid.summary(path)
        except scid.ScidError as exc:
            print(f"  [SKIP] {name}: {exc}")
            continue

        print("-" * 66)
        print(f"  {name}")
        print("-" * 66)
        print(f"    records        : {info['records']:,}")
        print(f"    date range      : {info['first_ts'][:10]}  ..  {info['last_ts'][:10]}")
        print(f"    prices            : {info['price_min']:,.2f}  ..  {info['price_max']:,.2f}")
        print(f"    volume         : {info['total_volume']:,}")
        print(f"    aggressive buying : {info['ask_volume']:,}")
        print(f"    aggressive selling : {info['bid_volume']:,}")
        print(f"    closing CVD   : {info['cvd_close']:,}")
        if info["single_trade_records"]:
            print(f"    ticks with bid/ask : {info['single_trade_records']:,}  (spread computable)")
        else:
            print("    ticks with bid/ask : 0  (aggregated records - delta OK, spread unavailable)")

        try:
            print()
            for line in features.describe(path).splitlines():
                print(f"    {line}")
        except Exception as exc:  # noqa: BLE001 - diagnostic tool, keep going
            print(f"    (features not computed: {exc})")
        print()

        rows.append(info)

    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUT_DIR, "scid_summary.csv")
    if rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print("=" * 66)
        print(f"  Summary written: {csv_path}")

    print("=" * 66)
    print()
    print("  WHAT THIS MEANS")
    print("  - CVD is the running difference between aggressive buying and selling.")
    print("    Rising = buyers pressing. Falling = sellers.")
    print("  - A divergence is price and CVD disagreeing. Price rising while CVD falls")
    print("    means a large seller is quietly filling the buyers.")
    print("  - This is data from your Sierra Chart install, not from Nasdaq.")
    print("    For NQ you need Databento data - see README.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
