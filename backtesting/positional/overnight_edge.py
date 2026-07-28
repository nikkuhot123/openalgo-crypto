"""
Overnight vs intraday decomposition on NIFTY / SENSEX + trend-gated overnight book.

Hypothesis (one of the most robust global equity anomalies, strong in India):
almost all index drift accrues OVERNIGHT (prev close -> today open); the intraday
session (today open -> today close) is roughly flat or negative. If true, an
overnight-only book has a far higher Sharpe than buy&hold, and - critically -
with Flattrade's ZERO brokerage it is actually tradable (one entry at close, one
exit at open, every day).

Decomposition, per day t (all causal, no lookahead - these are realised returns):
    overnight_t = open_t  / close_{t-1} - 1
    intraday_t  = close_t / open_t      - 1
    (1+overnight)(1+intraday) = close_t/close_{t-1} = full daily return

Books tested:
    ON        : hold overnight every day
    ID        : hold intraday every day
    ON_trend  : hold overnight only when the MA-ensemble trend filter is long
    ON_trend_VT: the above, vol-targeted

Cost: each held session = 1 unit turnover in and out. Futures/index slippage only
(brokerage 0). Overnight trading pays the bid/ask twice a day, so COST_BPS matters
more here - swept below.

Usage:
    ../venv/Scripts/python.exe backtesting/positional/overnight_edge.py
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
    df["intraday"] = df["close"] / df["open"] - 1
    df["daily"] = df["close"].pct_change()
    return df.dropna()


def stats(sr, cost_units=None, cost_bps=0.0):
    sr = sr.dropna()
    if cost_units is not None:
        sr = sr - cost_units.reindex(sr.index).fillna(0) * cost_bps / 1e4
    if len(sr) < 252 or sr.std() == 0:
        return None
    eq = (1 + sr).cumprod()
    yrs = len(sr) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR%": round(100 * cagr, 2),
            "Sharpe": round(sr.mean() / sr.std() * np.sqrt(ANN), 2),
            "maxDD%": round(100 * dd, 2),
            "Calmar": round(cagr / abs(dd), 2) if dd else 0,
            "hit%": round(100 * (sr > 0).mean(), 1),
            "ret/DD": round(cagr / abs(dd), 2) if dd else 0}


def ens_trend(df):
    sigs = [(df["close"] > df["close"].rolling(n).mean()).astype(float) for n in LOOKBACKS]
    return pd.concat(sigs, axis=1).mean(axis=1).shift(1)


def realised_vol(ret, hl=20):
    return ret.shift(1).ewm(halflife=hl, min_periods=hl).std() * np.sqrt(ANN)


def show(tag, m):
    if m is None:
        print(f"{tag:32s} (insufficient)")
        return
    print(f"{tag:32s} CAGR {m['CAGR%']:>6}%  Sharpe {m['Sharpe']:>4}  maxDD {m['maxDD%']:>7}%  "
          f"Calmar {m['Calmar']:>4}  hit {m['hit%']:>4}%  r/DD {m['ret/DD']:>4}")


def main():
    for sym in ("NIFTY", "SENSEX"):
        df = load(sym)
        print(f"\n{'='*100}\n{sym}   {df.index[0].date()}..{df.index[-1].date()}  ({len(df)} days)")

        show("buy&hold (full day)", stats(df["daily"]))
        show("overnight only (gross)", stats(df["overnight"]))
        show("intraday only (gross)", stats(df["intraday"]))

        # cost sensitivity on the overnight book (in+out each day = 2 units/day)
        cost_units = pd.Series(2.0, index=df.index)
        for cb in (1.0, 2.0, 3.0, 5.0):
            show(f"overnight net @ {cb:.0f}bps/side", stats(df["overnight"], cost_units, cb))

        # trend-gated overnight (only carry overnight when trend is up)
        sig = ens_trend(df)
        on_trend = sig * df["overnight"]
        turn_gate = sig.diff().abs().fillna(0) + sig * 2.0   # gate change + daily in/out
        show("ON trend-gated @ 2bps", stats(on_trend, turn_gate, 2.0))

        # vol-targeted trend-gated overnight
        rv = realised_vol(df["overnight"], 20)
        for tv in (0.06, 0.08, 0.10):
            for ml in (2.0, 3.0):
                lev = (tv / rv).clip(upper=ml)
                pos = (sig * lev).fillna(0)
                on_vt = pos * df["overnight"]
                turn = pos.diff().abs().fillna(0) + pos.abs() * 2.0
                show(f"ON trend+VT={tv:.2f} lev<={ml} @2bps",
                     stats(on_vt, turn, 2.0))


if __name__ == "__main__":
    main()
