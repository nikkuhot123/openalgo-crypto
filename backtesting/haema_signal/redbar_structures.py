"""
Red Bar / X-Candle — can ANY structure make this signal pay?
=============================================================
Established already:
  - intraday long options: forward PF 0.94 gated / 0.61 ungated -> no edge
  - overnight long options: the +15.6 pt gap edge is cancelled almost exactly
    by the measured -8.06 pt (-Rs 524/lot) overnight cost on DTE 5-9 weeklies

That leaves three structural questions, each tested on the fitted range
(<= 2026-05-27) AND the untouched forward window (> 2026-05-27):

  1. INVERSE -- if the signal is reliably wrong, fade it.
  2. FUTURES -- options cost theta and 0.41% spread; futures cost neither.
     A signal with a thin spot edge can survive in futures and die in
     options. Full delta 1.0 instead of the measured 0.358.
  3. SHORT PREMIUM -- the mirror of the overnight finding: sell the ATM
     straddle overnight and collect what the buyer pays. This is NOT the Red
     Bar strategy; it is what the data says has positive expectancy. Tail
     risk is reported honestly.

Cost models
  options : statutory 0.12% x2 + spread 0.41% of premium (no brokerage)
  futures : STT 0.0125% on the sell leg + ~0.004% exchange/stamp both legs
            + 0.25 index points of spread, on notional (no brokerage)

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_structures.py
"""
import os
import sqlite3
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")
sys.path.insert(0, str(Path(__file__).parent))

import redbar_backtest as rb
import redbar_trail_backtest as rt
from redbar_features import daily_features
from redbar_overnight import load_full_5m

ROOT = Path(__file__).resolve().parent.parent.parent
HARVEST = ROOT / "harvest_state.db"
ARCHIVE = ROOT / "harvest_options_archive.db"
LOT = 65
CUTOFF = datetime(2026, 5, 27).date()

FUT_STT = 0.0125 / 100.0      # sell leg only
FUT_MISC = 0.004 / 100.0      # exchange + stamp + sebi, both legs
FUT_SPREAD_PTS = 0.25


def pf(x):
    w = x[x > 0].sum()
    gl = abs(x[x <= 0].sum())
    return w / gl if gl else 99.0


def summarize(lbl, s):
    if not len(s):
        return
    print(f"  {lbl:26s} T {len(s):4d} | net Rs {s.sum():+9,.0f} | mean Rs {s.mean():+7,.0f} "
          f"| PF {pf(s):5.2f} | win {(s > 0).mean():5.1%}")


def gated_trades():
    df5 = load_full_5m()
    m = rb.load_strategy()
    m.EXIT_TIME = pd.to_datetime("2026-01-01 15:10").time()
    m.MAX_HOLD_MINUTES, m.RR, m.MAX_SL_PCT = 90, 3.0, 0.80
    t = rb.backtest(m, rt.resample(df5, 30), "NIFTY", LOT, 0.55)
    t["date"] = pd.to_datetime(t["date"])
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
    t["gated"] = (t["date"].dt.dayofweek != 1) & (t["mom5_prev"] < 0.0137)
    t["fitted"] = t["date"].dt.date <= CUTOFF
    return t


def futures_pnl(row):
    """Same signal, same entry/exit prices, traded as an index future."""
    gross = row["pts"] * LOT
    notional_in = row["entry"] * LOT
    notional_out = row["exit"] * LOT
    cost = notional_out * FUT_STT + (notional_in + notional_out) * FUT_MISC \
        + FUT_SPREAD_PTS * LOT
    return gross - cost


def main():
    t = gated_trades()
    g = t[t["gated"]]
    print(f"gated trades: {len(g)} ({int(g['fitted'].sum())} fitted, "
          f"{int((~g['fitted']).sum())} forward)\n")

    print("1. INVERSE the signal (long options, same exits)")
    for wl, sel in (("fitted", g["fitted"]), ("forward", ~g["fitted"])):
        summarize(f"{wl}: as-is", g[sel]["rs"])
        summarize(f"{wl}: inverted", -g[sel]["rs"])

    print("\n2. FUTURES instead of options (delta 1.0, no theta, no premium spread)")
    fut = g.apply(futures_pnl, axis=1)
    for wl, sel in (("fitted", g["fitted"]), ("forward", ~g["fitted"])):
        summarize(f"{wl}: options (delta .358)", g[sel]["rs"])
        summarize(f"{wl}: futures (delta 1.0)", fut[sel])
    print("  note: NIFTY futures need ~Rs 1.2-1.5L margin per lot, so a 5-6L")
    print("        account can carry 1-2 lots at most, vs ~Rs 13k per option lot.")

    print("\n3. SHORT the overnight ATM straddle (the mirror of the buyer's night cost)")
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
        except Exception:
            pass
        con.close()
    bars = pd.concat(frames, ignore_index=True)
    bars["ts"] = pd.to_datetime(bars["timestamp"].str.replace("+05:30", ""))
    bars["date"] = bars["ts"].dt.date
    bars["expiry"] = pd.to_datetime(bars["expiry"]).dt.date
    bars["strike"] = bars["strike"].astype(int)
    bars = bars.dropna(subset=["expiry", "close"])

    bars = bars.drop_duplicates(subset=["ts", "expiry", "strike", "option_type"])

    import duckdb
    con = duckdb.connect(str(ROOT / "backtesting/data/market_cache.duckdb"), read_only=True)
    sp = con.execute("select timestamp, close from market_data where symbol='NIFTY' "
                     "and interval='1m' order by timestamp").df()
    con.close()
    sp["ts"] = pd.to_datetime(sp["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
    spot = sp.set_index("ts")["close"].sort_index()

    def at(series, day, t_):
        tgt = pd.Timestamp(day) + pd.Timedelta(hours=t_.hour, minutes=t_.minute)
        idx = series.index[(series.index.date == day) & (series.index <= tgt)]
        return float(series.loc[idx[-1]]) if len(idx) else None

    dates = sorted(bars["date"].unique())
    exp_by_date = {d: sorted(x["expiry"].unique()) for d, x in bars.groupby("date")}
    nights = []
    for i in range(len(dates) - 1):
        d, nxt = dates[i], dates[i + 1]
        s_now = at(spot, d, dtime(15, 10))
        if s_now is None:
            continue
        exps = [e for e in exp_by_date.get(d, []) if e > nxt]
        if not exps:
            continue
        expiry, atm = exps[0], int(round(s_now / 50.0) * 50)
        legs = {}
        for otype in ("CE", "PE"):
            c = bars[(bars["expiry"] == expiry) & (bars["strike"] == atm) &
                     (bars["option_type"] == otype)]
            if c.empty:
                continue
            ser = c.set_index("ts")["close"].sort_index()
            p0, p1 = at(ser, d, dtime(15, 10)), at(ser, nxt, dtime(9, 20))
            if p0 and p1:
                legs[otype] = (p0, p1)
        if len(legs) == 2:
            credit = legs["CE"][0] + legs["PE"][0]
            debit = legs["CE"][1] + legs["PE"][1]
            cost = (credit + debit) * LOT * rb.OPT_COST_PCT / 100.0 \
                + credit * LOT * rb.SPREAD_PCT / 100.0
            nights.append({"date": d, "dte": (expiry - d).days,
                           "credit": credit, "pnl": (credit - debit) * LOT - cost})
    n = pd.DataFrame(nights)
    if len(n):
        s = n["pnl"]
        print(f"  short ATM straddle, 15:10 -> next 09:20, 1 lot each leg, {len(n)} nights")
        print(f"    net Rs {s.sum():+,.0f} | mean Rs {s.mean():+,.0f}/night | "
              f"win {(s > 0).mean():.1%} | PF {pf(s):.2f}")
        print(f"    sd Rs {s.std():,.0f} | WORST NIGHT Rs {s.min():,.0f} | "
              f"best Rs {s.max():,.0f}")
        worst5 = s.nsmallest(5).sum()
        print(f"    5 worst nights alone: Rs {worst5:,.0f} "
              f"({worst5 / s.sum() * 100:.0f}% of total profit)" if s.sum() > 0 else "")
        for lo, hi in ((0, 5), (5, 10), (10, 40)):
            gg = n[(n["dte"] >= lo) & (n["dte"] < hi)]
            if len(gg) > 5:
                print(f"    DTE {lo:2d}-{hi-1:2d}: n {len(gg):3d} | mean Rs "
                      f"{gg['pnl'].mean():+6,.0f} | worst Rs {gg['pnl'].min():8,.0f}")
        print("    margin: a short straddle needs ~Rs 1.5-2L per lot pair (SPAN+exposure)")


if __name__ == "__main__":
    main()
