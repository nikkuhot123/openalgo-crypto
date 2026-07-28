"""
Ensemble trend-following on NIFTY / SENSEX, daily, zero brokerage (Flattrade).

Motivation from the single-signal sweep (voltarget_harness.py): the only daily
mechanism that clearly beats buy&hold on risk-adjusted terms is a LONG/FLAT trend
filter (above a long MA) - Sharpe ~0.77, maxDD -16% vs buy&hold -38%. Long/short
and short lookbacks are worse (indices drift up, so shorting bleeds).

This module pushes that edge as far as it honestly goes with two legitimate,
non-overfitting levers:

  1. ENSEMBLE across lookbacks. Average the long/flat signal over several MA
     lengths {50,75,100,150,200}. This is signal diversification - each length
     catches a different trend horizon and the average has less whipsaw. It is
     the standard managed-futures construction, not a fitted parameter.

  2. VOL TARGET done responsively. Scale the (already smooth) ensemble exposure
     to a target annual vol using a causal EWMA vol estimate, capped at MAX_LEV.
     Because ensemble exposure is in [0,1] and smooth, the scaling mostly cuts
     size in high-vol crashes rather than chasing noise.

Strictly causal: signal and vol for day t use data through t-1 only; the booked
return is day t close-to-close. Cost = turnover * COST_BPS (futures slippage;
brokerage = 0 on Flattrade).

Usage:
    ../venv/Scripts/python.exe backtesting/positional/ensemble_trend.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
ANN = 252
COST_BPS = 1.0
LOOKBACKS = (50, 75, 100, 150, 200)


def load(sym):
    df = pd.read_csv(DATA / f"{sym}_daily.csv", index_col=0, parse_dates=True)
    df = df[["close"]].astype(float)
    df["ret"] = df["close"].pct_change()
    return df.dropna()


def ensemble_signal(df, lookbacks=LOOKBACKS):
    """Mean of long/flat MA filters -> smooth exposure in [0,1], lagged 1 day."""
    sigs = []
    for n in lookbacks:
        ma = df["close"].rolling(n).mean()
        sigs.append((df["close"] > ma).astype(float))
    return pd.concat(sigs, axis=1).mean(axis=1).shift(1)


def realised_vol(ret, halflife):
    return ret.shift(1).ewm(halflife=halflife, min_periods=halflife).std() * np.sqrt(ANN)


def run(df, base_pos, target_vol=None, max_lev=2.0, vol_hl=20, cost_bps=COST_BPS):
    ret = df["ret"]
    pos = base_pos.reindex(df.index).fillna(0.0)
    if target_vol is not None:
        rv = realised_vol(ret, vol_hl).reindex(df.index)
        pos = (pos * (target_vol / rv).clip(upper=max_lev)).fillna(0.0)
    pos = pos.clip(0, max_lev)
    turn = pos.diff().abs().fillna(0.0)
    sr = (pos * ret - turn * cost_bps / 1e4).dropna()
    if len(sr) < 252:
        return None, None
    eq = (1 + sr).cumprod()
    yrs = len(sr) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    m = {
        "CAGR%": round(100 * cagr, 2),
        "Sharpe": round(sr.mean() / sr.std() * np.sqrt(ANN), 2) if sr.std() > 0 else 0,
        "maxDD%": round(100 * dd, 2),
        "Calmar": round(cagr / abs(dd), 2) if dd else 0,
        "vol%": round(100 * sr.std() * np.sqrt(ANN), 1),
        "expo": round(pos.mean(), 2),
        "turn/yr": round(turn.sum() / yrs, 1),
        "ret/DD": round(cagr / abs(dd), 2) if dd else 0,
    }
    return m, sr


def show(tag, m):
    if m is None:
        print(f"{tag:34s} (insufficient)")
        return
    print(f"{tag:34s} CAGR {m['CAGR%']:>6}%  Sh {m['Sharpe']:>4}  DD {m['maxDD%']:>7}%  "
          f"Cal {m['Calmar']:>4}  vol {m['vol%']:>4}%  expo {m['expo']:>4}  t/y {m['turn/yr']:>4}  "
          f"r/DD {m['ret/DD']:>4}")


def main():
    series = {}
    for sym in ("NIFTY", "SENSEX"):
        df = load(sym)
        print(f"\n{'='*104}\n{sym}   {df.index[0].date()}..{df.index[-1].date()}")
        sig = ensemble_signal(df)
        show("ensemble long/flat (no VT)", run(df, sig)[0])
        for tv in (0.08, 0.10, 0.12, 0.15):
            for ml in (1.5, 2.0, 3.0):
                m, sr = run(df, sig, target_vol=tv, max_lev=ml)
                if tv == 0.12 and ml == 2.0:
                    series[sym] = sr
                show(f"ensemble VT={tv:.2f} lev<={ml}", m)

    # portfolio: 50/50 NIFTY + SENSEX on the VT=0.12 lev<=2 config
    if "NIFTY" in series and "SENSEX" in series:
        a, b = series["NIFTY"], series["SENSEX"]
        idx = a.index.intersection(b.index)
        port = (a.loc[idx] + b.loc[idx]) / 2
        eq = (1 + port).cumprod()
        yrs = len(port) / ANN
        cagr = eq.iloc[-1] ** (1 / yrs) - 1
        dd = (eq / eq.cummax() - 1).min()
        corr = a.loc[idx].corr(b.loc[idx])
        print(f"\n{'='*104}\n50/50 PORTFOLIO (VT=0.12, lev<=2)  daily-return corr {corr:.2f}")
        print(f"   CAGR {100*cagr:.2f}%  Sharpe {port.mean()/port.std()*np.sqrt(ANN):.2f}  "
              f"maxDD {100*dd:.2f}%  Calmar {cagr/abs(dd):.2f}  ret/DD {cagr/abs(dd):.2f}")


if __name__ == "__main__":
    main()
