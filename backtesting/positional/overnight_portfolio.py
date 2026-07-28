"""
Squeeze more from the overnight-drift edge WITHOUT parameter torture, by adding
genuinely uncorrelated return streams. Two theory-grounded levers:

  A. MULTI-INDEX overnight basket. NIFTY/SENSEX correlate 0.98 (same large-caps,
     no diversification). BANKNIFTY (banks only) and MIDCAP (different cap tier)
     have different composition, so their OVERNIGHT books may decorrelate. An
     equal-RISK basket of decorrelated streams lifts Sharpe for free - the only
     free lunch in finance.

  B. INTRADAY-SHORT complement. Intraday Sharpe is NEGATIVE on every index
     (-1.1 NIFTY, -1.6 SENSEX). Shorting the open->close session, trend-gated, is
     a structurally OPPOSITE stream to the overnight long. If it stands alone and
     decorrelates from the overnight book, the combined book is stronger.

Each per-index overnight book is the deliverable from overnight_drift_strategy.py:
ensemble long/flat trend gate * inverse-vol size, held close->open. Costs at the
Flattrade-verified 2.84 bps/side statutory + 0.5 bp slip = ~3.34 bps/side.

Equal-risk basket: weight each stream by 1/vol (risk parity), renormalised, then
the whole basket vol-targeted. Strictly causal throughout.

Usage:
    ../venv/Scripts/python.exe backtesting/positional/overnight_portfolio.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
ANN = 252
LOOKBACKS = (50, 75, 100, 150, 200)
COST_SIDE = 3.34          # Flattrade statutory 2.84 + 0.5bp slippage
UNIVERSE = ("NIFTY", "SENSEX", "BANKNIFTY", "MIDCAP50", "MIDCAP100")


def load(sym):
    df = pd.read_csv(DATA / f"{sym}_daily.csv", index_col=0, parse_dates=True)
    df = df[["open", "high", "low", "close"]].astype(float)
    df["on"] = df["open"] / df["close"].shift(1) - 1
    df["id"] = df["close"] / df["open"] - 1
    return df.dropna()


def trend(close):
    flags = [(close > close.rolling(n).mean()).astype(float) for n in LOOKBACKS]
    return pd.concat(flags, axis=1).mean(axis=1).shift(1)


def rvol(ret, hl=20):
    return ret.shift(1).ewm(halflife=hl, min_periods=hl).std() * np.sqrt(ANN)


def overnight_stream(df, target_vol=0.04, ml=2.0, cost=COST_SIDE):
    """Per-index overnight book return series (net), long/flat."""
    sig = trend(df["close"])
    pos = (sig * (target_vol / rvol(df["on"])).clip(upper=ml)).clip(0, ml).fillna(0)
    turn = pos.diff().abs().fillna(0) + pos.abs() * 2.0
    return (pos * df["on"] - turn * cost / 1e4).rename("r")


def intraday_short_stream(df, target_vol=0.04, ml=2.0, cost=COST_SIDE):
    """Short the intraday session when trend is DOWN (close < MAs). Structurally
    opposite to the overnight long. Short/flat: position in [-ml, 0]."""
    flags = [(df["close"] < df["close"].rolling(n).mean()).astype(float) for n in LOOKBACKS]
    bear = pd.concat(flags, axis=1).mean(axis=1).shift(1)     # [0,1] = how bearish
    pos = (-bear * (target_vol / rvol(df["id"])).clip(upper=ml)).clip(-ml, 0).fillna(0)
    turn = pos.diff().abs().fillna(0) + pos.abs() * 2.0
    return (pos * df["id"] - turn * cost / 1e4).rename("r")


def perf(sr):
    sr = sr.dropna()
    if len(sr) < 252 or sr.std() == 0:
        return None
    eq = (1 + sr).cumprod()
    yrs = len(sr) / ANN
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR%": round(100 * cagr, 2), "Sharpe": round(sr.mean() / sr.std() * np.sqrt(ANN), 2),
            "maxDD%": round(100 * dd, 2), "Calmar": round(cagr / abs(dd), 2) if dd else 0,
            "ret/DD": round(cagr / abs(dd), 2) if dd else 0}


def risk_parity(streams, target_vol=0.10, ml=2.0):
    """Combine return streams by inverse-vol weights, then vol-target the basket."""
    df = pd.concat(streams, axis=1).dropna()
    df.columns = range(df.shape[1])
    inv = 1.0 / df.rolling(60, min_periods=60).std().shift(1)
    w = inv.div(inv.sum(axis=1), axis=0).fillna(0)
    raw = (w * df).sum(axis=1)
    scale = (target_vol / (raw.shift(1).ewm(halflife=20, min_periods=20).std() * np.sqrt(ANN))).clip(upper=ml)
    return (raw * scale).dropna()


def show(tag, m):
    if m is None:
        print(f"  {tag:34s} (insufficient)")
    else:
        print(f"  {tag:34s} CAGR {m['CAGR%']:>6}%  Sharpe {m['Sharpe']:>4}  "
              f"maxDD {m['maxDD%']:>7}%  Calmar {m['Calmar']:>4}")


def main():
    on_streams, id_streams = {}, {}
    print("=== per-index OVERNIGHT book (VT=0.04, 3.34bps/side) ===")
    for sym in UNIVERSE:
        try:
            df = load(sym)
        except FileNotFoundError:
            continue
        s = overnight_stream(df)
        on_streams[sym] = s
        id_streams[sym] = intraday_short_stream(df)
        show(sym, perf(s))

    print("\n=== overnight-book cross-correlation (daily net returns) ===")
    C = pd.concat(on_streams, axis=1).dropna()
    C.columns = list(on_streams)
    print(C.corr().round(2).to_string())

    print("\n=== A. equal-RISK overnight basket ===")
    for tv in (0.04, 0.06, 0.08):
        show(f"basket ALL {len(on_streams)}, VT={tv:.2f}",
             perf(risk_parity(list(on_streams.values()), target_vol=tv)))
    # best decorrelated subset: large-cap + banks + midcap (drop SENSEX dup of NIFTY)
    sub = [on_streams[s] for s in ("NIFTY", "BANKNIFTY", "MIDCAP100") if s in on_streams]
    for tv in (0.04, 0.06):
        show(f"basket NIFTY+BANK+MID, VT={tv:.2f}", perf(risk_parity(sub, target_vol=tv)))

    print("\n=== B. INTRADAY-SHORT complement (standalone) ===")
    for sym in ("NIFTY", "SENSEX", "BANKNIFTY"):
        if sym in id_streams:
            show(f"{sym} intraday-short", perf(id_streams[sym]))
    # combine overnight-long + intraday-short on the same index
    print("\n=== A+B combined: overnight-long + intraday-short, per index ===")
    for sym in ("NIFTY", "SENSEX", "BANKNIFTY"):
        if sym in on_streams:
            corr = on_streams[sym].dropna().corr(id_streams[sym].dropna())
            show(f"{sym} ON+IDshort (corr {corr:+.2f})",
                 perf(risk_parity([on_streams[sym], id_streams[sym]], target_vol=0.04)))

    print("\n=== FULL STACK: all overnight + all intraday-short, equal-risk ===")
    allstreams = list(on_streams.values()) + list(id_streams.values())
    for tv in (0.04, 0.06, 0.08):
        show(f"full stack VT={tv:.2f}", perf(risk_parity(allstreams, target_vol=tv)))


if __name__ == "__main__":
    main()
