"""
Red Bar / X-Candle — walk-forward parameter grid (IS 2023-24 -> OOS 2025-26)
============================================================================
Honest protocol: every parameter combo is evaluated on IS (2023-24), the
winner is chosen on IS alone, then reported on OOS (2025-26) exactly once.
All combos also carry the two IS-validated gates (AUDITED 2026-08-06 for
lookahead: an earlier rv5 gate used today's close and was discarded):
  - mom5_prev < 0.0137   (skip strong-uptrend days: 5-day momentum ending
    YESTERDAY; 0.0137 = the IS 2023-24 75th percentile boundary of mom5_prev,
    chosen BEFORE any OOS view; Q4-up was the IS killer, PF 0.55)
  - skip Tuesday        (only IS-justified day gate that survived OOS)

CAS constraint: exits are capped at 15:10. Under the new CAS (live 2026-08-03)
the spot index freezes at 15:15 and the auction print lands ~15:28-29; the
strategy's exit logic is spot-based, so post-15:15 exits cannot be modelled
honestly on this data (the broker's history API returns nothing after 15:29
and spot teleports to the auction price). The grid therefore covers only the
CAS-valid exit window.

Cost model as everywhere this session: statutory 0.12% x2 + 0.41% spread on
premium, measured delta 0.358, NIFTY lot 65, premium 0.55% of spot.
"""
import importlib.util
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
from redbar_features import build_trades, daily_features, load_5m

FEATS = Path(__file__).parent / "redbar_trades_features_NIFTY.csv"


def pf(x):
    w = x.loc[x["rs"] > 0, "rs"].sum()
    gl = abs(x.loc[x["rs"] <= 0, "rs"].sum())
    return w / gl if gl else 99.0


def load_features():
    f = pd.read_csv(FEATS, parse_dates=["date"])
    f["yr"] = f["date"].dt.year
    f["dow"] = f["date"].dt.dayofweek
    # honest no-lookahead features: 5-day vol/momentum ending YESTERDAY
    df5 = load_5m("NIFTY")
    daily = daily_features(df5)
    daily["rv5_prev"] = daily["rv5"].shift(1)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    f = f.set_index("date").join(daily[["rv5_prev", "mom5_prev"]], how="left")
    return f


def run_grid():
    m = rb.load_strategy()
    df5 = rb.load_bars("NIFTY")
    d30 = rt.resample(df5, 30)
    feats = load_features()

    base = {k: getattr(m, k) for k in
            ("FIB_HI", "FIB_LO", "RR", "MAX_SL_PCT", "MIN_SL_PCT",
             "EXIT_TIME", "MAX_HOLD_MINUTES", "ENTRY_END")}

    grid = product(
        [pd.to_datetime("2026-01-01 " + t).time() for t in ("14:15", "14:30", "14:45", "15:00", "15:10")],
        [60, 90, 120, 180],
        [2.0, 3.0, 4.0],
        [0.40, 0.60, 0.80],
    )
    results = []
    for exit_t, hold, rr, maxsl in grid:
        m.EXIT_TIME, m.MAX_HOLD_MINUTES, m.RR, m.MAX_SL_PCT = exit_t, hold, rr, maxsl
        t = rb.backtest(m, d30, "NIFTY", 65, 0.55)
        t["date"] = pd.to_datetime(t["date"])
        t = t.set_index("date").join(feats[["rv5_prev", "mom5_prev", "dow", "yr"]], how="left")
        t = t[(t["mom5_prev"] < 0.0137) & (t["dow"] != 1)].dropna(subset=["mom5_prev"])
        is_t, oos_t = t[t["yr"] <= 2024], t[t["yr"] >= 2025]
        results.append({
            "exit": exit_t.strftime("%H:%M"), "hold": hold, "rr": rr, "maxsl": maxsl,
            "T_is": len(is_t), "net_is": is_t["rs"].sum(), "pf_is": pf(is_t),
            "T_oos": len(oos_t), "net_oos": oos_t["rs"].sum(), "pf_oos": pf(oos_t),
        })
        # restore per-combo
        for k, v in base.items():
            setattr(m, k, v)

    r = pd.DataFrame(results).sort_values("net_is", ascending=False)
    print("=" * 118)
    print(" GRID: gates mom5_prev<0.0137 (IS-Q4 boundary) + skip-Tue. Sorted by IS net Rs.")
    print("=" * 118)
    print(r.to_string(index=False, float_format=lambda x: f"{x:,.0f}" if abs(x) > 1000 else f"{x:.2f}"))
    r.to_csv(Path(__file__).parent / "redbar_grid_results.csv", index=False)
    return r


if __name__ == "__main__":
    run_grid()