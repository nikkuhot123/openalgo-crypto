"""
Red Bar / X-Candle — OVERNIGHT HOLD exploration
================================================
The intraday version is dead (forward window PF 0.94 gated, 0.61 ungated).
But its winners cluster in the 90-minute max-hold bucket and its losers in
fast stop-outs, which is the signature of a signal whose move takes longer
than the session allows. So: does the signal carry overnight?

Step 1 asks the only question that matters first -- does the signal predict
SPOT direction overnight at all? If the raw spot expectancy is not clearly
positive, no option structure can rescue it, because holding an option
overnight costs real theta on top.

Horizons measured, all in the signal's direction, from the signal bar's close:
    gap    : next session open        (pure overnight gap)
    d1_1510: next session 15:10       (gap + next day's move, CAS-safe exit)
    d2_1510: second session 15:10
    d1_best: next session's best excursion (MFE, upper bound on any exit rule)

Reported separately for the fitted range (<= 2026-05-27) and the untouched
forward window (> 2026-05-27), because everything in this study was fitted
inside the former.

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/redbar_overnight.py
"""
import os
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

ROOT = Path(__file__).resolve().parent.parent.parent
LOT = 65
DELTA = rb.DELTA          # 0.358 measured
CUTOFF = datetime(2026, 5, 27).date()
CALIB = 1.185             # real = 1.185 x delta (n=31, CI [0.936, 1.438])


def fetch_5m_live(start, end):
    from openalgo import api
    env = (ROOT / ".env").read_text()
    key = env.split("OPENALGO_API_KEY=")[1].split()[0]
    host = env.split("OPENALGO_HOST=")[1].split()[0]
    c = api(api_key=key, host=host)
    df = c.history(symbol="NIFTY", exchange="NSE_INDEX", interval="5m",
                   start_date=start, end_date=end)
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[["open", "high", "low", "close"]].sort_index()


def load_full_5m():
    """Cached history (to 2026-05-27) + fresh API bars after it."""
    hist = rb.load_bars("NIFTY")
    fresh = fetch_5m_live("2026-05-20", datetime.now().date().isoformat())
    fresh = fresh[fresh.index.date > hist.index.date.max()]
    return pd.concat([hist, fresh]).sort_index()


def signals(df5):
    """Gated Red Bar signals with entry spot and direction, one per day."""
    m = rb.load_strategy()
    m.EXIT_TIME = pd.to_datetime("2026-01-01 15:10").time()
    m.MAX_HOLD_MINUTES, m.RR, m.MAX_SL_PCT = 90, 3.0, 0.80
    d30 = rt.resample(df5, 30)
    t = rb.backtest(m, d30, "NIFTY", LOT, 0.55)
    t["date"] = pd.to_datetime(t["date"])
    daily = daily_features(df5)
    daily["mom5_prev"] = daily["mom5"].shift(1)
    t["mom5_prev"] = [daily.loc[pd.Timestamp(d), "mom5_prev"]
                      if pd.Timestamp(d) in daily.index else np.nan for d in t["date"]]
    t["gated"] = (t["date"].dt.dayofweek != 1) & (t["mom5_prev"] < 0.0137)
    return t


def main():
    df5 = load_full_5m()
    print(f"5m bars: {len(df5):,} | {df5.index.min().date()} .. {df5.index.max().date()}")
    t = signals(df5)
    print(f"signals: {len(t)} ({int(t['gated'].sum())} gated)")

    by_day = {d: g for d, g in df5.groupby(df5.index.date)}
    days = sorted(by_day)
    pos = {d: i for i, d in enumerate(days)}

    rows = []
    for _, tr in t.iterrows():
        d = tr["date"].date()
        i = pos.get(d)
        if i is None or i + 2 >= len(days):
            continue
        side = 1.0 if tr["dir"] == "CE" else -1.0
        entry = tr["entry"]
        d1, d2 = by_day[days[i + 1]], by_day[days[i + 2]]
        today_close = by_day[d]["close"].iloc[-1]

        def at_1510(day_df):
            upto = day_df[day_df.index.time <= dtime(15, 10)]
            return upto["close"].iloc[-1] if len(upto) else day_df["close"].iloc[-1]

        d1_open = d1["open"].iloc[0]
        best = d1["high"].max() if side > 0 else d1["low"].min()
        rows.append({
            "date": d, "dir": tr["dir"], "gated": tr["gated"],
            "entry": entry, "close": today_close,
            "gap": side * (d1_open - today_close),
            "d1_1510": side * (at_1510(d1) - today_close),
            "d2_1510": side * (at_1510(d2) - today_close),
            "d1_best": side * (best - today_close),
            "intraday_rs": tr["rs"],
        })
    r = pd.DataFrame(rows)
    r["fitted"] = r["date"] <= CUTOFF

    print("\nSPOT points in the signal's direction, from the signal day's close")
    print("(no costs, no theta -- this is the raw directional question)\n")
    hdr = f"{'window':10s} {'set':8s} {'T':>4s} " + " ".join(f"{h:>9s}" for h in
                                                             ("gap", "d1_1510", "d2_1510", "d1_best"))
    print(hdr)
    print("-" * len(hdr))
    for wl, wsel in (("fitted", r["fitted"]), ("forward", ~r["fitted"])):
        for sl, ssel in (("all", pd.Series(True, index=r.index)), ("gated", r["gated"])):
            g = r[wsel & ssel]
            if not len(g):
                continue
            cells = " ".join(f"{g[c].mean():+9.2f}" for c in
                             ("gap", "d1_1510", "d2_1510", "d1_best"))
            print(f"{wl:10s} {sl:8s} {len(g):4d} {cells}")

    print("\nhit rate (share of trades where the horizon closed in our favour):")
    for wl, wsel in (("fitted", r["fitted"]), ("forward", ~r["fitted"])):
        g = r[wsel & r["gated"]]
        if not len(g):
            continue
        cells = " ".join(f"{(g[c] > 0).mean():8.1%}" for c in
                         ("gap", "d1_1510", "d2_1510", "d1_best"))
        print(f"  {wl:8s} gated T={len(g):4d}  {cells}")

    # Rupees: delta model on the overnight horizons. Theta is NOT charged here;
    # it is measured separately (redbar_overnight_theta) because an overnight
    # option loses a full day of time value, which intraday holds never pay.
    print("\ndelta-model rupees per trade (1 lot, no theta charge, no costs):")
    for wl, wsel in (("fitted", r["fitted"]), ("forward", ~r["fitted"])):
        g = r[wsel & r["gated"]]
        if not len(g):
            continue
        cells = " ".join(f"{g[c].mean() * DELTA * LOT:+9.0f}" for c in
                         ("gap", "d1_1510", "d2_1510", "d1_best"))
        print(f"  {wl:8s} gated T={len(g):4d}  {cells}")

    r.to_csv(Path(__file__).parent / "redbar_overnight_horizons.csv", index=False)
    print(f"\nintraday baseline for the same trades: "
          f"fitted Rs {r[r['fitted'] & r['gated']]['intraday_rs'].sum():+,.0f} | "
          f"forward Rs {r[~r['fitted'] & r['gated']]['intraday_rs'].sum():+,.0f}")


if __name__ == "__main__":
    main()
