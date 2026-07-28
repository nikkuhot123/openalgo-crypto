"""
Ingest OpenAlgo history into a local DuckDB in **OpenAlgo Historify format**.

Why: the OpenAlgo API is rate-limited (~3 req/s) and the VPS Historify DB is empty
(schema only, 0 rows in market_data as of 2026-07-28). This builds the equivalent
locally so backtests read from disk with no rate limit, work offline, and are
byte-stable across re-runs.

Schema matches Historify exactly, so the skill pack's `load_from_historify()` and
`HISTORIFY_DB_PATH` work unchanged:

    market_data(symbol, exchange, interval, timestamp BIGINT epoch-seconds,
                open, high, low, close, volume, oi, created_at)
    PRIMARY KEY (symbol, exchange, interval, timestamp)

Only `1m` and `D` are stored (Historify convention); every other timeframe is
resampled from 1m at read time.

Idempotent — re-run any time to top up. Usage:
    ./venv/Scripts/python.exe backtesting/ingest_duckdb.py
    ./venv/Scripts/python.exe backtesting/ingest_duckdb.py --days 400
"""

import argparse
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from openalgo import api

load_dotenv(find_dotenv(), override=False)

# (symbol, exchange) pairs to cache — extend freely
SYMBOLS = [
    ("NIFTY", "NSE_INDEX"),
    ("SENSEX", "BSE_INDEX"),
]
INTERVALS = ["1m", "D"]
DB_PATH = Path(__file__).resolve().parent / "data" / "market_cache.duckdb"

DDL = """
CREATE TABLE IF NOT EXISTS market_data (
    symbol VARCHAR NOT NULL,
    exchange VARCHAR NOT NULL,
    interval VARCHAR NOT NULL,
    timestamp BIGINT NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume BIGINT NOT NULL,
    oi BIGINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, exchange, interval, timestamp)
)
"""


def to_rows(df, symbol, exchange, interval):
    d = df.copy()
    d.columns = [c.lower() for c in d.columns]
    for c in ("open", "high", "low", "close"):
        d[c] = d[c].astype(float)
    d["volume"] = d.get("volume", 0)
    d["oi"] = d.get("oi", 0)
    idx = pd.to_datetime(d.index)
    # Naive timestamps from the API are IST.
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Kolkata")
    # Epoch SECONDS, unit-explicit. pandas >=2 can hand back datetime64[s] (not
    # [ns]), so a bare astype('int64')//1e9 silently floors everything to ~0.
    epoch_s = idx.tz_convert("UTC").as_unit("s").astype("int64").to_numpy()
    out = pd.DataFrame({
        "symbol": symbol, "exchange": exchange, "interval": interval,
        "timestamp": epoch_s,
        "open": d["open"].values, "high": d["high"].values,
        "low": d["low"].values, "close": d["close"].values,
        "volume": pd.to_numeric(d["volume"], errors="coerce").fillna(0).astype("int64").values,
        "oi": pd.to_numeric(d["oi"], errors="coerce").fillna(0).astype("int64").values,
    })
    out = out.dropna(subset=["open", "high", "low", "close"])
    # the API can return repeated bars (e.g. a duplicated 09:14 open bar); the
    # Historify primary key forbids them, so keep the last observation per key
    return out.drop_duplicates(subset=["symbol", "exchange", "interval", "timestamp"],
                               keep="last").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="lookback window in days")
    args = ap.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = api(api_key=os.getenv("OPENALGO_API_KEY"),
                 host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))
    end = datetime.now().date()
    start = end - timedelta(days=args.days)

    con = duckdb.connect(str(DB_PATH))
    con.execute(DDL)

    print(f"db     : {DB_PATH}")
    print(f"window : {start} .. {end}\n")
    for symbol, exchange in SYMBOLS:
        for interval in INTERVALS:
            try:
                df = client.history(symbol=symbol, exchange=exchange, interval=interval,
                                    start_date=start.strftime("%Y-%m-%d"),
                                    end_date=end.strftime("%Y-%m-%d"))
                if not isinstance(df, pd.DataFrame) or df.empty:
                    print(f"  {symbol:7s} {exchange:10s} {interval:3s}  no data")
                    continue
                rows = to_rows(df, symbol, exchange, interval)
                con.register("incoming", rows)
                before = con.execute(
                    "SELECT count(*) FROM market_data WHERE symbol=? AND exchange=? AND interval=?",
                    [symbol, exchange, interval]).fetchone()[0]
                # upsert: replace overlapping keys, keep everything else
                con.execute("""
                    DELETE FROM market_data
                    WHERE (symbol, exchange, interval, timestamp) IN
                          (SELECT symbol, exchange, interval, timestamp FROM incoming)
                """)
                con.execute("""
                    INSERT INTO market_data
                        (symbol, exchange, interval, timestamp, open, high, low, close, volume, oi)
                    SELECT symbol, exchange, interval, timestamp, open, high, low, close, volume, oi
                    FROM incoming
                """)
                con.unregister("incoming")
                after = con.execute(
                    "SELECT count(*) FROM market_data WHERE symbol=? AND exchange=? AND interval=?",
                    [symbol, exchange, interval]).fetchone()[0]
                lo, hi = con.execute(
                    "SELECT min(timestamp), max(timestamp) FROM market_data "
                    "WHERE symbol=? AND exchange=? AND interval=?",
                    [symbol, exchange, interval]).fetchone()
                span = (f"{pd.to_datetime(lo, unit='s', utc=True).tz_convert('Asia/Kolkata'):%Y-%m-%d}"
                        f" .. {pd.to_datetime(hi, unit='s', utc=True).tz_convert('Asia/Kolkata'):%Y-%m-%d}")
                print(f"  {symbol:7s} {exchange:10s} {interval:3s}  fetched {len(rows):>6}  "
                      f"stored {after:>6} (+{after-before})  {span}")
                time.sleep(0.4)  # respect ~3 req/s
            except Exception as e:
                print(f"  {symbol:7s} {exchange:10s} {interval:3s}  FAILED {type(e).__name__}: {str(e)[:90]}")

    total = con.execute("SELECT count(*) FROM market_data").fetchone()[0]
    print(f"\ntotal rows: {total:,}")
    print("\nset in .env:  HISTORIFY_DB_PATH=" + str(DB_PATH))
    con.close()


if __name__ == "__main__":
    main()
