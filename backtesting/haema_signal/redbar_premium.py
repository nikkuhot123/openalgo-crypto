"""
Red Bar / X-Candle — REAL premium re-pricing (harvest_state.db)
================================================================
The delta model (pts * 0.358 * lot - costs) is optimistic for any position
held into 0DTE theta bleed. harvest_state.db carries ~3M readable 1-minute
NIFTY option bars for 2026-01-28..2026-05-27 -- the only real-premium
evidence this session has. This script re-prices every Red Bar trade in that
window at the ACTUAL ATM option premium: same entry/exit timestamps, same
direction, same costs (statutory 0.12% x2 + 0.41% spread on premium turnover).

P&L premium model: long option => (prem_exit - prem_entry) * lot - costs.
The delta model: pts * DELTA * lot - costs. The gap between the two is the
theta/vega/gamma/spread reality the spot model cannot see.

Fill semantics (audited 2026-08-06 against live greeks from the VPS):
  - entry  : first 1-min bar at/after the backtest's next-bar-open fill
  - EOD    : 15:10 wall-clock (CAS: last pre-freeze bar)
  - SL/target/max-hold: LAST 1-min print of the exit 30m bucket. The level
    has crossed by the end of the bucket; the bucket's FIRST minute precedes
    the crossing and zeroes out the move (a CE target showed premium DOWN on
    a winning trade under the old fill -- fixed).
  - Costs: statutory 0.12% x2 + 0.41% spread on real premium (no brokerage).
    Live anchor 2026-08-06: NIFTY 11-Aug weekly (5.13 DTE) ATM CE prem 144.35,
    IV 11.78%, delta 0.519, theta -13.38/day, spread 0.24-0.29% -- i.e.
    ~116 Rs/lot of physical friction per 90-min hold (55 theta + 22
    statutory + ~39 spread).

Usage:
    ../venv/Scripts/python.exe backtesting/haema_signal/redbar_premium.py

Outputs:
    redbar_premium_trades.csv   per-trade delta-vs-premium comparison
    prints    aggregate PF/net for both models on the shared trade set
"""
import importlib.util
import os
import sqlite3
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

HARVEST = Path(__file__).resolve().parent.parent.parent / "harvest_state.db"
OUT = Path(__file__).parent / "redbar_premium_trades.csv"
LOT = 65
READABLE_LIMIT = 7000000  # options_bars readable up to ~7.13M rowids (corrupt after)


def pf(x):
    w = x.loc[x["rs"] > 0, "rs"].sum()
    gl = abs(x.loc[x["rs"] <= 0, "rs"].sum())
    return w / gl if gl else 99.0


def load_option_bars():
    con = sqlite3.connect(HARVEST)
    q = f"""
        select timestamp, tradingsymbol, expiry, strike, option_type, open, high, low, close
        from (select * from options_bars limit {READABLE_LIMIT})
        where underlying = 'NIFTY'
    """
    df = pd.read_sql_query(q, con)
    con.close()
    df["ts"] = pd.to_datetime(df["timestamp"].str.replace("+05:30", ""))  # wall time, already IST
    df["date"] = df["ts"].dt.date
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


def main():
    bars = load_option_bars()
    bars = bars.dropna(subset=["expiry"])
    bars["strike"] = bars["strike"].astype(int)
    # nearest expiry >= trade date, per date, with a bar that day
    exp_by_date = {}
    for d, g in bars.groupby("date"):
        exp_by_date[d] = sorted(g["expiry"].dt.date.unique())

    m = rb.load_strategy()
    df5 = rb.load_bars("NIFTY")
    d30 = rt.resample(df5, 30)

    t = rb.backtest(m, d30, "NIFTY", LOT, 0.55)
    t["date"] = pd.to_datetime(t["date"])
    # honest gates (IS-chosen): skip-Tue + mom5_prev < 0.0137 (5-day momentum
    # ending YESTERDAY -- recomputed from the daily series, no lookahead)
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
    t = t[(t["date"].dt.dayofweek != 1) & (t["mom5_prev"] < 0.0137)].dropna(subset=["mom5_prev"])
    t = t[(t["date"].dt.date >= datetime(2026, 1, 28).date()) &
          (t["date"].dt.date <= datetime(2026, 5, 27).date())]
    t["date"] = t["date"].dt.date
    print(f"gated NIFTY trades in premium window: {len(t)}")

    priced = []
    for _, tr in t.iterrows():
        d = tr["date"]
        exps = [e for e in exp_by_date.get(d, []) if e >= d] or exp_by_date.get(d, [])
        if not exps:
            continue
        expiry = exps[0]
        strike = int(round(tr["entry"] / 50.0) * 50)
        otype = tr["dir"]
        day_bars = bars[(bars["date"] == d) & (bars["expiry"].dt.date == expiry) &
                        (bars["strike"] == strike) & (bars["option_type"] == otype)]
        if day_bars.empty:
            continue
        day_bars = day_bars.sort_values("ts")
        # entry: fill at the next-bar open (backtest entry_time), first 1-min
        # bar at or after it
        ent_i = np.searchsorted(day_bars["ts"].values, np.datetime64(tr["entry_ts"]))
        if ent_i >= len(day_bars):
            continue
        ent = day_bars.iloc[ent_i]
        # CAS: live exits at EXIT_TIME wall-clock; backtest EOD fill is the
        # 15:25 spot print, which no longer exists under the 15:15 freeze.
        if tr["reason"] == "EOD":
            exit_ts = pd.Timestamp(d) + pd.Timedelta(hours=15, minutes=10)
            ext_i = np.searchsorted(day_bars["ts"].values, np.datetime64(exit_ts))
        else:
            # SL/target/max-hold: the level/close was crossed sometime INSIDE
            # the exit 30m bucket, so the honest observable fill is the LAST
            # 1-min print of that bucket (end-of-bar), never the bucket's open
            # minute -- which precedes the crossing and zeroes out the move.
            window_end = pd.Timestamp(tr["exit_ts"]) + pd.Timedelta(minutes=30)
            exit_ts = window_end
            ext_i = np.searchsorted(day_bars["ts"].values, np.datetime64(window_end), side="left") - 1
            if ext_i <= ent_i:  # same-bucket exit: earliest possible fill is entry+1
                ext_i = ent_i + 1
        if ext_i < 0 or ext_i >= len(day_bars):
            continue
        ext = day_bars.iloc[ext_i]
        prem_e, prem_x = ent["close"], ext["close"]
        if prem_e is None or prem_x is None or pd.isna(prem_e) or pd.isna(prem_x):
            continue
        costs = (2 * prem_e * LOT) * rb.OPT_COST_PCT / 100.0 + prem_e * LOT * rb.SPREAD_PCT / 100.0
        rs_real = (prem_x - prem_e) * LOT - costs
        priced.append({
            "date": d, "dir": tr["dir"], "reason": tr["reason"],
            "entry_ts": tr["entry_ts"], "exit_ts": exit_ts,
            "strike": strike, "expiry": expiry, "dte": (expiry - d).days,
            "prem_entry": round(prem_e, 2), "prem_exit": round(prem_x, 2),
            "rs_delta": tr["rs"], "rs_real": rs_real,
            "delta_minus_real": tr["rs"] - rs_real,
        })
    out = pd.DataFrame(priced)
    if out.empty:
        print("NO TRADES PRICED -- data mismatch")
        return
    out.to_csv(OUT, index=False)

    print(f"\npriced {len(out)}/{len(t)} trades (unpriced = no bar for ATM strike/expiry)")
    for lbl, g in (("ALL", out), ("CE", out[out["dir"] == "CE"]), ("PE", out[out["dir"] == "PE"]),
                   ("EOD/max-hold", out[out["reason"].isin(["EOD", "max-hold"])]),
                   ("SL/target", out[out["reason"].isin(["SL", "target"])])):
        d_ = g.rename(columns={"rs_delta": "rs"}); r_ = g.rename(columns={"rs_real": "rs"})
        print(f"  {lbl:11s}: T {len(g):3d} | delta: net {g['rs_delta'].sum():+10,.0f} PF {pf(d_):5.2f}"
              f" | REAL: net {g['rs_real'].sum():+10,.0f} PF {pf(r_):5.2f}"
              f" | theta-tax {g['delta_minus_real'].sum():+9,.0f}")
    print("\nper-trade gap stats (delta - real):")
    print(out["delta_minus_real"].describe(percentiles=[.25, .5, .75]).round(0).to_string())
    print("\nby DTE bucket (real premium is only for the expiries harvest kept;"
          "\nDTE>20 rows are far-dated contracts -- live trades weeklies, so the"
          "\ntheta tax on those rows UNDERSTATES the true 0DTE bleed):")
    for lo, hi in ((0, 10), (10, 21), (21, 99)):
        g = out[(out["dte"] >= lo) & (out["dte"] < hi)]
        if len(g):
            r_ = g.rename(columns={"rs_real": "rs"})
            print(f"  DTE {lo:2d}-{hi-1:2d}: T {len(g):3d} | delta net {g['rs_delta'].sum():+9,.0f} "
                  f"| REAL net {g['rs_real'].sum():+9,.0f} PF {pf(r_):5.2f} | tax {g['delta_minus_real'].sum():+9,.0f}")


if __name__ == "__main__":
    main()
