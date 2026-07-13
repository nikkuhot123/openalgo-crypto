#!/usr/bin/env python3
"""POV MFE study — daily capture, cumulative report (read-only).

For every POV round-trip, reconstruct the option's post-entry 1m premium path
and record: MFE (max favorable excursion), whether T1/T2/T3 were reached, the
actual P&L, and a counterfactual "half at T1 + breakeven floor + 20% peak
trail" P&L. Results accumulate in a JSONL store so trades stay measurable
after their contracts expire (weekly options lose history at expiry — this is
why the study MUST run daily, not weekly).

Run daily after close (cron 15:50 IST Mon-Fri):
  cd /opt/openalgo && .venv/bin/python3 strategies/analysis/pov_mfe_study.py

Decision context (2026-07-13): trail deferred — winner-side evidence was a
wash on n=14; re-decide once the MIN_SCORE=5 gated population accumulates.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, "/opt/openalgo")

import requests

from services.strategy_metrics_service import _fifo_round_trips, _sandbox_trade_rows

API_KEY = os.environ.get(
    "OPENALGO_API_KEY",
    "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a",
)
HOST = os.environ.get("HOST_SERVER", "http://127.0.0.1:5000")
TRAIL_PCT = 0.20
EOD = dtime(15, 14)

STORE_DIR = Path("/opt/openalgo/log/strategies/mfe_study")
STORE_DIR.mkdir(parents=True, exist_ok=True)
STORE = STORE_DIR / "results.jsonl"

LOG_GLOB = "/opt/openalgo/log/strategies/pov_wall_squeeze_*.log"
ENTRY_PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ \[INFO\] Trade entered: (\S+) \| SL: ([\d.]+) \| "
    r"T1: ([\d.]+) \| T2: ([\d.]+) \| T3: ([\d.]+) \| Opt entry: ([\d.]+)"
)


def load_store():
    """Previously captured results, keyed by (day, symbol, entry_ts)."""
    seen, rows = set(), []
    if STORE.exists():
        for line in STORE.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            seen.add((r["day"], r["symbol"], r["entry_ts"]))
            rows.append(r)
    return seen, rows


def parse_entry_levels():
    """(symbol, date) -> {sl, t1, t2, t3, entry} from strategy logs."""
    levels = {}
    for lf in glob.glob(LOG_GLOB):
        try:
            with open(lf, errors="ignore") as f:
                for line in f:
                    m = ENTRY_PAT.match(line)
                    if m:
                        d, sym, sl, t1, t2, t3, e = m.groups()
                        levels[(sym, d)] = {
                            "sl": float(sl), "t1": float(t1), "t2": float(t2),
                            "t3": float(t3), "entry": float(e),
                        }
        except OSError:
            pass
    return levels


_hist_cache = {}


def fetch_1m(symbol, day):
    key = (symbol, day)
    if key in _hist_cache:
        return _hist_cache[key]
    exchange = "BFO" if symbol.startswith("SENSEX") else "NFO"
    try:
        r = requests.post(f"{HOST}/api/v1/history", json={
            "apikey": API_KEY, "symbol": symbol, "exchange": exchange,
            "interval": "1m", "start_date": day, "end_date": day,
        }, timeout=15)
        candles = r.json().get("data") or []
    except Exception:
        candles = []
    out = []
    for c in candles:
        ts = c.get("timestamp") or c.get("time")
        try:
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(int(ts))
            else:
                dt = datetime.fromisoformat(str(ts).replace("+05:30", ""))
        except (ValueError, OSError):
            continue
        out.append((dt, float(c.get("high", 0) or 0), float(c.get("close", 0) or 0)))
    out.sort()
    _hist_cache[key] = out
    return out


def simulate(rt, lv):
    """One round-trip -> result row, or None if the premium path is unavailable."""
    entry_ts = rt.get("entry_ts")
    if entry_ts is None:
        return None
    candles = fetch_1m(rt["symbol"], rt["day"])
    path = [(dt, hi, cl) for dt, hi, cl in candles
            if dt >= entry_ts and dt.time() <= EOD]
    if not path:
        return None

    entry, t1 = lv["entry"], lv["t1"]
    qty = rt["qty"]
    mfe = max(hi for _, hi, _ in path)
    actual_pnl = rt["pnl"]

    t1_idx = next((i for i, (_, _, cl) in enumerate(path) if cl >= t1), None)
    if t1_idx is None:
        cf_pnl = actual_pnl  # never crossed T1 -> policies identical
        hit_t1 = False
    else:
        hit_t1 = True
        half = qty // 2
        rest = qty - half
        pnl_half = (t1 - entry) * half
        peak = path[t1_idx][2]
        exit_px = path[-1][2]
        for _, _, cl in path[t1_idx + 1:]:
            peak = max(peak, cl)
            if cl <= max(entry, peak * (1 - TRAIL_PCT)):
                exit_px = cl
                break
        cf_pnl = pnl_half + (exit_px - entry) * rest

    return {
        "day": rt["day"], "symbol": rt["symbol"],
        "entry_ts": entry_ts.isoformat(), "qty": qty,
        "entry": entry, "t1": t1,
        "mfe": round(mfe, 2), "mfe_pct": round(100 * (mfe / entry - 1), 1),
        "hit_t1": hit_t1, "hit_t2": mfe >= lv["t2"], "hit_t3": mfe >= lv["t3"],
        "actual": round(actual_pnl, 0), "counterfactual": round(cf_pnl, 0),
        "captured": datetime.now().isoformat(timespec="seconds"),
    }


def main():
    seen, stored = load_store()
    levels = parse_entry_levels()

    rows = _sandbox_trade_rows("nikhil", None)
    round_trips, _ = _fifo_round_trips(rows)
    pov = [rt for rt in round_trips if rt["opener"] == "POV Wall-Squeeze"]

    new, skipped_levels, skipped_hist = [], 0, 0
    for rt in pov:
        key = (rt["day"], rt["symbol"],
               rt["entry_ts"].isoformat() if rt.get("entry_ts") else "")
        if key in seen:
            continue
        lv = levels.get((rt["symbol"], rt["day"]))
        if not lv:
            skipped_levels += 1
            continue
        res = simulate(rt, lv)
        if res is None:
            skipped_hist += 1
            continue
        new.append(res)

    if new:
        with STORE.open("a") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")

    allr = stored + new
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] POV round-trips: {len(pov)} | "
          f"captured now: {len(new)} | store total: {len(allr)} | "
          f"skipped (no levels): {skipped_levels}, (no history): {skipped_hist}")
    if not allr:
        return

    print(f"\n{'day':<11}{'symbol':<24}{'entry':>8}{'T1':>8}{'MFE':>8}{'MFE%':>7}"
          f"{'T2?':>5}{'T3?':>5}{'actual':>9}{'trail-cf':>9}")
    for r in sorted(allr, key=lambda x: (x["day"], x["entry_ts"]))[-30:]:
        print(f"{r['day']:<11}{r['symbol']:<24}{r['entry']:>8.1f}{r['t1']:>8.1f}"
              f"{r['mfe']:>8.1f}{r['mfe_pct']:>6.0f}%"
              f"{'Y' if r['hit_t2'] else '-':>5}{'Y' if r['hit_t3'] else '-':>5}"
              f"{r['actual']:>9.0f}{r['counterfactual']:>9.0f}")

    n = len(allr)
    hitters = [r for r in allr if r["hit_t1"]]
    a_sum = sum(r["actual"] for r in allr)
    c_sum = sum(r["counterfactual"] for r in allr)
    print(f"\nCUMULATIVE: {n} trades | hit T1: {len(hitters)} "
          f"({100 * len(hitters) / n:.0f}%)")
    if hitters:
        t2 = sum(1 for r in hitters if r["hit_t2"])
        t3 = sum(1 for r in hitters if r["hit_t3"])
        mfes = sorted(r["mfe_pct"] for r in hitters)
        print(f"Of T1-hitters: T2 {t2}/{len(hitters)}, T3 {t3}/{len(hitters)} | "
              f"MFE% median {mfes[len(mfes) // 2]:.0f}%, max {mfes[-1]:.0f}%")
        # Winner-side comparison: only trades where BOTH policies acted (hit T1)
        wa = sum(r["actual"] for r in hitters)
        wc = sum(r["counterfactual"] for r in hitters)
        print(f"T1-hitters only: actual {wa:+.0f} vs trail-cf {wc:+.0f} "
              f"(delta {wc - wa:+.0f})")
    print(f"ALL trades: actual {a_sum:+.0f} vs trail-cf {c_sum:+.0f} "
          f"(delta {c_sum - a_sum:+.0f})")


if __name__ == "__main__":
    main()
