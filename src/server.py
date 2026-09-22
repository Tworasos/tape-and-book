"""
Local HTTP server for Tape & Book.

Serves the three views (historical terminal, live terminal, learning report)
and the JSON API behind them. Runs on Python's built-in http.server so the
project has no web dependencies at all.

Security model — see security.py for the reasoning:

  * bound to 127.0.0.1 only, never 0.0.0.0
  * every request must carry a Host header naming this machine (anti DNS
    rebinding)
  * reads are GET; anything that changes state is POST, requires a matching
    Origin, and a session token the page fetches at load
  * static files resolve under web/ with traversal refused
  * strict CSP, no external origins, no framing

Usage:  python server.py [port]
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import decision  # noqa: E402
import journal as journalmod  # noqa: E402
import learn as learnmod  # noqa: E402
import live as livemod  # noqa: E402
import orderflow as of  # noqa: E402
import scid  # noqa: E402
import security  # noqa: E402
import trader as tradermod  # noqa: E402

ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")

# Where to look for Sierra Chart .scid files. The local path is optional and
# simply yields no files when absent.
DATA_DIRS = [
    os.environ.get("TAB_SCID_DIR", r"C:\SierraChart\Data"),
    os.path.join(ROOT, "data", "scid"),
]

PORT = 8777

# Endpoints that change state. Everything here is POST-only.
MUTATIONS = {
    "/api/trader/reset",
    "/api/trader/config",
    "/api/trader/toggle",
    "/api/learn/apply",
}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/live": ("live.html", "text/html; charset=utf-8"),
    "/live.html": ("live.html", "text/html; charset=utf-8"),
    "/learn": ("learn.html", "text/html; charset=utf-8"),
    "/learn.html": ("learn.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/live.js": ("live.js", "application/javascript; charset=utf-8"),
    "/learn.js": ("learn.js", "application/javascript; charset=utf-8"),
}

_limiter = security.RateLimiter()


# --------------------------------------------------------------------------
# file discovery
# --------------------------------------------------------------------------
def find_files():
    out = []
    seen = set()
    for folder in DATA_DIRS:
        if not folder or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".scid") or name in seen:
                continue
            path = os.path.join(folder, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size <= scid.HEADER_SIZE:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "size_mb": round(size / 1048576, 1),
                "records": int((size - scid.HEADER_SIZE) // scid.RECORD_SIZE),
            })

    # Rank for the UI default: a breadth index like TICK-NYSE is not tradable
    # and renders as noise, so push those to the end.
    def rank(f):
        n = f["name"].upper()
        return (1 if n.startswith("TICK") or n.startswith("$") else 0, -f["records"])

    out.sort(key=rank)
    return out


def resolve(name):
    """Map a requested file name to a real path, without trusting the name."""
    if not name or os.path.basename(name) != name:
        return None
    for folder in DATA_DIRS:
        if not folder or not os.path.isdir(folder):
            continue
        candidate = security.safe_subpath(folder, name)
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


_CACHE = {}
_CACHE_LOCK = __import__("threading").Lock()


def tape(path):
    """Cache the loaded tape per file; loading is the expensive part.

    Serialised deliberately: the UI fires several requests for one file at
    once, and without the lock each would re-read millions of records.
    """
    key = (path, os.path.getmtime(path))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        _CACHE.clear()
        _CACHE[key] = of.load(path)
        return _CACHE[key]


def jsonable(obj):
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind == "M":
            return obj.astype("datetime64[s]").astype(np.int64).tolist()
        if obj.dtype.kind == "b":
            return obj.astype(int).tolist()
        if obj.dtype.kind in "iu":
            return obj.astype(np.int64).tolist()
        return np.nan_to_num(obj.astype(float), nan=0.0).tolist()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    return obj


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TapeAndBook"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # keep the console readable

    # ---------- plumbing ----------
    def _send(self, body, ctype="application/json; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in security.SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(jsonable(obj), ensure_ascii=False), code=code)

    def _guard(self):
        """Common checks. Returns False when the request was already refused."""
        if not security.host_ok(self.headers.get("Host")):
            self._send("forbidden host", "text/plain; charset=utf-8", 403)
            return False
        if not _limiter.allow():
            # JSON, because the frontend parses every response as JSON and a
            # bare string here crashed it instead of degrading gracefully.
            self._json({"error": "rate limited"}, 429)
            return False
        return True

    def _guard_mutation(self):
        """Extra checks for state-changing requests: origin plus token."""
        if not security.origin_ok(self.headers.get("Origin"), PORT):
            self._json({"error": "bad origin"}, 403)
            return False
        token = self.headers.get("X-Token") or ""
        if not security.token_ok(token):
            self._json({"error": "bad or missing token"}, 403)
            return False
        return True

    # ---------- verbs ----------
    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        if u.path not in MUTATIONS:
            self._json({"error": "not a mutation endpoint"}, 404)
            return
        if not self._guard_mutation():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536)) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            self._mutate(u.path, payload)
        except Exception as exc:  # noqa: BLE001 - report, never crash the loop
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        route = u.path

        try:
            if route in MUTATIONS:
                self._json({"error": "use POST for this endpoint"}, 405)
                return
            if route in STATIC:
                name, ctype = STATIC[route]
                return self._file(os.path.join(WEB, name), ctype)
            if route.startswith("/vendor/"):
                target = security.safe_subpath(os.path.join(WEB, "vendor"),
                                               route[len("/vendor/"):])
                if not target:
                    return self._send("bad path", "text/plain; charset=utf-8", 400)
                return self._file(target, "application/javascript; charset=utf-8")
            if route == "/api/session":
                # The page reads this once and echoes the token back on writes.
                return self._json({"token": security.SESSION_TOKEN, "port": PORT})
            return self._read(route, q)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------- mutations ----------
    def _mutate(self, route, payload):
        symbol = str(payload.get("symbol", "btcusdt"))
        if route == "/api/learn/apply":
            proposals = learnmod.propose_weights(symbol)["proposals"]
            return self._json(learnmod.apply_weights(proposals))

        tr = tradermod.get_trader(symbol)
        if route == "/api/trader/reset":
            tr.reset()
            return self._json({"ok": True, "state": tr.state()})
        if route == "/api/trader/toggle":
            tr.stop() if tr.running else tr.start()
            return self._json({"running": tr.running})
        if route == "/api/trader/config":
            allowed = ("capital", "leverage", "max_trades_per_day", "fee_bps",
                       "target_pct", "stop_pct", "min_score", "compound",
                       "max_notional", "confirm_ticks", "cooldown_s")
            tr.configure(**{k: payload[k] for k in allowed if k in payload})
            tr.save()
            return self._json(tr.state())
        return self._json({"error": "unknown mutation"}, 404)

    # ---------- reads ----------
    def _read(self, route, q):
        if route == "/api/files":
            return self._json({"files": find_files()})

        if route.startswith("/api/live/"):
            what = route[len("/api/live/"):]
            if what == "symbols":
                return self._json({"symbols": livemod.SYMBOLS})
            feed = livemod.get_feed(q.get("symbol", "btcusdt"))
            readers = {
                "status": feed.status,
                "heatmap": feed.heatmap,
                "trades": lambda: {"trades": feed.recent_trades(int(q.get("n", 60)))},
                "events": lambda: {"events": feed.recent_events(int(q.get("n", 40)))},
                "dom": lambda: feed.dom(int(q.get("levels", 20))),
                "tape": lambda: feed.tape_stats() or {},
                "footprint": feed.footprint,
                "large": lambda: {"trades": feed.large()},
                "sweeps": lambda: {"sweeps": feed.sweeps()},
                "cvd": feed.cvd_curve,
            }
            if what == "all":
                # One round trip instead of nine. The UI polls this every
                # second; separate calls were both slower and self-throttling.
                return self._json({
                    "status": feed.status(),
                    "dom": feed.dom(int(q.get("levels", 14))),
                    "trades": feed.recent_trades(60),
                    "events": feed.recent_events(40),
                    "tape": feed.tape_stats() or {},
                    "footprint": feed.footprint(),
                    "large": feed.large(),
                    "sweeps": feed.sweeps(),
                    "decision": self._live_decision(feed),
                })
            if what in readers:
                return self._json(readers[what]())
            if what == "decision":
                return self._json(self._live_decision(feed))
            return self._send("not found", "text/plain; charset=utf-8", 404)

        if route.startswith("/api/trader/"):
            tr = tradermod.get_trader(q.get("symbol", "btcusdt"))
            what = route[len("/api/trader/"):]
            if what == "state":
                return self._json(tr.state())
            if what == "history":
                return self._json({"trades": tr.history(int(q.get("n", 60)))})
            if what == "curve":
                return self._json(tr.equity_curve())
            return self._send("not found", "text/plain; charset=utf-8", 404)

        if route.startswith("/api/learn/"):
            what = route[len("/api/learn/"):]
            if what == "report":
                return self._json(learnmod.propose_weights(
                    q.get("symbol", "btcusdt"),
                    days=int(q.get("days", 7)),
                    horizon=q.get("horizon", learnmod.BASE_HORIZON)))
            if what == "weights":
                return self._json({"weights": dict(decision.WEIGHTS)})
            return self._send("not found", "text/plain; charset=utf-8", 404)

        # historical endpoints, all keyed by a .scid file
        path = resolve(q.get("file", ""))
        if route.startswith("/api/") and not path:
            return self._json({"error": "file not found"}, 404)

        tf = q.get("tf", "5m")
        if route == "/api/bars":
            bars = of.bars(tape(path), tf)
            if not bars:
                return self._json({"error": "no data"}, 404)
            out = {k: v for k, v in bars.items() if not k.startswith("_")}
            out["cvd_div"] = of.cvd_divergence(bars)
            return self._json(out)
        if route == "/api/footprint":
            return self._json(of.footprint(tape(path), tf,
                                           max_bars=int(q.get("bars", 60)),
                                           imbalance_ratio=float(q.get("ratio", 3.0))))
        if route == "/api/heatmap":
            return self._json(of.heatmap(tape(path), tf,
                                         max_cols=int(q.get("cols", 320)),
                                         max_rows=int(q.get("rows", 200))))
        if route == "/api/large":
            return self._json(of.large_trades(tape(path), top=int(q.get("top", 200)),
                                              percentile=float(q.get("pct", 99.0))))
        if route == "/api/sweeps":
            return self._json(of.sweeps(tape(path), top=int(q.get("top", 150))))
        if route == "/api/profile":
            return self._json(of.volume_profile(tape(path)))
        if route == "/api/summary":
            return self._json(scid.summary(path))

        return self._send("not found", "text/plain; charset=utf-8", 404)

    @staticmethod
    def _live_decision(feed):
        st = feed.status()
        obs = []
        bi = st.get("imbalance") or 0.0
        if abs(bi) > 0.1:
            obs.append(decision.Observation(
                "book_imbalance", 1 if bi > 0 else -1,
                decision.WEIGHTS["book_imbalance"] * min(abs(bi), 1.0),
                f"book tilted {bi:+.2f}", "book"))
        for e in feed.recent_events(12):
            side = 1 if e.get("side") == 0 else -1
            kind = e.get("kind")
            if kind == "wall":
                obs.append(decision.Observation("wall_ahead", side,
                    decision.WEIGHTS["wall_ahead"],
                    f"wall {int(e.get('size', 0))} @ {e.get('price')}", "book"))
            elif kind == "pulled":
                obs.append(decision.Observation("wall_pulled", -side,
                    decision.WEIGHTS["wall_pulled"], f"pulled @ {e.get('price')}", "book"))
            elif kind == "iceberg":
                obs.append(decision.Observation("iceberg", side,
                    decision.WEIGHTS["iceberg"], f"iceberg @ {e.get('price')}", "book"))
        recent = feed.recent_trades(40)
        buys = sum(1 for t in recent if t["side"] == "BUY")
        if recent:
            tb = (buys - (len(recent) - buys)) / len(recent)
            if abs(tb) > 0.15:
                obs.append(decision.Observation("tape_imbalance", 1 if tb > 0 else -1,
                    0.35 * min(abs(tb), 1.0),
                    f"tape {buys}B/{len(recent) - buys}S", "tape"))
        d = decision.decide(obs)
        d["explain"] = decision.explain(d)
        d["observations"] = obs
        return d

    def _file(self, path, ctype):
        if not path or not os.path.isfile(path):
            return self._send("not found", "text/plain; charset=utf-8", 404)
        with open(path, "rb") as fh:
            return self._send(fh.read(), ctype)


# --------------------------------------------------------------------------
def main():
    global PORT
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    url = f"http://127.0.0.1:{PORT}"

    restored = learnmod.load_saved()
    files = find_files()

    print("=" * 64)
    print("   TAPE & BOOK — order flow terminal")
    print("=" * 64)
    print(f"   address        : {url}")
    print(f"   views          : /  (history)   /live   /learn")
    print(f"   scid files     : {len(files)}")
    if restored:
        print(f"   learned weights: {len(restored)} restored")
    print(f"   bound to       : 127.0.0.1 only (not reachable from the network)")
    print()
    print("   Keep this window open — it is the server.")
    print("=" * 64)

    def shutdown_cleanly():
        """Persist everything before exit, or a restart loses the demo equity
        and every observation still waiting for its label."""
        for t in list(tradermod._TRADERS.values()):
            t.stop()
            t.save()
        for j in list(journalmod._JOURNALS.values()):
            j.close()

    atexit.register(shutdown_cleanly)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: (shutdown_cleanly(), os._exit(0)))
        except (ValueError, OSError):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        webbrowser.open(f"{url}/live")
    except Exception:  # noqa: BLE001 - headless is fine
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n   stopped")


if __name__ == "__main__":
    main()
