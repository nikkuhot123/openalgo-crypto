"""
Vol-targeted daily positional research on NIFTY / SENSEX.

Why this design, given everything measured earlier in this repo:
  - Intraday option mechanisms are dead (SMC directional E[R]=-0.007R; premium
    selling is period-dependent short-vol). Not revisited.
  - Flattrade charges ZERO brokerage, so daily rebalancing costs only a few bps
    of futures slippage. That makes a POSITIONAL, frequently-rebalanced book
    viable for the first time.
  - smc/FINDINGS.md named the one untested lever with headroom: size by a real
    VOLATILITY estimate, not a trend label. Inverse-vol targeting is the
    canonical way to raise Sharpe and cap drawdown, and it is theory-motivated.

Everything here is strictly causal: the position for day t uses only data through
t-1 (a single shift), and the return booked is day t's close-to-close. No
lookahead, no in-sample peeking at the return being predicted.

Signals (each returns a raw position in [-1, +1] or {0,1}):
  tsmom_N   : sign of trailing N-day return (time-series momentum)
  ma_F_S    : +1 when fast SMA > slow SMA else 0/-1
  above_MA  : +1 when close > SMA_N else 0 (long/flat trend filter)

Overlay:
  vol target: multiply position by target_vol / realised_vol (EWMA), capped at
  MAX_LEV. Optional. This is where drawdown control comes from.

Cost model: per-rebalance turnover * COST_BPS (futures slippage; brokerage=0).

Reports CAGR, annualised Sharpe, max drawdown, Calmar, hit rate, exposure,
annual turnover, and the ret/DD ratio that decides whether the 4%/6% targets are
jointly reachable by sizing.

Usage:
    ../venv/Scripts/python.exe backtesting/positional/voltarget_harness.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
ANN = 252
COST_BPS = 1.0            # futures round-trip slippage per unit turnover, bps (brokerage=0)
TARGET_VOL = 0.10        # annualised target when vol-targeting
MAX_LEV = 3.0
VOL_HALFLIFE = 20        # EWMA halflife for realised-vol estimate (days)


def load(sym: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{sym}_daily.csv", index_col=0, parse_dates=True)
    df = df[["open", "high", "low", "close"]].astype(float)
    df["ret"] = df["close"].pct_change()
    return df.dropna()


def realised_vol(ret: pd.Series) -> pd.Series:
    """Causal EWMA daily vol, annualised. Uses returns through t-1 via shift."""
    ew = ret.shift(1).ewm(halflife=VOL_HALFLIFE, min_periods=VOL_HALFLIFE).std()
    return ew * np.sqrt(ANN)


# ---- signals: all return raw position aligned to the day whose return is booked ----
def sig_tsmom(df, n):
    mom = df["close"].pct_change(n)
    return np.sign(mom).shift(1)


def sig_above_ma(df, n):
    ma = df["close"].rolling(n).mean()
    return (df["close"] > ma).astype(float).shift(1)


def sig_ma_cross(df, f, s, long_short=True):
    fast = df["close"].rolling(f).mean()
    slow = df["close"].rolling(s).mean()
    if long_short:
        return np.where(fast > slow, 1.0, -1.0)[:, None].ravel().__class__  # placeholder
    return None


def ma_cross(df, f, s, long_short=True):
    fast = df["close"].rolling(f).mean()
    slow = df["close"].rolling(s).mean()
    pos = pd.Series(np.where(fast > slow, 1.0, (-1.0 if long_short else 0.0)), index=df.index)
    return pos.shift(1)


def backtest(df, raw_pos, vol_target=True):
    ret = df["ret"]
    pos = raw_pos.reindex(df.index).fillna(0.0)
    if vol_target:
        rv = realised_vol(ret).reindex(df.index)
        scale = (TARGET_VOL / rv).clip(upper=MAX_LEV)
        pos = (pos * scale).fillna(0.0)
    pos = pos.clip(-MAX_LEV, MAX_LEV)
    turnover = pos.diff().abs().fillna(0.0)
    cost = turnover * (COST_BPS / 1e4)
    stratret = pos * ret - cost
    stratret = stratret.dropna()
    if len(stratret) < 252:
        return None
    eq = (1 + stratret).cumprod()
    yrs = len(stratret) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = stratret.std() * np.sqrt(ANN)
    sharpe = stratret.mean() / stratret.std() * np.sqrt(ANN) if stratret.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    calmar = cagr / abs(dd) if dd != 0 else 0
    return {
        "CAGR%": round(100 * cagr, 2),
        "Sharpe": round(sharpe, 2),
        "maxDD%": round(100 * dd, 2),
        "Calmar": round(calmar, 2),
        "vol%": round(100 * vol, 1),
        "hit%": round(100 * (stratret > 0).mean(), 1),
        "expo": round(pos.abs().mean(), 2),
        "turn/yr": round(turnover.sum() / yrs, 1),
        "ret/DD": round(cagr / abs(dd), 2) if dd != 0 else 0,
    }


def buyhold(df):
    ret = df["ret"].dropna()
    eq = (1 + ret).cumprod()
    yrs = len(ret) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR%": round(100 * cagr, 2),
            "Sharpe": round(ret.mean() / ret.std() * np.sqrt(ANN), 2),
            "maxDD%": round(100 * dd, 2),
            "Calmar": round(cagr / abs(dd), 2)}


def main():
    combos = {
        "tsmom50":       lambda d: sig_tsmom(d, 50),
        "tsmom100":      lambda d: sig_tsmom(d, 100),
        "tsmom200":      lambda d: sig_tsmom(d, 200),
        "aboveMA100":    lambda d: sig_above_ma(d, 100),
        "aboveMA200":    lambda d: sig_above_ma(d, 200),
        "cross20_100":   lambda d: ma_cross(d, 20, 100, True),
        "cross50_200":   lambda d: ma_cross(d, 50, 200, True),
        "cross20_100_LO": lambda d: ma_cross(d, 20, 100, False),
    }
    for sym in ("NIFTY", "SENSEX"):
        df = load(sym)
        bh = buyhold(df)
        print(f"\n{'='*96}\n{sym}   {df.index[0].date()}..{df.index[-1].date()}  ({len(df)} days)")
        print(f"  buy&hold: CAGR {bh['CAGR%']}%  Sharpe {bh['Sharpe']}  maxDD {bh['maxDD%']}%  Calmar {bh['Calmar']}")
        print(f"{'combo':16s} {'vt':3s} {'CAGR%':>7s} {'Sharpe':>7s} {'maxDD%':>7s} {'Calmar':>7s} "
              f"{'vol%':>5s} {'hit%':>5s} {'expo':>5s} {'turn':>6s} {'ret/DD':>7s}")
        for name, fn in combos.items():
            for vt in (False, True):
                m = backtest(df, fn(df), vol_target=vt)
                if m is None:
                    continue
                print(f"{name:16s} {'Y' if vt else 'N':3s} {m['CAGR%']:>7} {m['Sharpe']:>7} "
                      f"{m['maxDD%']:>7} {m['Calmar']:>7} {m['vol%']:>5} {m['hit%']:>5} "
                      f"{m['expo']:>5} {m['turn/yr']:>6} {m['ret/DD']:>7}")


if __name__ == "__main__":
    main()
