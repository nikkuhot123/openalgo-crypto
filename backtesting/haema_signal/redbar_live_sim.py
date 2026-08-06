"""
Red Bar / X-Candle — LIVE-FAITHFUL 1-minute simulation on REAL premiums
=======================================================================
Every earlier harness graded this strategy on 30-minute spot bars with a
delta approximation. Three things about the live loop were never modelled:

  1. The live loop polls every 5 SECONDS while IN_TRADE (`time.sleep(5)`),
     so spot SL / target fire on touch -- not at a 30m bar boundary.
  2. It carries a PREMIUM DECAY FLOOR: exit when option LTP < 60% of the
     entry premium (DECAY_EXIT_PCT), which fires on days where the option
     bleeds while spot never reaches the spot stop.
  3. It parks a broker-side premium stop-limit 70% below entry
     (PREMIUM_SL_PCT), which can fill autonomously.

This script re-runs the SAME gated signals at 1-minute resolution against
REAL option premiums (harvest_state.db + harvest_options_archive.db), with
the live exit ladder in the live priority order:

    EOD (15:10, CAS) > spot SL > spot target > max-hold > decay floor
    (premium SL is checked first each minute -- it is broker-side)

Costs: statutory only (0.12% x2) + spread 0.41% on real premium. No
brokerage is charged -- the account pays statutory taxes only.

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_live_sim.py

Outputs:
    redbar_live_sim_trades.csv   per-trade real-premium result
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
OUT = Path(__file__).parent / "redbar_live_sim_trades.csv"

LOT = 65
WIN_LO, WIN_HI = datetime(2026, 1, 28).date(), datetime(2026, 5, 27).date()
# live parameters (strategies/examples/red_bar_x_candle_strategy.py defaults)
MAX_HOLD_MIN = 90
DECAY_EXIT_PCT = 0.60     # exit when premium < 60% of entry
PREMIUM_SL_PCT = 70.0     # broker-side stop 70% below entry
EXIT_TIME = dtime(15, 10)  # CAS: last pre-freeze bar


def pf(x):
    w = x[x > 0].sum()
    gl = abs(x[x <= 0].sum())
    return w / gl if gl else 99.0


def load_spot_1m(lo, hi):
    """1-minute NIFTY spot for the window (exit detection at touch)."""
    con = duckdb.connect(str(ROOT / "backtesting/data/market_cache.duckdb"), read_only=True)
    df = con.execute(
        "select timestamp, open, high, low, close from market_data "
        "where symbol='NIFTY' and interval='1m' order by timestamp"
    ).df()
    con.close()
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
    df = df[(df["ts"].dt.date >= lo) & (df["ts"].dt.date <= hi)]
    return df.set_index("ts").sort_index()


def load_option_bars():
    """Real 1-minute option bars: harvest_state first, archive as backfill.

    Both files are truncated by corruption; each query is bounded to the
    readable rowid region and failures degrade to whatever was read.
    """
    frames = []
    for path, table, limit in ((HARVEST, "options_bars", 7_000_000),
                               (ARCHIVE, "options_bars_full", 27_300_000)):
        if not path.exists():
            continue
        con = sqlite3.connect(path)
        try:
            q = (f"select timestamp, expiry, strike, option_type, high, low, close from "
                 f"(select * from {table} limit {limit}) where underlying='NIFTY'")
            frames.append(pd.read_sql_query(q, con))
        except Exception as exc:  # corrupt page inside the readable window
            print(f"  {path.name}: partial read ({str(exc)[:60]})")
        con.close()
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["timestamp"].str.replace("+05:30", ""))
    df["date"] = df["ts"].dt.date
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["strike"] = df["strike"].astype(int)
    return df.dropna(subset=["expiry", "close"]).drop_duplicates(
        subset=["ts", "expiry", "strike", "option_type"])


def gated_trades():
    """Same signals, same honest gates as the grid: skip-Tue + mom5_prev."""
    m = rb.load_strategy()
    df5 = rb.load_bars("NIFTY")
    d30 = rt.resample(df5, 30)
    t = rb.backtest(m, d30, "NIFTY", LOT, 0.55)
    t["date"] = pd.to_datetime(t["date"])
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
    t = t[(t["date"].dt.dayofweek != 1) & (t["mom5_prev"] < 0.0137)].dropna(subset=["mom5_prev"])
    t["date"] = t["date"].dt.date
    return t[(t["date"] >= WIN_LO) & (t["date"] <= WIN_HI)]


def main():
    print("loading real option bars (harvest_state + archive)...")
    bars = load_option_bars()
    exp_by_date = {d: sorted(g["expiry"].unique()) for d, g in bars.groupby("date")}
    print(f"  {len(bars):,} option bars | {bars['date'].min()} .. {bars['date'].max()}")

    spot = load_spot_1m(WIN_LO, WIN_HI)
    print(f"  {len(spot):,} 1-minute spot bars")

    t = gated_trades()
    print(f"gated trades in window: {len(t)}")

    rows = []
    for _, tr in t.iterrows():
        d = tr["date"]
        exps = [e for e in exp_by_date.get(d, []) if e >= d]
        if not exps:
            continue
        expiry = exps[0]
        strike = int(round(tr["entry"] / 50.0) * 50)
        otype = tr["dir"]
        c = bars[(bars["date"] == d) & (bars["expiry"] == expiry) &
                 (bars["strike"] == strike) & (bars["option_type"] == otype)]
        if c.empty:
            continue
        cb = c.set_index("ts").sort_index()
        prem = cb["close"]

        day_spot = spot[spot.index.date == d]
        entry_ts = pd.Timestamp(tr["entry_ts"])
        # entry premium: last real print at or before the entry minute
        pe_idx = prem.index[prem.index <= entry_ts]
        if len(pe_idx) == 0:
            continue
        prem_entry = float(prem.loc[pe_idx[-1]])
        if prem_entry <= 0:
            continue

        sign = 1.0 if otype == "CE" else -1.0
        slp, tgt = tr["sl"], tr["target"]
        decay_floor = prem_entry * DECAY_EXIT_PCT
        broker_stop = prem_entry * (1.0 - PREMIUM_SL_PCT / 100.0)

        fwd = day_spot[day_spot.index > entry_ts]
        exit_ts = exit_px = reason = None
        for ts, row in fwd.iterrows():
            held = (ts - entry_ts).total_seconds() / 60.0
            p_idx = prem.index[prem.index <= ts]
            p = float(prem.loc[p_idx[-1]]) if len(p_idx) else prem_entry
            # broker-side premium stop is autonomous: it fills whenever hit
            if p <= broker_stop:
                exit_ts, exit_px, reason = ts, broker_stop, "premium-SL"
                break
            if ts.time() >= EXIT_TIME:                       # EOD, CAS-safe
                exit_ts, exit_px, reason = ts, p, "EOD"
                break
            if sign * (row["low" if otype == "CE" else "high"] - slp) <= 0:
                exit_ts, exit_px, reason = ts, p, "SL"
                break
            if sign * (row["high" if otype == "CE" else "low"] - tgt) >= 0:
                exit_ts, exit_px, reason = ts, p, "target"
                break
            if held >= MAX_HOLD_MIN:
                exit_ts, exit_px, reason = ts, p, "max-hold"
                break
            if p < decay_floor:                              # premium decay floor
                exit_ts, exit_px, reason = ts, p, "decay"
                break
        if reason is None:
            if len(fwd) == 0:
                continue
            ts = fwd.index[-1]
            p_idx = prem.index[prem.index <= ts]
            exit_ts = ts
            exit_px = float(prem.loc[p_idx[-1]]) if len(p_idx) else prem_entry
            reason = "EOD"

        # BASE case: buy and sell at the minute's close, spread charged
        # explicitly (0.41%, vs 0.24-0.29% measured live 2026-08-06).
        costs = (2 * prem_entry * LOT) * rb.OPT_COST_PCT / 100.0 + \
            prem_entry * LOT * rb.SPREAD_PCT / 100.0
        rs_real = (exit_px - prem_entry) * LOT - costs
        # STRESS case: worst intra-minute fills -- buy at the entry minute's
        # HIGH, sell at the exit minute's LOW. The bar range subsumes the
        # bid/ask, so only statutory is charged on top (no double count).
        e_hi = float(cb.loc[pe_idx[-1], "high"])
        x_lo = float(cb.loc[cb.index[cb.index <= exit_ts][-1], "low"]) \
            if len(cb.index[cb.index <= exit_ts]) else exit_px
        rs_stress = (x_lo - e_hi) * LOT - (2 * e_hi * LOT) * rb.OPT_COST_PCT / 100.0
        rows.append({
            "date": d, "dir": otype, "strike": strike, "expiry": expiry,
            "dte": (expiry - d).days, "reason_live": reason,
            "reason_bt": tr["reason"], "entry_ts": entry_ts, "exit_ts": exit_ts,
            "held_min": round((exit_ts - entry_ts).total_seconds() / 60.0, 1),
            "prem_entry": round(prem_entry, 2), "prem_exit": round(exit_px, 2),
            "rs_delta": tr["rs"], "rs_real": rs_real, "rs_stress": rs_stress,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        print("NO TRADES PRICED")
        return
    out.to_csv(OUT, index=False)
    print(f"\npriced {len(out)}/{len(t)} trades at 1-minute resolution\n")

    print(f"{'bucket':22s} {'T':>4s} {'delta net':>11s} {'REAL net':>11s} {'REAL PF':>8s}"
          f" {'STRESS net':>11s} {'PF':>6s}")
    def line(lbl, g):
        if len(g):
            print(f"{lbl:22s} {len(g):4d} {g['rs_delta'].sum():+11,.0f} "
                  f"{g['rs_real'].sum():+11,.0f} {pf(g['rs_real']):8.2f}"
                  f" {g['rs_stress'].sum():+11,.0f} {pf(g['rs_stress']):6.2f}")
    line("ALL", out)
    line("CE", out[out["dir"] == "CE"])
    line("PE", out[out["dir"] == "PE"])
    for lo, hi in ((0, 10), (10, 21), (21, 99)):
        line(f"DTE {lo}-{hi-1}", out[(out["dte"] >= lo) & (out["dte"] < hi)])
    print("\nlive exit mix (1-minute resolution, real premiums):")
    for r, g in out.groupby("reason_live"):
        print(f"  {r:11s}: T {len(g):3d} | REAL net {g['rs_real'].sum():+9,.0f} "
              f"| PF {pf(g['rs_real']):5.2f} | median hold {g['held_min'].median():5.1f}min")
    print("\n30m-backtest exit mix for the same trades (what the delta model assumed):")
    print("  ", dict(out["reason_bt"].value_counts()))
    xtab = pd.crosstab(out["reason_bt"], out["reason_live"])
    print("\nbacktest reason (rows) vs live-faithful reason (cols):")
    print(xtab.to_string())


if __name__ == "__main__":
    main()
