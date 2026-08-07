"""
What does execution actually cost, and would limit entries be worth it?
=======================================================================
Every strategy here fires MARKET orders. Red Bar logged the first hard number
on 2026-08-07:

    Entry fill 114.7 vs quote 114.4 -> slippage +26 bps

At a 110 premium on a 65 lot that is ~Rs 21 a trade, against a friction hurdle
of ~1.9 index points. Roughly half the wall may be slippage rather than
statutory cost -- and unlike the statutory part, slippage is addressable.

Two questions, in order:
  1. What IS the slippage? Measured as logged quote vs broker fill.
  2. Would a limit entry pay? A limit saves the slippage on trades that fill
     and forfeits the trades that do not. So the honest form of the question is
     a BREAK-EVEN MISS RATE: above what miss rate does limiting lose money?
     That depends on the strategy's expectancy, which is computed here from
     real fills rather than assumed.

MEASUREMENT CAVEAT, and it is the reason this file exists rather than a guess:
POV overwrites its quote with the fill (`entry_opt_price = _fill_entry`), so
its logged entry IS the fill and its apparent zero slippage is an artifact, not
a measurement. Only Judas and Red Bar keep the pre-order quote. POV is measured
instead against the 1-minute bar it executed in, for contracts that have not
expired.

All rupee results are stated on the standard Rs 2,00,000 research notional
(backtesting/config.py), never the live balance.

Usage:
    ./venv/Scripts/python.exe backtesting/slippage_study.py
"""
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from config import BACKTEST_CAPITAL, LOT, OPT_COST_PCT  # noqa: E402

VPS = "ubuntu@80.225.250.15"
KEY = "~/.ssh/vps_deploy_key"
FILLS = HERE / "fills_all.csv"

# "... Opt entry: 127.0"  /  "Trade entered: SYM | SL: .. | Opt entry: 79.1"
RE_QUOTE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
                      r"(?:Entered Trade!|Trade entered:)\s*(\S+)?.*?Opt entry:\s*([\d.]+)")


def client():
    from openalgo import api

    env = (ROOT / ".env").read_text()
    return api(api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
               host=env.split("OPENALGO_HOST=")[1].split()[0])


def pull_quotes():
    cmd = (f'ssh -i {KEY} -o StrictHostKeyChecking=no -o BatchMode=yes {VPS} '
           f'"cd /opt/openalgo/log/strategies && grep -hE \'Opt entry:\' *.log 2>/dev/null"')
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    rows = []
    for line in out.stdout.splitlines():
        m = RE_QUOTE.match(line.replace(",", " ", 1) if "," in line[:24] else line)
        if m:
            rows.append({"ts": pd.Timestamp(m.group(1)), "quote": float(m.group(3))})
    return pd.DataFrame(rows)


def main():
    fills = pd.read_csv(FILLS, parse_dates=["ts"])
    quotes = pull_quotes()
    print(f"{len(fills)} attributed fills | {len(quotes)} logged pre-order quotes\n")

    # ---- 1. slippage: logged quote vs broker fill, BUY side ----------------
    buys = fills[fills["action"] == "BUY"].copy()
    merged = pd.merge_asof(
        buys.sort_values("ts"), quotes.sort_values("ts"),
        on="ts", tolerance=pd.Timedelta("90s"), direction="nearest",
    ).dropna(subset=["quote"])
    # POV logs the FILL as its quote -- excluded, it cannot measure itself
    honest = merged[~merged["strategy"].str.contains("POV", na=False)].copy()
    honest["bps"] = (honest["fill"] - honest["quote"]) / honest["quote"] * 10000

    print("ENTRY SLIPPAGE (quote -> fill, market BUY)")
    print(f"{'when':17s} {'strategy':17s} {'symbol':22s} {'quote':>8s} {'fill':>8s} {'bps':>7s}")
    for _, r in honest.iterrows():
        print(f"{r['ts']:%Y-%m-%d %H:%M} {r['strategy'][:17]:17s} {r['symbol'][:22]:22s} "
              f"{r['quote']:>8.2f} {r['fill']:>8.2f} {r['bps']:>+7.0f}")
    if honest.empty:
        print("  none measurable")
        return 1
    bps = honest["bps"]
    print(f"\n  n={len(bps)}  mean {bps.mean():+.0f} bps  median {bps.median():+.0f} bps  "
          f"worst {bps.max():+.0f} bps")
    print(f"  excluded {len(merged) - len(honest)} POV fills: it overwrites the quote "
          f"with the fill\n  (entry_opt_price = _fill_entry) so its slippage reads as "
          f"exactly zero by construction.")

    # ---- 2. expectancy from real round trips -------------------------------
    trips = []
    for (strat, sym), g in fills.groupby(["strategy", "symbol"]):
        g = g.sort_values("ts")
        stack = []
        for _, r in g.iterrows():
            if r["action"] == "BUY":
                stack.append(r)
            elif stack:
                b = stack.pop(0)
                lot = LOT.get("SENSEX" if "SENSEX" in sym else "NIFTY", 65)
                gross = (r["fill"] - b["fill"]) * lot
                cost = (b["fill"] + r["fill"]) * lot * OPT_COST_PCT / 100.0
                trips.append({"strategy": strat, "symbol": sym, "entry_ts": b["ts"],
                              "entry": b["fill"], "exit": r["fill"], "lot": lot,
                              "net": gross - cost})
    t = pd.DataFrame(trips)
    print(f"\n\nREALISED ROUND TRIPS ({len(t)}), net of statutory charges")
    for strat, g in t.groupby("strategy"):
        print(f"  {strat:18s} n={len(g):3d}  total Rs{g['net'].sum():+9,.0f}  "
              f"avg Rs{g['net'].mean():+8,.0f}  win {100*(g['net']>0).mean():4.0f}%")
    print(f"  {'ALL':18s} n={len(t):3d}  total Rs{t['net'].sum():+9,.0f}  "
          f"avg Rs{t['net'].mean():+8,.0f}  win {100*(t['net']>0).mean():4.0f}%")

    # ---- 3. would a limit entry pay? ---------------------------------------
    print("\n\nLIMIT-ENTRY BREAK-EVEN")
    print("A limit at mid saves the slippage on trades that fill, and forfeits the")
    print("trades that do not. Break-even miss rate = saving / expectancy.\n")
    slip = bps.mean() / 10000.0
    print(f"{'strategy':18s} {'avg prem':>9s} {'saving/trade':>13s} {'expectancy':>11s} "
          f"{'break-even miss':>16s}")
    for strat, g in list(t.groupby("strategy")) + [("ALL", t)]:
        prem, lot = g["entry"].mean(), g["lot"].mean()
        saving = slip * prem * lot
        ev = g["net"].mean()
        if ev <= 0:
            verdict = "n/a - EV<=0"
        else:
            verdict = f"{100 * saving / ev:.0f}%"
        print(f"{strat[:18]:18s} {prem:>9.2f} {saving:>13,.0f} {ev:>+11,.0f} {verdict:>16s}")

    print("\n  Read it as: if a limit entry misses MORE than that share of trades,")
    print("  it costs more in forgone trades than it saves in slippage.")
    print("  Where expectancy is <= 0 the comparison is meaningless -- fix the")
    print("  strategy before optimising its execution.")

    # ---- 4. scale to the research notional ---------------------------------
    print(f"\n\nON THE STANDARD Rs {BACKTEST_CAPITAL:,} RESEARCH NOTIONAL")
    prem = t["entry"].mean()
    per_lot = prem * 65
    lots = int((BACKTEST_CAPITAL * 0.5) // per_lot)
    print(f"  avg premium Rs{prem:.2f} x 65 = Rs{per_lot:,.0f} per lot")
    print(f"  deploying at most 50% of capital -> {lots} lots per position")
    print(f"  realised book at 1 lot: Rs{t['net'].sum():+,.0f} over {len(t)} trades")
    print(f"  same book at {lots} lots:    Rs{t['net'].sum() * lots:+,.0f} "
          f"({t['net'].sum() * lots / BACKTEST_CAPITAL * 100:+.1f}% on notional)")
    print(f"  slippage recovered at {lots} lots if limits fill: "
          f"Rs{slip * prem * 65 * lots * len(t):+,.0f}")
    t.to_csv(HERE / "realised_round_trips.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
