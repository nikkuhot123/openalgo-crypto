"""
Red Bar / X-Candle — ROLLING walk-forward with the gate re-fitted every quarter
===============================================================================
Everything before this leaned on one 28-trade forward window, which is far too
small: the best variant found there (+Rs 4,412) collapses to -Rs 2,027 if the
single best trade is removed. Two trades carried it.

This runs a proper rolling walk-forward instead, so that almost the whole
history becomes out-of-sample and no threshold is ever fitted on the data it
is scored against:

    for each calendar quarter Q from 2024Q1 onward:
        fit   : mom5_prev cutoff = 75th percentile of the trailing 12 months
                of signal days (the same rule the original gate used)
        apply : skip-Tue + that cutoff to Q's trades
        score : accumulate Q's result

skip-Tue is a fixed rule with no fitted parameter, so it carries over.

Configs compared (all exits capped at 15:10 for CAS):
    baseline     SL from trigger bar, RR 3.0, max-hold 90
    no-stop-90   spot stop removed, max-hold 90
    no-stop-EOD  spot stop removed, hold to the 15:10 squareoff

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_walkforward.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")
sys.path.insert(0, str(Path(__file__).parent))

import redbar_backtest as rb
import redbar_trail_backtest as rt
from redbar_features import daily_features
from redbar_overnight import load_full_5m

LOT = 65
TRAIL_MONTHS = 12
START_OOS = pd.Timestamp("2024-01-01")


def pf(s):
    w = s[s > 0].sum()
    gl = abs(s[s <= 0].sum())
    return w / gl if gl else 99.0


def run_config(d30, daily, hold, rr, maxsl, use_stop):
    m = rb.load_strategy()
    m.EXIT_TIME = pd.to_datetime("2026-01-01 15:10").time()
    m.MAX_HOLD_MINUTES, m.RR, m.MAX_SL_PCT = hold, rr, maxsl
    t = rb.backtest(m, d30, "NIFTY", LOT, 0.55, use_stop=use_stop)
    t["date"] = pd.to_datetime(t["date"])
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
    return t.dropna(subset=["mom5_prev"]).sort_values("date")


def walk_forward(t):
    """Quarterly rebalance; cutoff re-fitted on the trailing 12 months only."""
    out = []
    q_start = START_OOS
    last = t["date"].max()
    while q_start <= last:
        q_end = q_start + pd.offsets.QuarterEnd(0) + pd.Timedelta(days=1)
        train = t[(t["date"] < q_start) &
                  (t["date"] >= q_start - pd.DateOffset(months=TRAIL_MONTHS))]
        test = t[(t["date"] >= q_start) & (t["date"] < q_end)]
        if len(train) >= 30 and len(test):
            cutoff = train["mom5_prev"].quantile(0.75)
            sel = test[(test["date"].dt.dayofweek != 1) &
                       (test["mom5_prev"] < cutoff)].copy()
            sel["cutoff"] = cutoff
            sel["quarter"] = q_start.to_period("Q").strftime("%YQ%q")
            out.append(sel)
        q_start = q_end
    return pd.concat(out) if out else pd.DataFrame()


def main():
    df5 = load_full_5m()
    d30 = rt.resample(df5, 30)
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    print(f"data {df5.index.min().date()} .. {df5.index.max().date()}\n")

    configs = [
        ("baseline (stop, hold 90)", dict(hold=90, rr=3.0, maxsl=0.80, use_stop=True)),
        ("no-stop, hold 90", dict(hold=90, rr=3.0, maxsl=0.80, use_stop=False)),
        ("no-stop, hold to 15:10", dict(hold=400, rr=3.0, maxsl=0.80, use_stop=False)),
    ]
    results = {}
    for lbl, kw in configs:
        t = run_config(d30, daily, **kw)
        w = walk_forward(t)
        results[lbl] = w
        print(f"{lbl}")
        print(f"  walk-forward OOS: T {len(w):4d} | net Rs {w['rs'].sum():+9,.0f} "
              f"| mean Rs {w['rs'].mean():+6,.0f} | PF {pf(w['rs']):5.2f} "
              f"| win {(w['rs'] > 0).mean():5.1%}")
        by_year = w.groupby(w["date"].dt.year)["rs"].agg(["count", "sum"])
        cells = " | ".join(f"{y}: {r['count']:3.0f}T {r['sum']:+7,.0f}"
                           for y, r in by_year.iterrows())
        print(f"  {cells}")
        # robustness: how much of the result is one trade?
        s = w["rs"]
        print(f"  net without best trade Rs {s.sum() - s.max():+9,.0f} | "
              f"without best 3 Rs {s.sum() - s.nlargest(3).sum():+9,.0f}")
        eq = s.cumsum()
        print(f"  max drawdown Rs {(eq - eq.cummax()).min():,.0f}\n")

    best = max(results, key=lambda k: results[k]["rs"].sum())
    w = results[best]
    print(f"best by walk-forward net: {best}")
    print("\nquarter-by-quarter for that config:")
    q = w.groupby("quarter")["rs"].agg(["count", "sum"])
    for qq, r in q.iterrows():
        bar = "+" if r["sum"] > 0 else "-"
        print(f"  {qq}  T {r['count']:3.0f}  Rs {r['sum']:+8,.0f}  {bar * min(int(abs(r['sum']) / 1500) + 1, 20)}")
    print(f"\n  positive quarters: {(q['sum'] > 0).sum()}/{len(q)}")
    w.to_csv(Path(__file__).parent / "redbar_walkforward_trades.csv", index=False)


if __name__ == "__main__":
    main()
