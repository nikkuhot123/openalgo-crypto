"""
Judas Swing — did the stop-loss give back real, reachable profit?
=================================================================
The claim: trades run up ~20%, then reverse into a stop-out.

For every LIVE Judas trade in the logs, pull the option's own 1-minute series
and measure what the position was actually worth minute by minute between
entry and exit. Then ask what a premium-based exit would have banked instead.

Target and SL are defined in SPOT points, but P&L accrues in OPTION premium.
Those two are not the same distance, and that gap is what this measures.
"""
import re
import subprocess
import sys

import pandas as pd
from openalgo import api

API_KEY = "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a"
HOST = "https://openalgo.inikhilesh.com"
VPS = "ubuntu@80.225.250.15"
KEY = "~/.ssh/vps_deploy_key"

client = api(api_key=API_KEY, host=HOST)

ENTRY_RE = re.compile(
    r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ \[INFO\] Entered Trade! "
    r"Spot Entry: ([\d.]+) \| SL: ([\d.]+) \| Target: ([\d.]+) \| Opt entry: ([\d.]+)"
)
EXIT_RE = re.compile(
    r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ \[INFO\] !!! (Stop-Loss Hit|Target Hit|EOD Squareoff[^!]*) !!! "
    r"Closing position on (\S+?)\.\.\."
)
PNL_RE = re.compile(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ \[INFO\] Trade P&L: ₹([-+]?[\d.]+)")


def fetch_logs():
    out = subprocess.run(
        ["ssh", "-i", KEY, VPS,
         "cat /opt/openalgo/log/strategies/judas_swing_strategy*2026*.log"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return out.stdout


def parse(text):
    """Pair entries with the following exit + P&L line."""
    events = []
    for ln in text.splitlines():
        for rx, kind in ((ENTRY_RE, "entry"), (EXIT_RE, "exit"), (PNL_RE, "pnl")):
            m = rx.search(ln)
            if m:
                events.append((kind, m))
                break

    trades, open_t = [], None
    for kind, m in events:
        if kind == "entry":
            open_t = {
                "date": m.group(1), "t_in": m.group(2),
                "spot_in": float(m.group(3)), "spot_sl": float(m.group(4)),
                "spot_tgt": float(m.group(5)), "opt_in": float(m.group(6)),
            }
        elif kind == "exit" and open_t:
            open_t.update(t_out=m.group(2), reason=m.group(3).strip(), symbol=m.group(4))
        elif kind == "pnl" and open_t and "symbol" in open_t:
            open_t["pnl"] = float(m.group(3))
            trades.append(open_t)
            open_t = None
    return trades


def qty_for(sym):
    return 20 if sym.startswith("SENSEX") else 65


def exch_for(sym):
    return "BFO" if sym.startswith("SENSEX") else "NFO"


def main():
    trades = parse(fetch_logs())
    if not trades:
        print("No completed trades parsed.")
        return

    print("=" * 118)
    print(" LIVE JUDAS TRADES — premium peak vs what was actually realised")
    print("=" * 118)
    print(f"{'date':11s} {'symbol':22s} {'in':>7s} {'entry':>8s} {'PEAK':>8s} {'peak%':>7s} "
          f"{'peak Rs':>9s} {'exit':>8s} {'realised':>9s} {'reason':>14s}")
    print("-" * 118)

    tot_pnl = tot_peak = 0.0
    rows = []
    for t in trades:
        sym, q = t["symbol"], qty_for(t["symbol"])
        nxt = (pd.Timestamp(t["date"]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            df = client.history(symbol=sym, exchange=exch_for(sym), interval="1m",
                                start_date=t["date"], end_date=nxt)
        except Exception:
            df = None

        peak = peak_pct = peak_rs = None
        if isinstance(df, pd.DataFrame) and not df.empty:
            df = df.copy()
            df["hm"] = df.index.strftime("%H:%M")
            w = df[(df["hm"] >= t["t_in"][:5]) & (df["hm"] <= t["t_out"][:5])]
            if not w.empty:
                peak = float(w["high"].max())
                peak_pct = (peak - t["opt_in"]) / t["opt_in"] * 100
                peak_rs = (peak - t["opt_in"]) * q

        tot_pnl += t["pnl"]
        if peak_rs and peak_rs > 0:
            tot_peak += peak_rs

        rows.append({**t, "peak": peak, "peak_pct": peak_pct, "peak_rs": peak_rs, "qty": q})
        pk = f"{peak:8.2f}" if peak else "     n/a"
        pp = f"{peak_pct:+6.1f}%" if peak_pct is not None else "    n/a"
        pr = f"{peak_rs:+9,.0f}" if peak_rs is not None else "      n/a"
        print(f"{t['date']:11s} {sym:22s} {t['t_in'][:5]:>7s} {t['opt_in']:8.2f} {pk} {pp} "
              f"{pr} {t['t_out'][:5]:>8s} {t['pnl']:+9,.0f} {t['reason'][:14]:>14s}")

    print("-" * 118)
    print(f"{'TOTAL':11s} {len(trades)} trades{'':32s}{tot_peak:+9,.0f}{'':9s}{tot_pnl:+9,.0f}")

    print()
    print("=" * 118)
    print(" WHAT A PREMIUM-BASED EXIT WOULD HAVE BANKED INSTEAD")
    print(" (exit the moment the option premium gains X%, else keep the actual outcome)")
    print("=" * 118)
    for thr in (5, 8, 10, 12, 15, 20):
        total = 0.0
        hits = 0
        for r in rows:
            if r["peak_pct"] is not None and r["peak_pct"] >= thr:
                total += r["opt_in"] * thr / 100 * r["qty"]
                hits += 1
            else:
                total += r["pnl"]
        delta = total - tot_pnl
        print(f"  exit at +{thr:2d}% premium : {hits}/{len(rows)} trades would trigger | "
              f"total Rs {total:+9,.0f}  (vs actual Rs {tot_pnl:+,.0f}, delta Rs {delta:+,.0f})")

    print()
    print("=" * 118)
    print(" WHY THE SPOT STOP KEEPS GETTING HIT")
    print("=" * 118)
    for r in rows:
        sl_pts = abs(r["spot_in"] - r["spot_sl"])
        tgt_pts = abs(r["spot_tgt"] - r["spot_in"])
        print(f"  {r['date']} {r['symbol']:22s} SL {sl_pts:7.1f} pts ({sl_pts / r['spot_in'] * 10000:5.1f} bps of spot) | "
              f"target {tgt_pts:7.1f} pts | held {r['t_in'][:5]}-{r['t_out'][:5]}")


if __name__ == "__main__":
    main()
