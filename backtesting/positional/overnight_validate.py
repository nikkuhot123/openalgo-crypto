"""
Rigour pass on the overnight-drift result before believing it.

The gross numbers (overnight Sharpe 2.7 NIFTY / 3.6 SENSEX, intraday Sharpe
NEGATIVE) are large enough to demand skepticism. This script stress-tests them:

  1. SUB-PERIOD STABILITY. Split the 15y history in half and score each half
     independently. An anomaly that only exists post-2019 is a data artifact;
     one present in both halves is structural.

  2. YEAR-BY-YEAR. Overnight Sharpe every calendar year - is it ever-present or
     driven by a couple of years?

  3. HONEST INDIA COSTS. NIFTY/SENSEX futures carry statutory costs Flattrade
     cannot waive: STT 0.02% (2bps) on the SELL side, plus exchange txn ~0.2bps,
     SEBI/stamp/GST a few more. A round trip is realistically ~3bps all-in even
     with zero brokerage. The book is scored at 2/3/4/5 bps PER SIDE so the reader
     sees exactly where the edge dies.

  4. EXECUTION CAVEAT (documented, not coded): the book buys the CLOSE and sells
     the OPEN. On the cash index the open is a synthetic auction print; the
     tradable proxy is the NIFTY/SENSEX FUTURE held close->open, whose gap tracks
     the index gap closely. Slippage vs the exact print is folded into the bps
     sweep above.

Strictly causal throughout. Booked returns are realised open/close ratios.

Usage:
    ../venv/Scripts/python.exe backtesting/positional/overnight_validate.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
ANN = 252
LOOKBACKS = (50, 75, 100, 150, 200)


def load(sym):
    df = pd.read_csv(DATA / f"{sym}_daily.csv", index_col=0, parse_dates=True)
    df = df[["open", "high", "low", "close"]].astype(float)
    df["overnight"] = df["open"] / df["close"].shift(1) - 1
    df["daily"] = df["close"].pct_change()
    return df.dropna()


def sharpe(sr):
    sr = sr.dropna()
    return sr.mean() / sr.std() * np.sqrt(ANN) if len(sr) > 20 and sr.std() > 0 else np.nan


def metrics(sr):
    sr = sr.dropna()
    if len(sr) < 60 or sr.std() == 0:
        return None
    eq = (1 + sr).cumprod()
    yrs = len(sr) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR%": round(100 * cagr, 2), "Sharpe": round(sharpe(sr), 2),
            "maxDD%": round(100 * dd, 2), "Calmar": round(cagr / abs(dd), 2) if dd else 0,
            "hit%": round(100 * (sr > 0).mean(), 1), "ret/DD": round(cagr / abs(dd), 2) if dd else 0}


def ens_trend(df):
    sigs = [(df["close"] > df["close"].rolling(n).mean()).astype(float) for n in LOOKBACKS]
    return pd.concat(sigs, axis=1).mean(axis=1).shift(1)


def gated_overnight(df, cost_bps_side, vt=None, ml=2.0):
    sig = ens_trend(df)
    pos = sig.copy()
    if vt is not None:
        rv = df["overnight"].shift(1).ewm(halflife=20, min_periods=20).std() * np.sqrt(ANN)
        pos = (sig * (vt / rv).clip(upper=ml)).fillna(0)
    turn = pos.diff().abs().fillna(0) + pos.abs() * 2.0     # gate change + daily in/out
    return (pos * df["overnight"] - turn * cost_bps_side / 1e4).dropna()


def main():
    for sym in ("NIFTY", "SENSEX"):
        df = load(sym)
        print(f"\n{'='*98}\n{sym}   {df.index[0].date()}..{df.index[-1].date()}  ({len(df)} days)")

        # 1. sub-period stability of the GROSS overnight anomaly
        half = len(df) // 2
        h1, h2 = df.iloc[:half], df.iloc[half:]
        print("  [1] gross overnight Sharpe by half:")
        print(f"      H1 {h1.index[0].date()}..{h1.index[-1].date()}: {sharpe(h1['overnight']):.2f}   "
              f"intraday {sharpe(h1['close']/h1['open']-1):.2f}")
        print(f"      H2 {h2.index[0].date()}..{h2.index[-1].date()}: {sharpe(h2['overnight']):.2f}   "
              f"intraday {sharpe(h2['close']/h2['open']-1):.2f}")

        # 2. year by year
        print("  [2] gross overnight Sharpe by year:")
        yr = df.groupby(df.index.year)["overnight"].apply(sharpe).round(2)
        print("      " + "  ".join(f"{y}:{v:+.1f}" for y, v in yr.items()))
        pos_years = (yr > 0).sum()
        print(f"      positive in {pos_years}/{len(yr)} years")

        # 3. cost sensitivity of the deliverable (trend-gated + VT=0.08, lev<=2)
        print("  [3] trend-gated + VT=0.08 overnight, cost sensitivity:")
        for cb in (2.0, 3.0, 4.0, 5.0):
            m = metrics(gated_overnight(df, cb, vt=0.08, ml=2.0))
            print(f"      {cb:.0f}bps/side -> CAGR {m['CAGR%']:>6}%  Sharpe {m['Sharpe']:>4}  "
                  f"maxDD {m['maxDD%']:>6}%  Calmar {m['Calmar']:>4}  hit {m['hit%']}%")

        # 4. deliverable, split-sample at the honest 3bps/side
        print("  [4] deliverable (VT=0.08, 3bps/side) - in/out of sample:")
        for tag, d in (("H1", h1), ("H2", h2), ("FULL", df)):
            m = metrics(gated_overnight(d, 3.0, vt=0.08, ml=2.0))
            if m:
                print(f"      {tag:5s} CAGR {m['CAGR%']:>6}%  Sharpe {m['Sharpe']:>4}  "
                      f"maxDD {m['maxDD%']:>6}%  Calmar {m['Calmar']:>4}  ret/DD {m['ret/DD']}")


if __name__ == "__main__":
    main()
