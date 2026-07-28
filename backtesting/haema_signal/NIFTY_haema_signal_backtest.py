"""
HA-EMA 34 channel breakout — SIGNAL-QUALITY backtest on NIFTY.

Data: OpenAlgo (the same broker feed the live strategies trade on), so this does
NOT suffer the data-source divergence that made the third-party backtest agree
with live on only 50% of days.

SCOPE — read this before trusting the numbers:
  The live strategy BUYS WEEKLY OPTIONS on this signal. VectorBT cannot model
  strike selection, premium, theta or gamma, so this backtest measures the
  DIRECTIONAL EDGE OF THE SIGNAL ITSELF on the index (a futures-style proxy).
  It answers "does the HA-EMA breakout predict direction?" — not "what would the
  option P&L be". If the signal has no directional edge here, no amount of
  option-side tuning can rescue it, which is exactly why this test is worth running.

Live logic replicated:
  - previous-day Heikin-Ashi bias gates direction (GREEN -> long only, RED -> short only)
  - 34-EMA channel on 5-min highs/lows; breakout of the channel is the trigger
  - entry window 09:45-14:30, ONE entry per day, EOD square-off 15:15
  - stop 0.10% of spot (the MIN_SL_PCT floor deployed 2026-07-28), target = RR x stop

Usage:  ../../venv/Scripts/python.exe NIFTY_haema_signal_backtest.py
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt
from dotenv import find_dotenv, load_dotenv
from openalgo import api, ta

load_dotenv(find_dotenv(), override=False)

# ── Parameters ───────────────────────────────────────────────────────────────
SYMBOL = "NIFTY"
EXCHANGE = "NSE_INDEX"
INTERVAL = "5m"
LOOKBACK_DAYS = 400
EMA_LEN = 34
SL_PCT = 0.0010          # 0.10% of spot — deployed MIN_SL_PCT
RR = 2.0                 # target = RR x stop
ENTRY_START, ENTRY_END = "09:45", "14:30"
EOD_EXIT = "15:15"
LOT_SIZE = 65            # SEBI revised, Dec 2025
INIT_CASH = 2_000_000    # sized so 1 lot needs no leverage (avoids margin artifacts)
FEES = 0.00018           # F&O futures 0.018% (reference: Zerodha)
FIXED_FEES = 20.0        # Rs 20 per order
SKIP_EXPIRY_WEEKDAY = 1  # Tuesday = NIFTY weekly expiry; deployed skip. None to disable.

OUT = Path(__file__).resolve().parent


def fetch(symbol, exchange, interval, days):
    client = api(api_key=os.getenv("OPENALGO_API_KEY"),
                 host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))
    end = datetime.now().date()
    start = end - timedelta(days=days)
    df = client.history(symbol=symbol, exchange=exchange, interval=interval,
                        start_date=start.strftime("%Y-%m-%d"),
                        end_date=end.strftime("%Y-%m-%d"))
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise SystemExit(f"no data for {symbol} {exchange} {interval}: {df}")
    return df.sort_index()


def ha_bias_by_day(daily):
    """Heikin-Ashi close>open per day -> the NEXT day's tradable bias."""
    o, h, l, c = (daily[k].astype(float).tolist() for k in ("open", "high", "low", "close"))
    ha_o = (o[0] + c[0]) / 2.0
    ha_c = (o[0] + h[0] + l[0] + c[0]) / 4.0
    bias = {}
    for i in range(1, len(o)):
        ha_o = (ha_o + ha_c) / 2.0
        ha_c = (o[i] + h[i] + l[i] + c[i]) / 4.0
        # bias formed on day i is used on day i+1 (no lookahead)
        if i + 1 < len(daily.index):
            bias[daily.index[i + 1].date()] = "GREEN" if ha_c > ha_o else "RED"
    return bias


def main():
    d5 = fetch(SYMBOL, EXCHANGE, INTERVAL, LOOKBACK_DAYS)
    dd = fetch(SYMBOL, EXCHANGE, "D", LOOKBACK_DAYS)
    close, high, low = d5["close"].astype(float), d5["high"].astype(float), d5["low"].astype(float)

    ema_hi = pd.Series(ta.ema(high, EMA_LEN), index=d5.index)
    ema_lo = pd.Series(ta.ema(low, EMA_LEN), index=d5.index)

    bias = ha_bias_by_day(dd)
    bias_s = pd.Series([bias.get(ts.date()) for ts in d5.index], index=d5.index)

    t = d5.index.time
    in_window = (t >= pd.Timestamp(ENTRY_START).time()) & (t < pd.Timestamp(ENTRY_END).time())
    is_eod = t >= pd.Timestamp(EOD_EXIT).time()
    if SKIP_EXPIRY_WEEKDAY is not None:
        in_window &= d5.index.weekday != SKIP_EXPIRY_WEEKDAY

    long_raw = (bias_s == "GREEN") & (close > ema_hi) & in_window
    short_raw = (bias_s == "RED") & (close < ema_lo) & in_window

    # one entry per day: keep only the first signal of each session
    day = pd.Series(d5.index.date, index=d5.index)
    first_of_day = lambda s: s & ~s.groupby(day).cumsum().shift(fill_value=0).astype(bool)
    long_e, short_e = first_of_day(long_raw), first_of_day(short_raw)
    # if both fire the same day, the earlier one wins
    clash = long_e & short_e
    long_e, short_e = long_e & ~clash, short_e & ~clash

    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=long_e, exits=pd.Series(is_eod, index=d5.index),
        short_entries=short_e, short_exits=pd.Series(is_eod, index=d5.index),
        sl_stop=SL_PCT, tp_stop=SL_PCT * RR,
        size=LOT_SIZE, init_cash=INIT_CASH,
        fees=FEES, fixed_fees=FIXED_FEES,
        freq=INTERVAL,
    )

    tr = pf.trades.records_readable
    n = len(tr)
    pnl = tr["PnL"].sum() if n else 0.0
    wins = (tr["PnL"] > 0).sum() if n else 0
    bh = (close.iloc[-1] / close.iloc[0] - 1) * 100

    print("=" * 78)
    print(f"HA-EMA SIGNAL EDGE — {SYMBOL} {INTERVAL} via OpenAlgo (same feed as live)")
    print(f"period   : {d5.index[0]} .. {d5.index[-1]}  ({len(d5)} bars, {day.nunique()} sessions)")
    print(f"scope    : directional signal on the INDEX (futures proxy) — NOT option P&L")
    print(f"config   : EMA{EMA_LEN} channel | SL {SL_PCT*100:.2f}% | RR {RR} | 1 lot ({LOT_SIZE})"
          + (" | expiry-day skipped" if SKIP_EXPIRY_WEEKDAY is not None else ""))
    print("-" * 78)
    print(f"trades           : {n}")
    print(f"win rate         : {(100*wins/n if n else 0):.1f}%  ({wins}W / {n-wins}L)")
    print(f"net P&L          : Rs {pnl:,.0f}  (after {FEES*100:.3f}% + Rs{FIXED_FEES:.0f}/order)")
    print(f"avg per trade    : Rs {(pnl/n if n else 0):,.0f}  = {(pnl/n/LOT_SIZE if n else 0):.1f} index points")
    print(f"total return     : {pf.total_return()*100:+.2f}% on Rs {INIT_CASH:,}")
    print(f"sharpe           : {pf.sharpe_ratio():.2f}")
    print(f"max drawdown     : {pf.max_drawdown()*100:.2f}%")
    print(f"NIFTY buy & hold : {bh:+.2f}%  <- benchmark")
    print("-" * 78)
    if n:
        by_dow = tr.copy()
        by_dow["dow"] = pd.to_datetime(by_dow["Entry Timestamp"]).dt.day_name().str[:3]
        g = by_dow.groupby("dow")["PnL"].agg(["count", "sum", "mean"])
        print("by weekday:")
        for d_, r in g.reindex(["Mon", "Tue", "Wed", "Thu", "Fri"]).dropna().iterrows():
            print(f"  {d_}  n={int(r['count']):3d}  total Rs {r['sum']:>10,.0f}  avg Rs {r['mean']:>8,.0f}")
        tr.to_csv(OUT / f"{SYMBOL}_haema_signal_trades.csv", index=False)
        print(f"\ntrades -> {OUT / f'{SYMBOL}_haema_signal_trades.csv'}")
    print("=" * 78)


if __name__ == "__main__":
    main()
