"""
Dr Devendra Smart Renko Engine PRO -- Stock Intraday Backtest.
============================================================
Replays the exact same Renko engine signals on Cash Intraday (MIS) stocks.

Unlike index options where friction is 4% of premium, stock cash trading has
extremely low friction -- roughly 0.035% of turnover (no brokerage, 0.025% STT
on sell, transaction charges, GST, stamp duty). A strategy with a 2.0% brick
size (the stock default) has targets of 2-4% of stock price, making friction a
fraction of the target.

This backtest measures if the directional edge exists on liquid stocks under a
professional risk-sizing model:
  - Capital: Rs 2,00,000 (fixed research notional)
  - Risk: 1% per trade (Rs 2,000) based on entry-to-SL distance
  - Leverage: 5x MIS limit (max position value Rs 10,000,000)
  - Friction: 0.035% of turnover

Symbols: RELIANCE, SBIN, HDFCBANK, ICICIBANK, TCS
Timeframes: 15m and 30m (yfinance intraday data limit is 60 days)

Usage:
    ./venv/Scripts/python.exe backtesting/renko_engine/stock_backtest.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from renko_engine_backtest import Renko, Day, Trade, run  # noqa: E402
sys.path.insert(0, str(HERE.parent))
from config import BACKTEST_CAPITAL  # noqa: E402

STOCKS = ["RELIANCE.NS", "SBIN.NS", "HDFCBANK.NS", "ICICIBANK.NS", "TCS.NS"]
RISK_PER_TRADE = BACKTEST_CAPITAL * 0.01  # Rs 2,000 (1%)
LEVERAGE = 5.0
MAX_VAL = BACKTEST_CAPITAL * LEVERAGE  # Rs 1,000,000
COST_PCT = 0.035                       # % of turnover round-trip


def fetch_stock(ticker, tf_str):
    print(f"Downloading {ticker}...")
    df = yf.download(ticker, period="60d", interval=tf_str, progress=False)
    if df.empty:
        return None
    # yfinance multi-index columns fix
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df.index.name = "ts"
    df["day"] = df.index.normalize()
    df["mins"] = df.index.hour * 60 + df.index.minute
    # lowercase columns to match backtest engine
    df = df.rename(columns={c: c.lower() for c in df.columns})
    return df


def stock_pnl(t):
    """Compute Rupees P&L under a raw flat-size allocation (no risk-sizing)."""
    if t.empty:
        return pd.DataFrame()
    t = t.copy()
    # No risk sizing: flat position value of Rs 1,00,000 per trade
    TRADE_VALUE = 100000.0
    t["qty"] = (TRADE_VALUE / t["entry"]).astype(int)

    t["turnover"] = (t["entry"] + t["exit"].abs()) * t["qty"]
    t["friction"] = t["turnover"] * COST_PCT / 100.0
    # gross points profit: 50% target 1, 50% target 2 or stop
    t["gross"] = t["pts"] * t["qty"]
    t["net"] = t["gross"] - t["friction"]
    return t


def main():
    print(f"Dr Devendra Smart Renko Engine PRO -- Stock Intraday Study (RAW)")
    print(f"Capital: Rs {BACKTEST_CAPITAL:,} | Position Size: Rs 1,00,000 flat per trade")
    print(f"Friction: {COST_PCT}% of turnover | yfinance 60-day limit\n")

    for tf in [15, 30]:
        tf_str = f"{tf}m"
        print(f"================== {tf_str} ==================")
        summary = []
        for s in STOCKS:
            df = fetch_stock(s, tf_str)
            if df is None:
                continue
            raw = run(df, s)
            t = stock_pnl(raw)
            if t.empty:
                print(f"  {s:12s} no trades")
                continue
            gw, gl = t.loc[t["net"] > 0, "net"].sum(), -t.loc[t["net"] < 0, "net"].sum()
            pf = gw / gl if gl > 0 else np.inf
            win = (t["net"] > 0).mean() * 100

            # Raw stats: drawdown on Rs 2,00,000 capital and trade Sharpe
            eq = t["net"].cumsum() + BACKTEST_CAPITAL
            dd = (eq.cummax() - eq).max()
            dd_pct = (dd / BACKTEST_CAPITAL) * 100.0
            sharpe = (t["net"].mean() / t["net"].std() * np.sqrt(len(t))) if len(t) > 1 and t["net"].std() > 0 else 0.0

            print(f"  {s:12s} n={len(t):3d}  win={win:4.1f}%  PF={pf:4.2f}  "
                  f"net={t['net'].sum():+7,.0f}  avg={t['net'].mean():+5.0f}  "
                  f"maxDD={dd:5,.0f} ({dd_pct:4.1f}%)  Sharpe={sharpe:5.2f}")
            summary.append(t)
        if not summary:
            continue
        all_t = pd.concat(summary).sort_index()
        aw = (all_t["net"] > 0).mean() * 100
        agw = all_t.loc[all_t["net"] > 0, "net"].sum()
        agl = -all_t.loc[all_t["net"] < 0, "net"].sum()
        apf = agw / agl if agl > 0 else np.inf

        # Portfolio level stats
        eq = all_t["net"].cumsum() + BACKTEST_CAPITAL
        dd = (eq.cummax() - eq).max()
        dd_pct = (dd / BACKTEST_CAPITAL) * 100.0
        asharpe = (all_t["net"].mean() / all_t["net"].std() * np.sqrt(len(all_t))) if len(all_t) > 1 and all_t["net"].std() > 0 else 0.0

        print(f"  {'ALL':12s} n={len(all_t):3d}  win={aw:4.1f}%  PF={apf:4.2f}  "
              f"net={all_t['net'].sum():+7,.0f}  avg={all_t['net'].mean():+5.0f}  "
              f"maxDD={dd:5,.0f} ({dd_pct:4.1f}%)  Sharpe={asharpe:5.2f}  "
              f"({all_t['net'].sum() / BACKTEST_CAPITAL * 100:+.1f}% on 2L)")
        print()



if __name__ == "__main__":
    sys.exit(main())
