# Backtesting (VectorBT + OpenAlgo)

Local backtesting that pulls data **from our own OpenAlgo instance**, so backtests
use the *same broker feed the live strategies trade on*.

## Why this exists

A third-party backtest (Volrix) agreed with our live trades on only **50% of days**
(6/12). The largest identified cause was a different data source for the daily
Heikin-Ashi bias. This setup removes that variable, and gives **13 months** of
history instead of Volrix's 6-month plan cap.

## Setup (already done — recorded for reproducibility)

```bash
npx skills add marketcalls/vectorbt-backtesting-skills   # -> .agents/skills/ (gitignored)
uv venv venv --python 3.12
uv pip install --python venv/Scripts/python.exe openalgo vectorbt plotly anywidget \
    nbformat pandas numpy yfinance python-dotenv tqdm scipy numba ipywidgets \
    openstatz ccxt duckdb psutil
```

TA-Lib is deliberately **not** installed: the skill pack's own guidance is that
`from openalgo import ta` covers the same indicators plus ~90 more, and it avoids
the Windows C-library build. Add it later only if a script explicitly needs `talib`.

Config lives in the **root `.env`** (gitignored). The backtesting keys were
*appended* to the existing 431-line app config, not written over it:

```
OPENALGO_API_KEY=...            # from the OpenAlgo dashboard
OPENALGO_HOST=https://openalgo.inikhilesh.com
DUCKDB_PATH=                    # optional
HISTORIFY_DB_PATH=              # optional: direct DuckDB, no rate limit
```

A pre-change backup is at `.env.bak.pre-backtest`.

## Running

```bash
# from the repo root (Windows)
./venv/Scripts/python.exe backtesting/haema_signal/NIFTY_haema_signal_backtest.py
```

Agent commands from the skill pack also work: `/backtest`, `/optimize`,
`/quick-stats`, `/strategy-compare`, `/setup`.

## Data notes

- `client.history(..., source="api")` — broker API, rate-limited ~3 req/s (default).
- `client.history(..., source="db")` — OpenAlgo DuckDB/Historify, **no rate limit**,
  supports custom intervals (`2m`, `4h`, `W`, ...). The VPS has `db/historify.duckdb`;
  set `HISTORIFY_DB_PATH` to use it directly.
- Verified available: NIFTY `NSE_INDEX` daily (270 bars) and 5m (13,439 bars) back to
  2025-06-23, including the current session.

## SCOPE LIMIT — read before trusting any number here

VectorBT backtests **signals on a price series**. Our live strategies **buy weekly
options**. This harness therefore measures the *directional edge of the signal* on
the index (a futures-style proxy) and **cannot** model strike selection, premium,
theta or gamma.

That is still the decisive test: if the signal has no directional edge on the index,
no option-side tuning can rescue it.

## Result: HA-EMA signal, 13 months, 127 trades

```
win rate       37.8%          net P&L   Rs -84,687
avg per trade  -10.3 points   Sharpe    -6.94
total return   -4.23%         buy&hold  -3.78%
```

Costs applied are futures-level only (0.018% + Rs 20/order) — far lighter than the
~1% option slippage the live system actually pays. **The signal loses ~10 index
points per trade before option costs even enter the picture.**

Two secondary observations:

1. **67 of 127 entries (53%) fire at 09:45** — the first bar of the entry window.
   The strategy is largely "trade the 09:45 breakout", which matches live behaviour.
2. The weekday ranking here (Wed the only positive; Thu/Fri negative) **contradicts**
   the Volrix weekday ranking (Thu/Fri best). Two datasets disagreeing on which
   weekdays pay is strong evidence the weekday effect is noise — consistent with the
   decision not to deploy the Mon+Tue skip filter.

Trade-by-trade parity with live is *not* expected from this proxy (fixed 0.10%/0.20%
stops vs live candle-extreme stops; close-based stop checks vs live tick polling).
Use it for aggregate signal edge, not for reconciling individual fills.

## Local DuckDB cache (`HISTORIFY_DB_PATH`)

**The VPS Historify DB is empty.** `/opt/openalgo/db/historify.duckdb` is 536 KB,
untouched since 2025-06-20, and every table — including `market_data` — has **0 rows**.
Pointing `HISTORIFY_DB_PATH` at it would gain nothing, so the cache is built locally
in the identical Historify schema:

```bash
./venv/Scripts/python.exe backtesting/ingest_duckdb.py --days 400   # idempotent
```

Contents of `backtesting/data/market_cache.duckdb` (7.6 MB, gitignored):

| symbol | exchange | interval | rows | span |
|---|---|---|--:|---|
| NIFTY | NSE_INDEX | 1m | 67,349 | 2025-06-23 .. 2026-07-28 |
| NIFTY | NSE_INDEX | D | 270 | 2025-06-23 .. 2026-07-27 |
| SENSEX | BSE_INDEX | 1m | 94,375 | 2025-06-23 .. 2026-07-28 |
| SENSEX | BSE_INDEX | D | 154 | 2026-01-12 .. 2026-07-27 |

Only `1m` and `D` are stored (Historify convention); other timeframes resample from
1m. Scripts default to `BT_SOURCE=db`; use `BT_SOURCE=api` to bypass the cache.

### Parity check (cache vs API)

| source | bars | trades | win% | net P&L | return | Sharpe | runtime |
|---|--:|--:|--:|--:|--:|--:|--:|
| DuckDB cache | 13,440 | 127 | 37.8% | -84,563 | -4.23% | -6.93 | 8 s |
| OpenAlgo API | 13,439 | 127 | 37.8% | -84,687 | -4.23% | -6.94 | 28 s |

Same trades, same win rate, P&L within Rs 124 (0.15%) - and 3.5x faster, no rate limit.

### Resampling convention (important)

The broker's intraday bars are **left-labelled / closed-left**: a 09:15 bar covers
09:15-09:19. Resampling with the rule file's `label="right", closed="right"` would
shift every bar by one interval and silently desynchronise signals. Verified by
resampling 1m->5m and comparing against the broker's own 5m: **`open` matched on
13,439/13,439 bars.**

### Data-quality finding: ~1% of broker 5m bars are corrupt

While validating, 150 of 13,439 5m bars disagreed with the 1m-derived values -
**149 of them at 15:25**, the session's last bar. Worst case 2025-12-19: the 1m bars
close at 25,961 while the API's 5m bar reports **23,970** - a 2026 price level on a
2025 timestamp, i.e. a corrupt tail row in the 5m series.

The 1m-derived series is therefore more trustworthy. These bars sit outside the
09:45-14:30 entry window and after the 15:15 exit, so they do not affect the result
above - but **any strategy trading or exiting near the close should use 1m data,
not the broker's 5m.**
