"""
Red Bar / X-Candle — FULL walk-forward: parameters AND gate re-fitted quarterly
================================================================================
redbar_walkforward.py re-fitted only the gate cutoff and found the baseline
config at PF 1.36 over 347 OOS trades. That still flatters the strategy,
because hold / RR / max_sl came from a grid fitted on this same history. A
config chosen with hindsight will look stable in a walk-forward that never
re-chooses it.

This removes the last leak. Every quarter, BOTH the parameters and the gate
cutoff are re-selected using only the trailing 12 months, then applied
untouched to the next quarter:

    for each quarter Q from 2024Q1:
        train = trailing 12 months of signals
        cutoff = 75th pct of train mom5_prev
        params = argmax over the grid of train net (with that cutoff)
        score  = apply params + cutoff to Q, once

If the edge survives this, it is a property of the signal rather than of the
sweep. If it does not, the earlier PF 1.36 was the grid talking.

A "frozen" arm is reported alongside: the single hindsight-chosen config
(hold 90 / RR 3.0 / maxSL 0.80) scored on the same quarters. The gap between
the two arms is the cost of not knowing the right parameters in advance.

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_walkforward_full.py
"""
import os
import sys
from itertools import product
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
HOLDS = (60, 90, 120, 180)
RRS = (2.0, 3.0, 4.0)
MAXSLS = (0.40, 0.60, 0.80)
FROZEN = (90, 3.0, 0.80)


def pf(s):
    w = s[s > 0].sum()
    gl = abs(s[s <= 0].sum())
    return w / gl if gl else 99.0


def precompute(d30, daily):
    """Run every grid combo once over the full history; index by params."""
    m = rb.load_strategy()
    m.EXIT_TIME = pd.to_datetime("2026-01-01 15:10").time()
    tables = {}
    for hold, rr, maxsl in product(HOLDS, RRS, MAXSLS):
        m.MAX_HOLD_MINUTES, m.RR, m.MAX_SL_PCT = hold, rr, maxsl
        t = rb.backtest(m, d30, "NIFTY", LOT, 0.55)
        t["date"] = pd.to_datetime(t["date"])
        t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                          if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
        t = t.dropna(subset=["mom5_prev"])
        t = t[t["date"].dt.dayofweek != 1]           # fixed rule, no fitting
        tables[(hold, rr, maxsl)] = t.sort_values("date")
    return tables


def main():
    df5 = load_full_5m()
    d30 = rt.resample(df5, 30)
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    print(f"data {df5.index.min().date()} .. {df5.index.max().date()}")
    print(f"precomputing {len(HOLDS) * len(RRS) * len(MAXSLS)} grid combos...")
    tables = precompute(d30, daily)

    picked, frozen_rows = [], []
    q_start = START_OOS
    last = max(t["date"].max() for t in tables.values())
    log = []
    while q_start <= last:
        q_end = q_start + pd.offsets.QuarterEnd(0) + pd.Timedelta(days=1)
        tr_lo = q_start - pd.DateOffset(months=TRAIL_MONTHS)

        ref = tables[FROZEN]
        train_ref = ref[(ref["date"] >= tr_lo) & (ref["date"] < q_start)]
        if len(train_ref) < 30:
            q_start = q_end
            continue
        cutoff = train_ref["mom5_prev"].quantile(0.75)

        best, best_net = None, -np.inf
        for params, t in tables.items():
            tr = t[(t["date"] >= tr_lo) & (t["date"] < q_start) & (t["mom5_prev"] < cutoff)]
            if len(tr) < 20:
                continue
            if tr["rs"].sum() > best_net:
                best, best_net = params, tr["rs"].sum()
        if best is None:
            q_start = q_end
            continue

        qlabel = q_start.to_period("Q").strftime("%YQ%q")
        for params, bucket in ((best, picked), (FROZEN, frozen_rows)):
            t = tables[params]
            te = t[(t["date"] >= q_start) & (t["date"] < q_end) & (t["mom5_prev"] < cutoff)].copy()
            te["quarter"], te["params"] = qlabel, str(params)
            bucket.append(te)
        log.append((qlabel, best, best_net, len(picked[-1])))
        q_start = q_end

    sel = pd.concat(picked)
    frz = pd.concat(frozen_rows)

    print("\nquarterly parameter choice (trailing-12m winner) and its OOS quarter:")
    print(f"  {'quarter':8s} {'chosen (hold,RR,maxSL)':24s} {'trainNet':>9s} "
          f"{'T':>3s} {'OOS Rs':>9s}")
    for (qlabel, best, tnet, _), (_, g) in zip(log, sel.groupby("quarter", sort=False)):
        print(f"  {qlabel:8s} {str(best):24s} {tnet:+9,.0f} {len(g):3d} {g['rs'].sum():+9,.0f}")

    print()
    for lbl, w in (("re-fitted params (honest)", sel), ("frozen 90/3.0/0.80 (hindsight)", frz)):
        s = w["rs"]
        eq = s.cumsum()
        print(f"{lbl}")
        print(f"  OOS: T {len(w):4d} | net Rs {s.sum():+9,.0f} | mean Rs {s.mean():+6,.0f} "
              f"| PF {pf(s):5.2f} | win {(s > 0).mean():5.1%}")
        by_year = w.groupby(w["date"].dt.year)["rs"].agg(["count", "sum"])
        print("  " + " | ".join(f"{y}: {r['count']:3.0f}T {r['sum']:+7,.0f}"
                                for y, r in by_year.iterrows()))
        q = w.groupby("quarter")["rs"].sum()
        print(f"  positive quarters {int((q > 0).sum())}/{len(q)} | "
              f"maxDD Rs {(eq - eq.cummax()).min():,.0f} | "
              f"net w/o best 3 Rs {s.sum() - s.nlargest(3).sum():+,.0f}\n")

    sel.to_csv(Path(__file__).parent / "redbar_walkforward_full_trades.csv", index=False)


if __name__ == "__main__":
    main()
