# Gold Daily Intelligence Agent

A small, modular pipeline that turns a morning's worth of market inputs (economic calendar, macro data, positioning, news) into **one structured daily briefing for gold (XAU/USD)**, shown on a live browser dashboard that sits next to a TradingView chart.

> It is an **information tool**. It does not place trades, send signals or give financial advice.

## Why it is built this way

Every unit (calendar, fundamentals, news, synthesis) follows the same pattern and emits the **same JSON contract**:

```
raw data (HTTP / web page)  ->  interpretation layer (LLM)  ->  standard JSON
```

```json
{
  "unit": "calendar | fundamental | news | synthesis",
  "timestamp": "2026-01-15T06:00:00Z",
  "bias": "bullish | bearish | neutral",
  "impact": "high | medium | low",
  "confidence": 0.0,
  "reasoning": "why this conclusion was reached",
  "raw_events": []
}
```

As long as the contract holds, any unit can be replaced, re-run or upgraded on its own without breaking the others. The synthesis unit reads the other three and produces the briefing (overall bias, high-risk events with times, caution notes).

```
 calendar ─┐
 fundamental ─┼─> synthesis ─> logs/<unit>/<date>.json ─> dashboard (/api/today)
 news ─────┘
```

## What is in this repo

| Path | What it does |
|---|---|
| `dashboard/server.py` | Tiny local HTTP server. `/api/today` merges today's four unit files; serves the UI. Falls back to demo data on a fresh clone. |
| `dashboard/index.html` | Single-file dashboard (HUD style): bias, confidence, high-risk events, cautions, positioning, news, TradingView chart. Timezone defaults to your browser's; override with `?tz=Europe/Berlin`. |
| `bridge/claude_bridge.py` | HTTP bridge so a containerised workflow tool (e.g. n8n in Docker) can call the `claude -p` CLI on the host. Loopback-only by default, optional bearer token, request size cap, no shell. |
| `scripts/fetch_fundamentals.py` | Pulls free official data: FRED (dollar index proxy, 10y real yield, CPI, NFP) and the CFTC COT report for COMEX gold. |
| `examples/logs/` | **Synthetic** sample output for each unit, clearly marked as example data. |
| `tests/test_all.py` | 17 unit/integration tests (no network or CLI needed). |

The interpretation step is deliberately pluggable: any process that writes valid JSON to `logs/<unit>/<YYYY-MM-DD>.json` feeds the dashboard. The bridge is one way to do that with an LLM.

## Quick start (no keys needed)

```bash
python dashboard/server.py
# open http://127.0.0.1:8790  -> shows demo data with a banner
```

Python 3.9+, standard library only.

## Use real data

```bash
export FRED_API_KEY=...                       # free: https://fred.stlouisfed.org/docs/api/api_key.html
python scripts/fetch_fundamentals.py          # prints JSON: FRED series + COT positioning
```

Write each unit's JSON (following the contract above) to `logs/<unit>/<YYYY-MM-DD>.json` and the dashboard switches from demo to live data automatically.

### Using the LLM bridge

```bash
python bridge/claude_bridge.py                # 127.0.0.1:8787
curl -s localhost:8787/interpret -d '{"prompt":"Return JSON: {\"ok\": true}"}'
```

Binding to a non-loopback address (needed on some Docker setups) requires a token or the bridge refuses to start:

```bash
BRIDGE_HOST=0.0.0.0 BRIDGE_TOKEN=<long random string> python bridge/claude_bridge.py
# clients send:  Authorization: Bearer <token>
```

## Engineering notes

- **Untrusted output:** the dashboard renders text that came from an LLM reading web pages. Everything is HTML-escaped and links are limited to `http(s)`, so a hostile page cannot inject script into the dashboard.
- **Secrets:** API keys come from environment variables; a test scans the repo for hard-coded secrets.
- **Bridge hardening:** loopback default, constant-time token check, body size limit, errors logged server-side instead of echoed to callers.
- **Resilience:** missing or corrupt unit files are reported in the UI instead of crashing the server; network failures in the fetcher become per-series error objects.
- **Config:** ports, hosts and the logs directory are environment variables.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Limitations

- Daily/weekly sources (FRED, COT) update slowly, so re-running the fundamental unit hourly mostly re-interprets the same data.
- The dashboard is a local tool: one user, no authentication, meant for `127.0.0.1`.

*Not financial advice.*
