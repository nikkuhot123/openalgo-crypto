"""
SKB Stochastic Crossover -- OpenAlgo engine backtest + parameter tuning.
=======================================================================
Strategy as specified on the SKB Trading Lab chart (NIFTY 50, 15m):

    Indicator : Stochastic (14, 3, 3)   -- %K length 14, smooth 3, %D 3
    BUY       : %K crosses ABOVE %D while in the oversold zone (< 20)
    SELL      : %K crosses BELOW %D while in the overbought zone (> 80)

The chart states its own caveat, and it is the single most important line
on the image: "Stochastic works best in Trading Ranges (Sideways Market).
In strong trending markets, use it with other tools." So a plain crossover
is expected to bleed in trends -- the tuning below tests exactly that by
adding a regime filter and measuring whether it earns its keep.

RAW REPORTING, per the standing convention:
  - fixed Rs 2,00,000 research notional, never the live balance
  - flat lot sizing, no risk-based position scaling
  - win rate / profit factor / net / max drawdown / Sharpe
  - option translation via the measured DELTA and real friction, so the
    number is what an option BUYER would actually have kept

Long signal  -> buy ATM CE
Short signal -> buy ATM PE
Exits: spot SL / target, opposite crossover (optional), EOD square-off.

Usage:
    ./venv/Scripts/python.exe backtesting/stochastic/stoch_backtest.py
    ./venv/Scripts/python.exe backtesting/stochastic/stoch_backtest.py --symbol SENSEX
    ./venv/Scripts/python.exe backtesting/stochastic/stoch_backtest.py --sweep
"""
import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "backtesting"))
from config import (  # noqa: E402
    BACKTEST_CAPITAL,
    DELTA,
    LOT,
    OPT_COST_PCT,
    PREMIUM_PCT,
    SPREAD_PCT,
)

DB = ROOT / "backtesting" / "data" / "market_cache.duckdb"
SESSION_END = 15 * 60 + 10          # flat by 15:10, ahead of the CAS freeze
ENTRY_END = 15 * 60                 # no new entries after 15:00


def load_bars(symbol, tf_min):
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute(
        "SELECT to_timestamp(timestamp)::TIMESTAMP ts, open, high, low, close "
        "FROM market_data WHERE symbol=? AND interval='5m' ORDER BY timestamp",
        [symbol],
    ).fetchdf()
    con.close()
    df = df.set_index(pd.DatetimeIndex(df.pop("ts")))
    if tf_min != 5:
        df = (df.resample(f"{tf_min}min",
                          origin=df.index[0].normalize() + pd.Timedelta("9h15m"),
                          label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
                .dropna())
    df["day"] = df.index.normalize()
    df["mins"] = df.index.hour * 60 + df.index.minute
    return df


def stochastic(df, k=14, smooth_k=3, d=3):
    """Stochastic (k, smooth_k, d) -- the chart's (14, 3, 3)."""
    ll = df["low"].rolling(k).min()
    hh = df["high"].rolling(k).max()
    rng = (hh - ll).replace(0, np.nan)
    raw_k = 100.0 * (df["close"] - ll) / rng
    pct_k = raw_k.rolling(smooth_k).mean()
    pct_d = pct_k.rolling(d).mean()
    return pct_k.values, pct_d.values


def run(df, p):
    """Event-driven replay. Returns a trade DataFrame in index points."""
    k, d = stochastic(df, p["k"], p["smooth_k"], p["d"])
    o, h, l_, c = (df[x].values for x in ("open", "high", "low", "close"))
    ts, days, mins = df.index, df["day"].values, df["mins"].values
    ema = df["close"].ewm(span=p["trend_len"], adjust=False).mean().values
    atr = (df["high"] - df["low"]).rolling(14).mean().values

    trades, pos = [], None
    for i in range(1, len(df)):
        if np.isnan(k[i]) or np.isnan(d[i]) or np.isnan(k[i - 1]):
            continue
        last_of_day = (i + 1 >= len(df)) or days[i + 1] != days[i]

        # ---- manage open position -------------------------------------
        if pos is not None:
            sign = 1.0 if pos["side"] == "CE" else -1.0
            hit = None
            long_leg = sign > 0
            # stop is taken before target when a bar spans both -- intrabar
            # order is unknowable and the optimistic pick invents edges
            if (l_[i] <= pos["sl"]) if long_leg else (h[i] >= pos["sl"]):
                hit, px = "SL", pos["sl"]
            elif (h[i] >= pos["tgt"]) if long_leg else (l_[i] <= pos["tgt"]):
                hit, px = "TGT", pos["tgt"]
            if hit is None and p["exit_on_cross"]:
                opp = (k[i] < d[i] and k[i - 1] >= d[i - 1]) if sign > 0 else \
                      (k[i] > d[i] and k[i - 1] <= d[i - 1])
                if opp:
                    hit, px = "CROSS", c[i]
            if hit is None and (last_of_day or mins[i] >= SESSION_END):
                hit, px = "EOD", c[i]
            if hit:
                pos.update(exit_ts=ts[i], exit=px, reason=hit,
                           pts=sign * (px - pos["entry"]))
                trades.append(pos)
                pos = None

        if pos is not None or last_of_day or mins[i] >= ENTRY_END:
            continue

        # ---- signals (the chart's rule) --------------------------------
        cross_up = k[i] > d[i] and k[i - 1] <= d[i - 1]
        cross_dn = k[i] < d[i] and k[i - 1] >= d[i - 1]
        long_sig = cross_up and k[i - 1] < p["oversold"]
        short_sig = cross_dn and k[i - 1] > p["overbought"]

        # regime filter: the chart says this works in RANGES, not trends
        if p["trend_filter"] == "with":        # only trade with the EMA trend
            long_sig &= c[i] > ema[i]
            short_sig &= c[i] < ema[i]
        elif p["trend_filter"] == "range":     # only trade when price hugs EMA
            band = p["range_band"] * atr[i] if not np.isnan(atr[i]) else np.inf
            near = abs(c[i] - ema[i]) <= band
            long_sig &= near
            short_sig &= near

        if not (long_sig or short_sig):
            continue
        side = "CE" if long_sig else "PE"
        entry = c[i]
        risk = p["sl_pct"] / 100.0 * entry
        sign = 1.0 if side == "CE" else -1.0
        pos = {"day": days[i], "entry_ts": ts[i], "side": side, "entry": entry,
               "sl": entry - sign * risk, "tgt": entry + sign * risk * p["rr"]}
    return pd.DataFrame(trades)


def stats(t, symbol, label=""):
    """Raw stats on the Rs 2,00,000 notional, option-translated."""
    if t.empty or len(t) < 2:
        return None
    lot = LOT.get(symbol.upper(), 65)
    pts = t["pts"]
    gw, gl = pts[pts > 0].sum(), -pts[pts < 0].sum()
    pf = gw / gl if gl > 0 else np.inf
    prem = t["entry"].mean() * PREMIUM_PCT / 100.0
    cost_rt = (2 * prem * lot) * OPT_COST_PCT / 100.0 + prem * lot * SPREAD_PCT / 100.0
    rs = pts * (DELTA * lot) - cost_rt
    lots = max(int((BACKTEST_CAPITAL * 0.5) // (prem * lot)), 1)
    eq = (rs * lots).cumsum()
    dd = (eq.cummax() - eq).max()
    sharpe = rs.mean() / rs.std() * np.sqrt(len(rs)) if rs.std() > 0 else 0.0
    return {
        "label": label, "n": len(t), "win": 100 * (pts > 0).mean(), "pf": pf,
        "net": rs.sum() * lots, "dd": dd, "dd_pct": 100 * dd / BACKTEST_CAPITAL,
        "sharpe": sharpe, "ret_pct": 100 * rs.sum() * lots / BACKTEST_CAPITAL,
        "lots": lots, "be_pts": cost_rt / (DELTA * lot),
    }


def line(s):
    return (f"  {s['label']:26s} n={s['n']:4d}  win={s['win']:4.1f}%  PF={s['pf']:4.2f}  "
            f"net={s['net']:+9,.0f}  maxDD={s['dd']:7,.0f} ({s['dd_pct']:5.1f}%)  "
            f"Sharpe={s['sharpe']:5.2f}  {s['ret_pct']:+7.1f}% of 2L")


BASE = dict(k=14, smooth_k=3, d=3, oversold=20, overbought=80, sl_pct=0.25,
            rr=2.0, exit_on_cross=True, trend_filter="none", trend_len=50,
            range_band=1.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()

    print(f"SKB Stochastic Crossover (14,3,3) -- {a.symbol}")
    print(f"Rs {BACKTEST_CAPITAL:,} notional | flat lots | option-translated "
          f"(delta {DELTA}, {OPT_COST_PCT}%x2 + {SPREAD_PCT}% spread)\n")

    if not a.sweep:
        for tf in (5, 15, 30):
            df = load_bars(a.symbol, tf)
            s = stats(run(df, BASE), a.symbol, f"{tf}m chart-default")
            print(f"--- {tf}m | {len(df):,} bars | {df.index[0]:%Y-%m-%d}..{df.index[-1]:%Y-%m-%d}")
            print(line(s) if s else "  too few trades")
        return 0

    # ---- tuning sweep --------------------------------------------------
    rows = []
    for tf in (5, 15, 30):
        df = load_bars(a.symbol, tf)
        for zone in ((20, 80), (25, 75), (30, 70)):
            for filt in ("none", "with", "range"):
                for rr in (1.5, 2.0, 3.0):
                    for sl in (0.20, 0.35):
                        p = dict(BASE, oversold=zone[0], overbought=zone[1],
                                 trend_filter=filt, rr=rr, sl_pct=sl)
                        s = stats(run(df, p), a.symbol,
                                  f"{tf}m z{zone[0]} {filt} rr{rr} sl{sl}")
                        if s and s["n"] >= 30:
                            s["tf"], s["zone"] = tf, zone[0]
                            s["filt"], s["rr"], s["sl"] = filt, rr, sl
                            rows.append(s)
    if not rows:
        print("no configuration produced a usable sample")
        return 1
    r = pd.DataFrame(rows).sort_values("net", ascending=False)
    print(f"=== top 12 of {len(r)} configurations, ranked by net ===")
    for _, s in r.head(12).iterrows():
        print(line(s))
    print(f"\n=== worst 3 (for the honest spread) ===")
    for _, s in r.tail(3).iterrows():
        print(line(s))
    print(f"\nconfigurations profitable: {(r['net'] > 0).sum()} / {len(r)}")
    r.to_csv(HERE / f"sweep_{a.symbol}.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
