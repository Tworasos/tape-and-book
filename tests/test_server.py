"""The HTTP server, attacked the way SECURITY.md says it is defended.

Starts the real handler on an ephemeral port. Live feeds are replaced by
offline ones (see conftest.sandbox), so no test opens a connection outward.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import security
import server
from conftest import depth_msg, trade_msg


@pytest.fixture
def srv(sandbox, monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(server, "PORT", port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None, raw_length=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": f"127.0.0.1:{port}"}
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if raw_length is not None:
        conn.putrequest(method, path, skip_host=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", raw_length)
        conn.endheaders()
    else:
        conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    payload = r.read()
    conn.close()
    try:
        return r.status, json.loads(payload or b"null")
    except ValueError:
        return r.status, payload


def mutation_headers(port, token=True, origin=True):
    h = {"Content-Type": "application/json"}
    if origin:
        h["Origin"] = f"http://127.0.0.1:{port}"
    if token:
        h["X-Token"] = security.SESSION_TOKEN
    return h


def test_reads_work(srv):
    status, body = call(srv, "GET", "/api/files")
    assert status == 200 and "files" in body


def test_dns_rebinding_is_refused(srv):
    status, _ = call(srv, "GET", "/api/files", headers={"Host": "evil.com"})
    assert status == 403


def test_csrf_by_get_is_refused(srv):
    status, _ = call(srv, "GET", "/api/trader/reset")
    assert status == 405


def test_mutation_without_token_is_refused(srv):
    status, _ = call(srv, "POST", "/api/trader/toggle", {},
                     headers=mutation_headers(srv, token=False))
    assert status == 403


def test_mutation_from_foreign_origin_is_refused(srv):
    h = mutation_headers(srv)
    h["Origin"] = "http://evil.com"
    status, _ = call(srv, "POST", "/api/trader/toggle", {}, headers=h)
    assert status == 403


def test_path_traversal_is_refused(srv):
    status, _ = call(srv, "GET", "/vendor/../../src/server.py")
    assert status in (400, 404)


def test_mutation_with_token_and_origin_works(srv):
    status, body = call(srv, "POST", "/api/trader/toggle", {},
                        headers=mutation_headers(srv))
    assert status == 200 and "running" in body


def test_bad_setting_is_a_client_error(srv):
    status, body = call(srv, "POST", "/api/trader/config", {"leverage": 0},
                        headers=mutation_headers(srv))
    assert status == 400 and "leverage" in body["error"]


def test_negative_content_length_is_refused(srv):
    # Regression: read(-1) waited for EOF on a keep-alive connection.
    status, _ = call(srv, "POST", "/api/trader/toggle",
                     headers=mutation_headers(srv), raw_length="-1")
    assert status == 400


def test_unknown_horizon_is_refused(srv):
    status, _ = call(srv, "GET", "/api/learn/report?horizon=7")
    assert status == 400
    status, _ = call(srv, "POST", "/api/learn/apply", {"horizon": "7"},
                     headers=mutation_headers(srv))
    assert status == 400


def test_garbage_query_parameter_falls_back(srv):
    status, body = call(srv, "GET", "/api/trader/history?n=lots")
    assert status == 200 and "trades" in body


def test_live_snapshot_shape_matches_the_page(srv, sandbox):
    # Regression: live.js read all.large.trades and all.sweeps.sweeps, but both
    # are plain arrays, so large prints and sweeps never appeared on the page.
    status, _ = call(srv, "GET", "/api/live/status")
    feed = sandbox["btcusdt"]
    feed._handle(depth_msg(0))
    for j in range(60):
        feed._handle(trade_msg(j, 100.0, 0.01))
    feed._handle(trade_msg(100, 100.0, 5.0))
    status, body = call(srv, "GET", "/api/live/all")
    assert status == 200
    assert isinstance(body["large"], list) and body["large"]
    assert isinstance(body["sweeps"], list)
    assert "observations" in body["decision"]


def test_decision_panel_uses_the_bots_threshold(srv, sandbox):
    import trader
    t = trader.get_trader("btcusdt")
    t.configure(min_score=0.05)
    feed = sandbox["btcusdt"]
    feed._handle(depth_msg(0, sizes={("b", i): 5 for i in range(20)}))   # heavy bid
    _, body = call(srv, "GET", "/api/live/all")
    d = body["decision"]
    assert d["action"] == 1, d
    t.configure(min_score=50)
    _, body = call(srv, "GET", "/api/live/all")
    assert body["decision"]["action"] == 0
