"""
Red Bar / X-Candle — OVERNIGHT hold priced on REAL option premiums
===================================================================
The spot study (redbar_overnight.py) found a +15.6 pt overnight gap edge in
the signal's direction on the fitted window. An ATM weekly option loses real
time value overnight, so the question is arithmetic: is the gap worth more
than the night costs?

This script answers it with measured data instead of a theta model.

PART A -- overnight decay, measured. For every (date, nearest expiry, ATM
strike) in the harvest window, take the premium at 15:10 and at 09:20 the
next session, with NIFTY spot at both times, then regress

    d_premium  =  b * d_spot  +  c

b is the realised overnight delta; c is what one night costs in premium
points, net of everything (theta, overnight vol repricing, open auction).
This uses ALL contracts, not just signal days, so the sample is large.

PART B -- the strategy itself. For the gated signals inside the harvest
window, price the real overnight hold: buy the ATM option at the signal, sell
it at the next session's 09:20 (gap capture) or 15:10 (full next day).

Costs: statutory 0.12% x2 + spread 0.41% on real premium. No brokerage.

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_overnight_premium.py
"""
import os
import sqlite3
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")
sys.path.insert(0, str(Path(__file__).parent))

import redbar_backtest as rb
import redbar_trail_backtest as rt
from redbar_features import daily_features

ROOT = Path(__file__).resolve().parent.parent.parent
HARVEST = ROOT / "harvest_state.db"
ARCHIVE = ROOT / "harvest_options_archive.db"
LOT = 65


def pf(x):
    w = x[x > 0].sum()
    gl = abs(x[x <= 0].sum())
    return w / gl if gl else 99.0


def load_option_bars():
    frames = []
    for path, table, limit in ((HARVEST, "options_bars", 7_000_000),
                               (ARCHIVE, "options_bars_full", 27_300_000)):
        if not path.exists():
            continue
        con = sqlite3.connect(path)
        try:
            frames.append(pd.read_sql_query(
                f"select timestamp, expiry, strike, option_type, close from "
                f"(select * from {table} limit {limit}) where underlying='NIFTY'", con))
        except Exception as exc:
            print(f"  {path.name}: partial read ({str(exc)[:50]})")
        con.close()
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["timestamp"].str.replace("+05:30", ""))
    df["date"] = df["ts"].dt.date
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["strike"] = df["strike"].astype(int)
    return df.dropna(subset=["expiry", "close"]).drop_duplicates(
        subset=["ts", "expiry", "strike", "option_type"])


def load_spot_1m():
    con = duckdb.connect(str(ROOT / "backtesting/data/market_cache.duckdb"), read_only=True)
    df = con.execute("select timestamp, close from market_data "
                     "where symbol='NIFTY' and interval='1m' order by timestamp").df()
    con.close()
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
    return df.set_index("ts")["close"].sort_index()


def price_at(series, day, t, how="last_at_or_before"):
    """Last print at or before `t` on `day` (None if the day has no data)."""
    target = pd.Timestamp(day) + pd.Timedelta(hours=t.hour, minutes=t.minute)
    idx = series.index[(series.index.date == day) & (series.index <= target)]
    if len(idx) == 0:
        return None
    return float(series.loc[idx[-1]])


def main():
    print("loading real option bars...")
    bars = load_option_bars()
    spot = load_spot_1m()
    dates = sorted(bars["date"].unique())
    exp_by_date = {d: sorted(g["expiry"].unique()) for d, g in bars.groupby("date")}
    print(f"  {len(bars):,} option bars | {dates[0]} .. {dates[-1]}")

    # ---------------------------------------------------------------- PART A
    obs = []
    for i in range(len(dates) - 1):
        d, nxt = dates[i], dates[i + 1]
        s_now = price_at(spot, d, dtime(15, 10))
        s_nxt = price_at(spot, nxt, dtime(9, 20))
        if s_now is None or s_nxt is None:
            continue
        exps = [e for e in exp_by_date.get(d, []) if e > nxt]   # must survive the night
        if not exps:
            continue
        expiry = exps[0]
        atm = int(round(s_now / 50.0) * 50)
        for otype in ("CE", "PE"):
            c = bars[(bars["expiry"] == expiry) & (bars["strike"] == atm) &
                     (bars["option_type"] == otype)]
            if c.empty:
                continue
            ser = c.set_index("ts")["close"].sort_index()
            p_now = price_at(ser, d, dtime(15, 10))
            p_nxt = price_at(ser, nxt, dtime(9, 20))
            if p_now is None or p_nxt is None or p_now <= 0:
                continue
            obs.append({"date": d, "type": otype, "strike": atm, "expiry": expiry,
                        "dte": (expiry - d).days, "d_spot": s_nxt - s_now,
                        "d_prem": p_nxt - p_now, "prem": p_now})
    o = pd.DataFrame(obs)
    print(f"\nPART A -- overnight decay measured on {len(o)} contract-nights "
          f"({o['date'].nunique()} nights)")
    if len(o) > 10:
        # CE and PE together: signed delta, so flip PE spot move
        o["d_spot_signed"] = np.where(o["type"] == "CE", o["d_spot"], -o["d_spot"])
        A = np.column_stack([o["d_spot_signed"].values, np.ones(len(o))])
        coef, *_ = np.linalg.lstsq(A, o["d_prem"].values, rcond=None)
        b, c = coef
        resid = o["d_prem"].values - A @ coef
        se = resid.std() / np.sqrt(len(o))
        print(f"  d_premium = {b:.4f} * d_spot  {c:+.2f}   (n={len(o)}, resid sd {resid.std():.1f})")
        print(f"  realised overnight delta : {b:.3f}")
        print(f"  ONE NIGHT COSTS          : {c:+.2f} premium points "
              f"= Rs {c * LOT:+,.0f} per lot  (+/- {se * LOT:,.0f})")
        for lo, hi in ((0, 5), (5, 10), (10, 40)):
            g = o[(o["dte"] >= lo) & (o["dte"] < hi)]
            if len(g) > 10:
                Ag = np.column_stack([g["d_spot_signed"].values, np.ones(len(g))])
                cg, *_ = np.linalg.lstsq(Ag, g["d_prem"].values, rcond=None)
                print(f"    DTE {lo:2d}-{hi-1:2d}: delta {cg[0]:.3f} | night cost "
                      f"{cg[1]:+.2f} pts = Rs {cg[1] * LOT:+,.0f}/lot  (n={len(g)})")

    # ---------------------------------------------------------------- PART B
    m = rb.load_strategy()
    m.EXIT_TIME = pd.to_datetime("2026-01-01 15:10").time()
    m.MAX_HOLD_MINUTES, m.RR, m.MAX_SL_PCT = 90, 3.0, 0.80
    df5 = rb.load_bars("NIFTY")
    t = rb.backtest(m, rt.resample(df5, 30), "NIFTY", LOT, 0.55)
    t["date"] = pd.to_datetime(t["date"])
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
    t = t[(t["date"].dt.dayofweek != 1) & (t["mom5_prev"] < 0.0137)].dropna(subset=["mom5_prev"])
    t["date"] = t["date"].dt.date
    t = t[(t["date"] >= dates[0]) & (t["date"] <= dates[-1])]

    dpos = {d: i for i, d in enumerate(dates)}
    rows = []
    for _, tr in t.iterrows():
        d = tr["date"]
        i = dpos.get(d)
        if i is None or i + 1 >= len(dates):
            continue
        nxt = dates[i + 1]
        exps = [e for e in exp_by_date.get(d, []) if e > nxt]
        if not exps:
            continue
        expiry = exps[0]
        strike = int(round(tr["entry"] / 50.0) * 50)
        c = bars[(bars["expiry"] == expiry) & (bars["strike"] == strike) &
                 (bars["option_type"] == tr["dir"])]
        if c.empty:
            continue
        ser = c.set_index("ts")["close"].sort_index()
        entry_ts = pd.Timestamp(tr["entry_ts"])
        idx = ser.index[ser.index <= entry_ts]
        if len(idx) == 0:
            continue
        p_in = float(ser.loc[idx[-1]])
        p_gap = price_at(ser, nxt, dtime(9, 20))
        p_eod = price_at(ser, nxt, dtime(15, 10))
        if p_in <= 0 or p_gap is None:
            continue
        costs = (2 * p_in * LOT) * rb.OPT_COST_PCT / 100.0 + p_in * LOT * rb.SPREAD_PCT / 100.0
        rows.append({
            "date": d, "dir": tr["dir"], "dte": (expiry - d).days,
            "prem_in": round(p_in, 2), "prem_gap": round(p_gap, 2),
            "prem_d1": round(p_eod, 2) if p_eod else np.nan,
            "rs_intraday": tr["rs"],
            "rs_gap": (p_gap - p_in) * LOT - costs,
            "rs_d1": ((p_eod - p_in) * LOT - costs) if p_eod else np.nan,
        })
    b_ = pd.DataFrame(rows)
    print(f"\nPART B -- overnight hold on REAL premiums, {len(b_)} gated trades")
    if len(b_):
        for col, lbl in (("rs_intraday", "intraday (baseline)"),
                         ("rs_gap", "hold to next 09:20"),
                         ("rs_d1", "hold to next 15:10")):
            g = b_[col].dropna()
            print(f"  {lbl:22s}: T {len(g):3d} | net Rs {g.sum():+9,.0f} "
                  f"| mean Rs {g.mean():+7,.0f} | PF {pf(g):5.2f} | win {(g > 0).mean():5.1%}")
        b_.to_csv(Path(__file__).parent / "redbar_overnight_premium_trades.csv", index=False)


if __name__ == "__main__":
    main()
