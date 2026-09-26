# Tape & Book

An order flow terminal and research harness. It reads the **order book** and the
**trade tape**, turns them into named observations, and measures whether those
observations actually predict anything.

Runs locally. No API keys, no accounts, no cloud. Paper trading only.

```
python src/server.py        →  http://127.0.0.1:8777
```

---

## Why this exists

Most retail trading tools show you indicators computed from price — RSI, moving
averages, MACD. Those are re-arrangements of the same number. They contain no
information about *who is buying, how aggressively, and against what resistance*.

Order flow does. The book shows resting liquidity: who is waiting, at what price,
in what size. The tape shows who crossed the spread to get filled. Together they
describe the mechanics of a move rather than its shadow.

This project reads both, and — importantly — **measures itself**. Every
observation is logged with the price at that moment; 30, 120 and 300 seconds
later, the actual move is appended. From those labels it computes how often each
observation was right, and reweights accordingly.

That measurement is the point. The repository ships with a result where the
baseline rule **loses money before costs**. It is kept on purpose: a tool that
only reports success is a sales brochure, not an instrument.

---

## The three views

| View | URL | What it is for |
|---|---|---|
| **History** | `/` | Order flow on recorded Sierra Chart `.scid` files: footprint, volume profile, CVD, sweeps, large prints |
| **Live** | `/live` | Real-time depth from Binance: liquidity heatmap, DOM ladder, footprint, tape, book events, the decision panel and a demo bot |
| **Learning** | `/learn` | What the bot measured: per-observation hit rate, out-of-sample check, proposed weights |

---

## How it works

```
         ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
data ───▶│   live.py    │──▶│   book.py    │──▶│ decision.py  │──▶ trader.py
         │ Binance WS   │   │ order book   │   │ weigh + say  │    paper P&L
         │ depth+trades │   │ walls, OFI   │   │    why       │        │
         └──────────────┘   └──────────────┘   └──────────────┘        │
                                                       │               │
                                                       ▼               ▼
                                                 journal.py  ◀────  observations
                                                 record + label      + outcome
                                                       │
                                                       ▼
                                                  learn.py
                                            measure skill → weights
```

### Modules

| File | Responsibility |
|---|---|
| `live.py` | Live Binance feed (depth + trades), tick-size autodetection, 10 pairs |
| `book.py` | Order book state, liquidity heatmap, walls, pulls, icebergs, voids, OFI, microprice |
| `orderflow.py` | Footprint with diagonal imbalance, CVD, volume profile (POC/VAH/VAL), absorption, sweeps, large prints |
| `decision.py` | Reads market state into weighted observations and decides — **with reasoning attached** |
| `trader.py` | Paper trader: leverage, compounding, liquidation, daily limits, persistence |
| `journal.py` | Logs every observation and labels it with what price did next |
| `learn.py` | Turns labels into measured hit rates, then into weights |
| `scid.py` | Sierra Chart `.scid` reader (layout taken from `IntradayRecord.h`, not from guesswork) |
| `binance_hist.py` | Historical klines **with real taker-buy volume** — true delta, not inferred |
| `yf_source.py` | Free Yahoo bars incl. `NQ=F`; delta here is **estimated**, and labelled as such |
| `paper.py` | Offline backtester with contract economics and no look-ahead |
| `security.py` | Host/Origin validation, session token, CSP, path-traversal guards |
| `server.py` | Local HTTP server and JSON API |

---

## Order flow mechanics implemented

**From the book** (needs depth data — live on Binance today):

- **Liquidity heatmap** — Bookmap-style time × price grid of resting size
- **Walls** — unusually large resting orders (threshold on standard deviation)
- **Pulls** — liquidity withdrawn as price approaches it
- **Stacking** — liquidity added into an approach
- **Icebergs** — a level that refills repeatedly after being hit
- **Voids** — gaps in the book
- **OFI** — order flow imbalance, Cont–Kukanov–Stoikov construction
- **Microprice** — depth-weighted fair value

**From the tape:**

- **Footprint** with diagonal imbalance (buying at price *p* vs selling one tick below)
- **CVD** — cumulative volume delta, and its divergence from price
- **Absorption** — heavy volume that fails to move price
- **Sweeps** — one aggressor clearing several levels inside a window
- **Large prints** — two independent filters (percentile and multiple of median)
- **Volume profile** — POC, value area high/low
- **Session context** — the same reading means different things at the open and the close

---

## How the bot learns

Not a neural network, and that is deliberate. On tick data a flexible model
memorises noise within minutes, and its confidence says nothing about whether an
edge is real.

Instead there is a measurement loop:

1. `journal.py` records **every** observation with the price at that moment
2. after 30 / 120 / 300 s it appends what price actually did, in basis points
3. `learn.py` counts how often each observation type was right
4. the weight follows that measurement, not anyone's intuition

What goes into a journal row matters as much as how it is scored. The row is
taken once a second, and observations come in two kinds:

- **state** — book imbalance, walls standing right now, tape imbalance — read
  fresh every second
- **events** — a wall pulled, an iceberg, a sweep, a large print — recorded
  **once**, in the first row after they happened, not in every row while they
  remain in a rolling window

Seven guards keep it from fooling itself:

| Guard | Prevents |
|---|---|
| each event counted once | one large print becoming hundreds of "samples" |
| one vote per feature per row | three walls in one moment counting as three outcomes |
| flat outcomes not scored | "price did not move" counting as a miss for every feature |
| minimum 40 samples | a feature seen a handful of times moving a weight |
| Wilson lower bound | 3 hits out of 3 outranking 600 out of 1000 |
| 70/30 train–test split | fitting noise — disagreement cuts the weight to 40% |
| shrinkage (35% per update) | one strange day flipping the bot |

The book reader has its own guards: a top-20 snapshot shows only a window of
the book, so a wall that scrolls out of view as price moves away is not
reported as pulled, and one that price traded through was consumed, not
pulled. Icebergs are read at the touch — the best level emptying and refilling
at the same price.

Weights persist to `data/weights.json` and reload at startup.

Journal rows carry a format version (`"v": 2`). Rows written before it
duplicated event observations — a wall could appear three times per snapshot,
a large print stayed in every row for minutes — so event-feature counts from
them are inflated. The `/learn` page says how many such rows it is reading;
delete `data/journal/` to measure from a clean slate.

**What this cannot do:** it measures whether *existing* observations have an
edge. It will not invent new ones. If nothing clears 50% out-of-sample, the
honest answer is "these signals carry no edge" — and that is also a result.

---

## Data sources

| Source | Depth | Aggressor side | Cost |
|---|---|---|---|
| **Binance** (live + historical) | full book | **exchange-reported** | free, no key |
| **Sierra Chart `.scid`** | no | **exchange-reported** volume | needs a data subscription |
| **Yahoo** (`NQ=F`, etc.) | no | **estimated** by tick rule | free |

The distinction matters more than it looks. On Yahoo bars the tick rule assigns a
whole bar's volume to one side, so imbalance is always ±1.0 and any filter on it
is meaningless — a lesson learned the hard way and documented here so nobody
repeats it. Binance klines carry `takerBuyBaseVolume`, which is exchange truth.

### Getting Nasdaq futures

The engine is source-agnostic. For NQ/MNQ you need:

- **History** — Databento (`GLBX.MDP3`, schema `mbp-10`); new accounts get
  starting credit
- **Live** — CME Level 2 for non-professionals plus a feed provider
  (Rithmic, or Sierra Chart with a depth subscription)

Wiring that in means writing one feed class alongside `live.py`. Everything
downstream is unchanged.

---

## Security

A localhost server holding trading state is a real target. See
**[SECURITY.md](SECURITY.md)** for the full model. Summary:

- `Host` header validation blocks **DNS rebinding**
- state changes are `POST` + `Origin` check + session token — blocks **CSRF**
- path traversal refused on every file route
- strict CSP, no external origins, no framing
- no API keys exist in this project; every source is public

Verified by simulated attack:

```
GET  /api/files                          200   ok
GET  /api/files   Host: evil.com         403   rebinding blocked
GET  /api/trader/reset                   405   CSRF-by-GET blocked
POST /api/trader/reset  (no token)       403   blocked
POST /api/trader/reset  (bad origin)     403   blocked
GET  /vendor/../../src/server.py         404   traversal blocked
POST /api/trader/toggle (token+origin)   200   ok
```

---

## Install

Requires Python 3.10+ (tested on 3.14). Dependencies: `numpy`, `websockets`.
Optional: `yfinance` for Yahoo bars, `pandas` for the offline tools.

```bash
git clone https://github.com/<you>/tape-and-book.git
cd tape-and-book
pip install numpy websockets
python src/server.py
```

The browser opens at `/live`. Depth starts streaming immediately; the liquidity
heatmap builds from one snapshot per second, so give it a minute to fill.

### Tests

```bash
pip install pytest
python -m pytest -q
```

No network needed: live feeds are driven with synthetic Binance messages. The
server tests replay the attacks listed under **Security** against the real
handler. CI runs the suite on Python 3.10 and 3.13 and syntax-checks every
script the pages load.

### Offline tools

```bash
python src/analyze.py                  # report on local .scid files
python src/binance_hist.py BTCUSDT 5m  # historical bars with real delta
python src/paper.py NQ=F 5m            # backtest with contract economics
python src/learn.py btcusdt            # what the journal has measured
```

---

## Status and honesty

What works: live depth, the full mechanics list above, the paper bot, the
measurement loop, persistence across restarts.

What is **not** established: that any of it is profitable. The measured result
so far is that a simple imbalance rule on 5-minute bars loses before costs. The
open question — and the reason the journal exists — is whether book-sourced
observations on a horizon of seconds do better.

Read **[DISCLAIMER.md](DISCLAIMER.md)** before connecting this to anything real.

## License

MIT — see [LICENSE](LICENSE).
