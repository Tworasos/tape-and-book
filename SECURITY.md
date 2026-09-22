# Security

## Threat model

Tape & Book runs a local HTTP server that holds trading state. Binding to
`127.0.0.1` alone does **not** make that safe: two attacks reach a localhost
server from any web page the user happens to open.

| Attack | How it works | Defence here |
|---|---|---|
| **DNS rebinding** | Attacker points `evil.com` at `127.0.0.1`. The browser then treats their JavaScript as same-origin with this server and can read the API. | Every request must carry a `Host` header naming this machine. A rebound request carries `Host: evil.com` and is refused with 403. |
| **CSRF** | Any page can issue cross-origin `GET`s (`<img src=…>`) and form `POST`s. If a state-changing endpoint answered `GET`, merely visiting a malicious page could reset the bot or change leverage. | Mutations are `POST` only, require a matching `Origin`, and require a session token the page fetches at load. Cross-origin readers cannot read that token. |
| **Path traversal** | `/vendor/../../etc/passwd`, or `?file=../../secrets` | All paths resolve through `security.safe_subpath()`, which refuses anything escaping its root. File parameters must be bare basenames. |
| **Clickjacking / injection** | Framing the UI, or injecting markup through data | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, a strict CSP with no external origins, and HTML-escaping of every value rendered from data. |

## What is deliberately absent

- **No API keys anywhere.** Every data source used (Binance public WebSocket
  and REST, Yahoo, local Sierra Chart files) is public and key-free. There is
  nothing to steal from this repository.
- **No outbound calls except market data.** The server talks to the exchange
  feed and nothing else. No telemetry, no analytics, no remote logging.
- **No order placement.** The bot is paper-only. There is no broker
  integration and no code path that can place a real order.

## If you add a broker

The moment you wire in real execution, this threat model changes completely:

1. Put credentials in environment variables or an OS keychain — never in the
   repository. `.gitignore` already excludes `.env`, `*.key`, `*.pem`.
2. Give the API key **trade-only** permissions. Never enable withdrawals.
3. Whitelist your IP at the exchange.
4. Treat the local server as privileged: anyone who reaches it could move real
   money. Consider requiring the token on reads too.

## Reporting

Open an issue. Do not include keys, tokens, or account identifiers.
