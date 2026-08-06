"""
Red Bar / X-Candle — TRUE FORWARD TEST on unseen data (2026-05-28 .. today)
===========================================================================
Every number in the spec so far comes from bars that end 2026-05-27, which is
where the local duckdb cache stops. The gates (skip-Tue, mom5_prev < 0.0137)
and the parameter grid were both fitted on data inside that boundary.

This script pulls FRESH 5-minute NIFTY spot from the live broker API and
re-runs the recommended configuration on 2026-05-28 onward -- bars that no
gate, no grid and no threshold in this session has ever seen. It is the only
genuinely out-of-sample window available.

It matters because the strategy file's own docstring records a Volrix run
(real weekly premiums) over 2026-06-08..2026-08-04 that lost money:
NIFTY 35 trades, -Rs 15,301, PF 0.5 -- but that was the UNGATED config with
in-sample-tuned constants. This asks whether the gated config survives the
same period.

Config under test (spec section 2):
    EXIT_TIME 15:10 | MAX_HOLD 90 | RR 3.0 | MAX_SL_PCT 0.80
    gates: skip Tuesday, mom5_prev < 0.0137

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_forward_test.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")
sys.path.insert(0, str(Path(__file__).parent))

import redbar_backtest as rb
import redbar_trail_backtest as rt
from redbar_features import daily_features

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = Path(__file__).parent / "redbar_forward_trades.csv"

LOT = 65
CUTOFF = datetime(2026, 5, 27).date()   # local cache ends here; everything after is unseen
WARMUP_FROM = "2026-03-01"              # lookback for anchors + daily features
CALIB = 1.185                           # real = 1.185 x delta (n=31, CI [0.936, 1.438])


def pf(x):
    w = x[x > 0].sum()
    gl = abs(x[x <= 0].sum())
    return w / gl if gl else 99.0


def main():
    # NOTE: the broker's 5m history is range-dependent -- a multi-day request
    # silently returns different bars than a single-day one (2026-06-12 came
    # back at 23,984.85 over a long range vs 23,631.75 alone). load_full_5m
    # fetches per-session and verifies, so it is the only safe source here.
    from redbar_overnight import load_full_5m
    today = datetime.now().date().isoformat()
    print(f"loading cached history + per-day-verified fresh bars .. {today}")
    df5 = load_full_5m()
    print(f"  {len(df5):,} bars | {df5.index.min()} .. {df5.index.max()}")
    unseen = sorted({d for d in df5.index.date if d > CUTOFF})
    print(f"  unseen sessions (> {CUTOFF}): {len(unseen)}")

    m = rb.load_strategy()
    # recommended configuration (spec section 2)
    m.EXIT_TIME = pd.to_datetime("2026-01-01 15:10").time()
    m.MAX_HOLD_MINUTES = 90
    m.RR = 3.0
    m.MAX_SL_PCT = 0.80
    print(f"config: exit {m.EXIT_TIME} | hold {m.MAX_HOLD_MINUTES} | "
          f"RR {m.RR} | maxSL {m.MAX_SL_PCT}%")

    d30 = rt.resample(df5, 30)
    t = rb.backtest(m, d30, "NIFTY", LOT, 0.55)
    if t.empty:
        print("no trades generated")
        return
    t["date"] = pd.to_datetime(t["date"])

    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]

    fwd = t[t["date"].dt.date > CUTOFF].copy()
    print(f"\nraw signals in the unseen window: {len(fwd)}")
    gated = fwd[(fwd["date"].dt.dayofweek != 1) &
                (fwd["mom5_prev"] < 0.0137)].dropna(subset=["mom5_prev"])
    print(f"after gates (skip-Tue + mom5_prev<0.0137): {len(gated)}")

    for lbl, g in (("UNGATED", fwd), ("GATED", gated)):
        if not len(g):
            continue
        net = g["rs"].sum()
        print(f"\n{lbl}: T {len(g)} | delta net Rs {net:+,.0f} | PF {pf(g['rs']):.2f} "
              f"| win {(g['rs'] > 0).mean():.1%}")
        print(f"  real-equivalent (x{CALIB}): Rs {net * CALIB:+,.0f}")
        print("  exits:", dict(g["reason"].value_counts()))
        eq = g["rs"].cumsum()
        print(f"  max drawdown (delta): Rs {(eq - eq.cummax()).min():,.0f}")

    gated.to_csv(OUT, index=False)
    print(f"\nwrote {OUT.name}")
    if len(gated):
        print("\nper-trade detail:")
        cols = ["date", "dir", "reason", "pts", "rs"]
        print(gated[cols].to_string(index=False))


if __name__ == "__main__":
    main()
