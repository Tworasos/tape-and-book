"""
Security layer for the local HTTP server.

A server bound to 127.0.0.1 is NOT automatically safe. Two attacks reach it
from any web page the user happens to visit:

  DNS REBINDING  An attacker points evil.com at 127.0.0.1. The browser treats
                 scripts from evil.com as same-origin with whatever answers
                 there, so their JavaScript can read this API. The only
                 reliable defence is checking the Host header: a rebound
                 request carries Host: evil.com, never 127.0.0.1.

  CSRF           Any page can issue cross-origin GETs (<img src=...>) and form
                 POSTs. If a state-changing endpoint answers GET, visiting a
                 malicious page is enough to reset the bot or change leverage.
                 Defence: mutations require POST, a matching Origin, and a
                 token the attacker cannot read.

Both are cheap to implement and both are non-negotiable for something that
holds trading state. The token lives in memory only and rotates every restart.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from urllib.parse import urlparse

# Only these Host values are served. Anything else is a rebinding attempt.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

# Browsers send no Origin on top-level navigations, which is fine for GET.
# For mutations an Origin is required and must match one of these.
def allowed_origins(port):
    return frozenset({
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    })


SESSION_TOKEN = secrets.token_urlsafe(32)

SECURITY_HEADERS = {
    # No third-party anything: everything this app needs is served locally.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cache-Control": "no-store",
}


def host_ok(host_header):
    """True when the Host header names this machine, not a rebound domain."""
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):                       # IPv6 literal
        end = host.find("]")
        name = host[: end + 1] if end != -1 else host
    else:
        name = host.split(":", 1)[0]
    return name.lower() in ALLOWED_HOSTS


def origin_ok(origin_header, port):
    """True when a cross-site request comes from our own page."""
    if not origin_header:
        return False
    try:
        u = urlparse(origin_header)
    except ValueError:
        return False
    if u.scheme != "http":
        return False
    return origin_header in allowed_origins(port)


def token_ok(supplied):
    """Constant-time comparison of the per-session token."""
    if not supplied:
        return False
    return hmac.compare_digest(str(supplied), SESSION_TOKEN)


class RateLimiter:
    """Crude fixed-window limiter. Stops a runaway script, not an attacker.

    Real protection here is the Host and Origin checks; this only keeps a buggy
    page from hammering the trading loop into the ground.
    """

    def __init__(self, limit=1200, window=10.0):
        # The UI polls ~12 endpoints per second and a user may keep two tabs
        # open, so a tight limit throttles the app itself. This is a runaway
        # guard, not a security control - the Host and Origin checks are.
        self.limit = limit
        self.window = window
        self._hits = []

    def allow(self):
        now = time.monotonic()
        cutoff = now - self.window
        self._hits = [t for t in self._hits if t > cutoff]
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True


def safe_subpath(root, candidate):
    """Resolve `candidate` under `root`, refusing traversal.

    Used for the static file routes. Returns None when the path escapes root.
    """
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, candidate.lstrip("/\\")))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target
