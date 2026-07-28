"""
Ingest clean index bars from harvest_state.db into the Historify-format DuckDB cache.

Why this exists
---------------
`harvest_state.db` (5.4 GB) holds a `historical_bars` table with 3.1 years of
index OHLC - far more history than either alternative:

    Volrix free plan : 125 trading days (6 months, gated)
    OpenAlgo broker  : ~400 calendar days, and our cache had a 4-month hole
    harvest_state.db : 778 trading days, 5 symbols          <- this

Verified before use, not assumed:
  - median exactly 75 bars/day on 5m (a complete NSE session), 759/778 days full
  - 24 out-of-hours rows in 665,137 (negligible)
  - parity vs the broker feed over Nov-2025: close 98.9% exact, mean diff
    0.0067 points on ~25,500 (0.00003%)

What it does NOT provide: option premiums. The companion
`harvest_options_archive.db` is corrupt beyond rowid ~27.2M and its readable
portion only covers 2026-02..2026-05 with impossible rows (options expiring
2026-06 carrying bars dated 2023, timestamps at 18:25). Do not build on it.

Usage:
    ../venv/Scripts/python.exe backtesting/ingest_harvest.py [--db harvest_state.db]
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

IST = ZoneInfo("Asia/Kolkata")
CACHE = Path(__file__).resolve().parent / "data" / "market_cache.duckdb"

# harvest interval label -> cache interval label
INTERVALS = {"minute": "1m", "5minute": "5m"}
# symbol -> exchange, matching the OpenAlgo/Historify convention already in the cache
EXCHANGES = {
    "NIFTY": "NSE_INDEX",
    "BANKNIFTY": "NSE_INDEX",
    "FINNIFTY": "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "SENSEX": "BSE_INDEX",
}

DDL = """
CREATE TABLE IF NOT EXISTS market_data (
    symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL, interval VARCHAR NOT NULL,
    timestamp BIGINT NOT NULL,
    open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
    volume BIGINT NOT NULL, oi BIGINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, exchange, interval, timestamp)
);
"""


def to_epoch(ts: str) -> int | None:
    """'YYYY-MM-DD HH:MM:SS[+05:30]' (IST wall time) -> epoch seconds."""
    s = ts.strip()[:19].replace("T", " ")
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=IST).timestamp())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="harvest_state.db")
    ap.add_argument("--cache", default=str(CACHE))
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con = duckdb.connect(args.cache)
    con.execute(DDL)

    grand = 0
    for h_iv, c_iv in INTERVALS.items():
        for sym, exch in EXCHANGES.items():
            rows = src.execute(
                """SELECT timestamp, open, high, low, close, volume, oi
                   FROM historical_bars WHERE symbol=? AND interval=?""",
                (sym, h_iv),
            ).fetchall()
            if not rows:
                print(f"  {sym:11s} {c_iv:3s} - none")
                continue

            out = []
            skipped = 0
            for ts, o, hi, lo, cl, vol, oi in rows:
                ep = to_epoch(ts)
                # drop unparseable stamps and out-of-session rows (the DB has 24)
                if ep is None or not (o and hi and lo and cl):
                    skipped += 1
                    continue
                hhmm = ts[11:16]
                if hhmm < "09:15" or hhmm > "15:30":
                    skipped += 1
                    continue
                out.append((sym, exch, c_iv, ep, float(o), float(hi), float(lo),
                            float(cl), int(vol or 0), int(oi or 0)))
            if not out:
                continue

            con.execute("CREATE OR REPLACE TEMP TABLE _stage AS SELECT * FROM market_data LIMIT 0")
            con.executemany(
                """INSERT INTO _stage
                   (symbol, exchange, interval, timestamp, open, high, low, close, volume, oi)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", out)
            # idempotent: never overwrite an existing (symbol,exchange,interval,timestamp)
            n = con.execute("""
                INSERT INTO market_data
                    (symbol, exchange, interval, timestamp, open, high, low, close, volume, oi)
                SELECT s.symbol, s.exchange, s.interval, s.timestamp,
                       s.open, s.high, s.low, s.close, s.volume, s.oi
                FROM (SELECT DISTINCT ON (symbol, exchange, interval, timestamp) *
                      FROM _stage) s
                LEFT JOIN market_data m USING (symbol, exchange, interval, timestamp)
                WHERE m.timestamp IS NULL
                RETURNING 1""").fetchall()
            grand += len(n)
            span = con.execute("""
                SELECT strftime(MIN(to_timestamp(timestamp)::TIMESTAMP), '%Y-%m-%d'),
                       strftime(MAX(to_timestamp(timestamp)::TIMESTAMP), '%Y-%m-%d'), COUNT(*)
                FROM market_data WHERE symbol=? AND interval=?""", (sym, c_iv)).fetchone()
            print(f"  {sym:11s} {c_iv:3s} +{len(n):>7,} new (skipped {skipped:>3}) "
                  f"-> cache now {span[2]:>7,} rows {span[0]}..{span[1]}")

    print(f"\ntotal inserted: {grand:,}")
    print("cache contents:")
    for r in con.execute("""
        SELECT symbol, interval, COUNT(*) n,
               strftime(MIN(to_timestamp(timestamp)::TIMESTAMP), '%Y-%m-%d') lo,
               strftime(MAX(to_timestamp(timestamp)::TIMESTAMP), '%Y-%m-%d') hi,
               COUNT(DISTINCT strftime(to_timestamp(timestamp)::TIMESTAMP, '%Y-%m-%d')) AS "days"
        FROM market_data GROUP BY 1,2 ORDER BY n DESC""").fetchall():
        print(f"   {r[0]:11s} {r[1]:3s} {r[2]:>8,} rows  {r[3]}..{r[4]}  {r[5]:>4} days")
    con.close()
    src.close()


if __name__ == "__main__":
    main()
