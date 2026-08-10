"""
Does the tuned Stochastic config survive its own tuning? Cross-apply + IS/OOS.
=============================================================================
The sweep produced a tempting headline (NIFTY 30m, PF 1.21, +Rs 347k) but two
facts make it suspect before anything else is measured:

  1. Only 16 of 162 NIFTY configurations were profitable (10%). Picking the
     best of 162 is a multiple-comparisons problem, not a discovery.
  2. The discriminating parameter FLIPS between symbols. Every one of NIFTY's
     top 12 used the `range` regime filter; every one of SENSEX's top 7 used
     `none`. A real mechanism does not invert when you change the index.

So this file does the two tests that a curve-fit cannot pass:

  CROSS-APPLY  -- run each symbol's champion config on the OTHER symbol.
                  A genuine edge transfers; a fitted one collapses.
  IS/OOS       -- split each run at the calendar midpoint. Parameters were
                  chosen using the whole period, so OOS here is generous
                  (it is contaminated), and failing even THAT is damning.

Usage:
    ./venv/Scripts/python.exe backtesting/stochastic/validate.py
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from stoch_backtest import BASE, line, load_bars, run, stats  # noqa: E402

# champions straight off the two sweeps
NIFTY_BEST = dict(BASE, oversold=30, overbought=70, trend_filter="range",
                  rr=3.0, sl_pct=0.35)
SENSEX_BEST = dict(BASE, oversold=30, overbought=70, trend_filter="none",
                   rr=1.5, sl_pct=0.35)
CONFIGS = {"NIFTY-champion (range rr3.0)": NIFTY_BEST,
           "SENSEX-champion (none rr1.5)": SENSEX_BEST}


def main():
    print("Stochastic tuned-config validation -- cross-apply and IS/OOS")
    print("A config that only works on the symbol it was fitted to is a fit.\n")

    for cfg_name, p in CONFIGS.items():
        print(f"================ {cfg_name} ================")
        for sym in ("NIFTY", "SENSEX"):
            df = load_bars(sym, 30)
            t = run(df, p)
            if t.empty or len(t) < 10:
                print(f"  {sym}: too few trades")
                continue
            s = stats(t, sym, f"{sym} ALL")
            print(line(s))
            cut = t["day"].iloc[len(t) // 2]
            for lab, part in (("IS", t[t["day"] < cut]), ("OOS", t[t["day"] >= cut])):
                ss = stats(part, sym, f"{sym} {lab}")
                if ss:
                    print(line(ss))
        print()

    # the single most useful number: how often is the champion profitable
    # on the symbol it was NOT fitted to?
    print("=== transfer test summary ===")
    for cfg_name, p in CONFIGS.items():
        home = "NIFTY" if cfg_name.startswith("NIFTY") else "SENSEX"
        away = "SENSEX" if home == "NIFTY" else "NIFTY"
        rows = {}
        for sym in (home, away):
            t = run(load_bars(sym, 30), p)
            s = stats(t, sym, sym)
            rows[sym] = s["net"] if s else float("nan")
        verdict = "TRANSFERS" if rows[away] > 0 else "FAILS TO TRANSFER"
        print(f"  {cfg_name:30s} home {home}: {rows[home]:+10,.0f}   "
              f"away {away}: {rows[away]:+10,.0f}   -> {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
