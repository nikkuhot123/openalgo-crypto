#!/usr/bin/env python
"""Backfill the strategy_trades archive from order_logs.

Strategy Performance & History only ever had a same-session data source in
live mode (the broker tradebook), so everything older than today was missing.
`strategy_trades` fixes that going forward; this recovers the past.

SOURCE
    order_logs rows with api_type='orderstatus'. The request carries the
    strategy tag and the response carries the completed fill:

      REQ  {"strategy": "Judas Swing", "orderid": "26080700064070"}
      RESP {"status":"success","data":{"symbol":"NIFTY11AUG2624600PE",
            "exchange":"NFO","action":"SELL","quantity":"65",
            "order_status":"complete","timestamp":"10:18:45 07-08-2026",
            "average_price":127.8}}

    That is a complete, strategy-attributed fill record -- better than the
    tradebook, which carries no strategy tag at all.

    Only order_status == 'complete' rows are taken; a rejected or pending
    order is not a fill. placeorder rows are ignored: they predate the fill
    and carry no average price.

Idempotent: archive_trades() is unique on (orderid, action, qty, price), so
running this twice adds nothing the second time.

Usage:
    .venv/bin/python upgrade/backfill_strategy_trades.py [--dry-run]
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "db" / "openalgo.db"
TS_FORMATS = ("%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_ts(raw, fallback):
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(str(raw).strip(), fmt)
        except (ValueError, TypeError):
            continue
    try:                                    # created_at, e.g. 2026-08-07 10:18:45.123
        return datetime.fromisoformat(str(fallback).split(".")[0])
    except (ValueError, TypeError):
        return None


def collect():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select request_data, response_data, created_at from order_logs "
        "where api_type='orderstatus' order by created_at"
    ).fetchall()
    con.close()

    out, skipped = [], {"unparseable": 0, "not_complete": 0, "no_price": 0}
    for req_raw, resp_raw, created in rows:
        try:
            req = json.loads(req_raw) if req_raw else {}
            resp = json.loads(resp_raw) if resp_raw else {}
        except (json.JSONDecodeError, TypeError):
            skipped["unparseable"] += 1
            continue
        d = resp.get("data") or {}
        if not d or resp.get("status") != "success":
            skipped["unparseable"] += 1
            continue
        if str(d.get("order_status", "")).lower() != "complete":
            skipped["not_complete"] += 1
            continue
        price = d.get("average_price") or d.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            skipped["no_price"] += 1
            continue
        ts = parse_ts(d.get("timestamp"), created)
        if ts is None:
            skipped["unparseable"] += 1
            continue
        out.append({
            "ts": ts,
            "orderid": str(d.get("orderid") or req.get("orderid") or ""),
            "strategy": req.get("strategy") or None,
            "symbol": d.get("symbol") or "",
            "exchange": d.get("exchange") or None,
            "action": str(d.get("action") or "").upper(),
            "qty": d.get("quantity") or 0,
            "price": price,
            "source": "backfill",
        })
    return out, skipped


def main():
    dry = "--dry-run" in sys.argv
    fills, skipped = collect()
    print(f"order_logs orderstatus rows -> {len(fills)} completed fills")
    print(f"  skipped: {skipped}")
    if fills:
        lo, hi = fills[0]["ts"], fills[-1]["ts"]
        print(f"  span: {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}")
        by_strat = {}
        for f in fills:
            by_strat[f["strategy"] or "(untagged)"] = by_strat.get(f["strategy"] or "(untagged)", 0) + 1
        for k, v in sorted(by_strat.items(), key=lambda kv: -kv[1]):
            print(f"    {k:28s} {v:4d} fills")

    if dry:
        print("\n--dry-run: nothing written")
        return 0

    from database.strategy_trades_db import archive_span, archive_trades
    added = archive_trades(fills)
    lo, hi, n = archive_span()
    print(f"\narchived {added} new fills")
    print(f"archive now holds {n} fills"
          + (f" spanning {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}" if n else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
