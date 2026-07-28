"""
HOSTILE test of the weekday x intraday effect.

The 5m scan (tod_profile.py, 777 sessions x 4 indices) threw up:
    Monday  intraday (open->close)  +12.35 bps  t=+4.19
    Tuesday intraday (open->close)  -13.23 bps  t=-3.85
Both magnitudes exceed our ~6.7 bps round-trip cost bar, and both clear a
Bonferroni threshold for 5 weekday tests. Every other intraday structure we
measured (time-of-day blocks, gap conditioning) was BELOW cost.

Day-of-week effects are also the single most data-mined artifact in finance, so
this script tries hard to KILL the result before believing it:

  1. Power: 15 years of daily bars (~735 Mondays per index) instead of 777 days.
  2. Independent instrument: SENSEX as well as NIFTY (different exchange, and a
     different weekly-expiry day - NIFTY Thu, SENSEX Tue).
  3. Sub-period stability: per-half and per-year. A weekday effect that only
     exists in one regime is an artifact.
  4. Decomposition: is the effect in the INTRADAY leg (open->close), the
     OVERNIGHT leg (prev close->open), or both? Our established fact is that
     overnight carries the drift, so we must not accidentally re-discover that.
  5. Cost: every number in bps, netted against the round-trip bar.
  6. The actual tradable book: long intraday Monday, short intraday Tuesday,
     with realistic cost, reported as CAGR / Sharpe / maxDD.

Usage:
    ../venv/Scripts/python.exe backtesting/intraday/weekday_intraday.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
ANN = 252
COST_RT_BPS = 6.7          # 2.84 bps/side statutory + ~1 bp slippage, round trip
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def load(sym):
    df = pd.read_csv(DATA / f"{sym}_daily.csv", index_col=0, parse_dates=True)
    df = df[["open", "high", "low", "close"]].astype(float)
    df["intraday_bps"] = (df["close"] / df["open"] - 1) * 1e4
    df["overnight_bps"] = (df["open"] / df["close"].shift(1) - 1) * 1e4
    df["weekday"] = df.index.day_name()
    df["year"] = df.index.year
    return df.dropna()


def tstat(x):
    x = pd.Series(x).dropna()
    if len(x) < 20 or x.std() == 0:
        return np.nan, np.nan, 0
    return x.mean(), x.mean() / (x.std() / np.sqrt(len(x))), len(x)


def table(df, col, label):
    print(f"\n--- {label}: {col} by weekday (bps) ---")
    print(f"{'day':>10s} {'n':>5s} {'mean':>8s} {'t':>7s} {'net_vs_cost':>12s}  verdict")
    res = {}
    for wd in WEEKDAYS:
        m, t, n = tstat(df[df["weekday"] == wd][col])
        res[wd] = (m, t, n)
        net = abs(m) - COST_RT_BPS
        # Bonferroni for 5 weekday tests at 5% -> |t| > 2.58
        v = "TRADABLE" if (net > 0 and abs(t) > 2.58) else \
            ("significant but < cost" if abs(t) > 2.58 else "not significant")
        print(f"{wd:>10s} {n:>5d} {m:>8.2f} {t:>7.2f} {net:>12.2f}  {v}")
    return res


def book(df, long_days, short_days, cost=COST_RT_BPS):
    """Trade the intraday session: long on long_days, short on short_days."""
    pos = pd.Series(0.0, index=df.index)
    pos[df["weekday"].isin(long_days)] = 1.0
    pos[df["weekday"].isin(short_days)] = -1.0
    gross = pos * df["intraday_bps"]
    net = gross - (pos.abs() * cost)
    r = (net / 1e4).dropna()
    if len(r) < 100:
        return None
    eq = (1 + r).cumprod()
    yrs = len(df) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    traded = r[pos.reindex(r.index).abs() > 0]
    return {"trades": int((pos.abs() > 0).sum()),
            "CAGR%": round(100 * cagr, 2),
            "Sharpe": round(traded.mean() / traded.std() * np.sqrt(len(traded) / yrs), 2)
            if traded.std() > 0 else 0,
            "maxDD%": round(100 * dd, 2),
            "hit%": round(100 * (traded > 0).mean(), 1),
            "avg_bps": round(traded.mean() * 1e4, 2)}


def main():
    store = {}
    for sym in ("NIFTY", "SENSEX"):
        df = load(sym)
        store[sym] = df
        print(f"\n{'='*92}\n{sym}  {df.index[0].date()}..{df.index[-1].date()}  ({len(df)} days)")
        intr = table(df, "intraday_bps", f"{sym} FULL PERIOD")
        table(df, "overnight_bps", f"{sym} overnight leg (sanity - drift should live here)")

        # sub-period stability
        half = len(df) // 2
        print(f"\n--- {sym}: intraday by weekday, SPLIT HALVES ---")
        print(f"{'day':>10s} {'H1 mean':>9s} {'H1 t':>7s} {'H2 mean':>9s} {'H2 t':>7s}  stable?")
        for wd in WEEKDAYS:
            m1, t1, _ = tstat(df.iloc[:half].query("weekday == @wd")["intraday_bps"])
            m2, t2, _ = tstat(df.iloc[half:].query("weekday == @wd")["intraday_bps"])
            same = (np.sign(m1) == np.sign(m2)) and abs(t1) > 1.5 and abs(t2) > 1.5
            print(f"{wd:>10s} {m1:>9.2f} {t1:>7.2f} {m2:>9.2f} {t2:>7.2f}  "
                  f"{'YES' if same else 'no'}")

        # per year for the two candidate days
        print(f"\n--- {sym}: Monday & Tuesday intraday mean by year (bps) ---")
        for wd in ("Monday", "Tuesday"):
            row = []
            for y, g in df.groupby("year"):
                m, t, n = tstat(g[g["weekday"] == wd]["intraday_bps"])
                if n >= 20:
                    row.append(f"{y}:{m:+.0f}")
            print(f"  {wd:9s} " + " ".join(row))

    # the tradable book, on each index and combined
    print(f"\n{'='*92}\nTRADABLE BOOK: long intraday Monday, short intraday Tuesday "
          f"(cost {COST_RT_BPS} bps round trip)")
    for sym, df in store.items():
        for tag, L, S in (("Mon long only", ["Monday"], []),
                          ("Tue short only", [], ["Tuesday"]),
                          ("Mon long + Tue short", ["Monday"], ["Tuesday"])):
            m = book(df, L, S)
            if m:
                print(f"  {sym:7s} {tag:22s} {m}")


if __name__ == "__main__":
    main()
