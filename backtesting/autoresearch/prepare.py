"""
Fixed evaluation harness for NIFTY autoresearch.  DO NOT MODIFY.

Adapted from marketcalls/emacrossover-autoresearch (see program.md for credit),
with ONE deliberate deviation described below.

Usage:
    ../../venv/Scripts/python.exe prepare.py

The agent modifies strategy.py only. This file is read-only.

WHY THIS DIFFERS FROM THE REFERENCE
-----------------------------------
The reference harness scores a single window and keeps any change that improves
it. On this account that is an overfitting machine: on 2026-07-28 a weekday
filter scored +5.38% / Sharpe 1.33 over Feb-Jul, then split into +7.20% on the
first half and -10.42% on the second. One number hid a regime break.

So the data is cut three ways, chronologically:
    TRAIN   60%  - explore freely here
    VAL     20%  - THE SCORE COMES FROM HERE (so a change must generalise)
    TEST    20%  - never scored, printed once for information

Scoring on VAL means VAL is gradually consumed by selection pressure; that is
the same trade-off Karpathy's autoresearch makes with val_bpb, and it is far
safer than scoring the window you tuned on. TEST is the honest read - every time
you look at it you spend a little of its credibility, so look rarely.
"""

import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import vectorbt as vbt
from dotenv import find_dotenv, load_dotenv
from openalgo import api

# ── Fixed constants (agent must not change these) ───────────────────────────
SYMBOL = "NIFTY"
EXCHANGE = "NSE_INDEX"
INTERVAL = "5m"
LOOKBACK_DAYS = 400
SOURCE = "db"                 # OpenAlgo Historify DuckDB — no rate limit
INIT_CASH = 2_000_000
FEES = 0.00018                # F&O futures 0.018%
FIXED_FEES = 20.0             # Rs 20 per order
LOT_SIZE = 65                 # SEBI revised, Dec 2025
SPLIT = (0.60, 0.20, 0.20)    # train / val / test, chronological

# Targets are judged on the VAL slice (~2.5 months of 5-min bars)
TARGET_RETURN = 4.0           # % on INIT_CASH
TARGET_SHARPE = 1.0
TARGET_MAX_DD = 6.0           # %
MIN_TRADES = 15               # per slice, for any statistical meaning

script_dir = Path(__file__).resolve().parent
load_dotenv(find_dotenv(), override=False)


def load_data():
    client = api(api_key=os.getenv("OPENALGO_API_KEY"),
                 host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))
    end = datetime.now().date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    df = client.history(symbol=SYMBOL, exchange=EXCHANGE, interval=INTERVAL,
                        start_date=start.strftime("%Y-%m-%d"),
                        end_date=end.strftime("%Y-%m-%d"), source=SOURCE)
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise SystemExit(f"no data for {SYMBOL} {EXCHANGE} {INTERVAL} (source={SOURCE})")
    df = df.sort_index()
    df.index = pd.to_datetime(df.index)
    # keep regular session bars only (the feed carries a 09:14 pre-open bar)
    return df[df.index.time >= pd.Timestamp("09:15").time()]


def split_data(df):
    """Chronological train/val/test. Split on DAY boundaries so no session is cut."""
    days = sorted({d.date() for d in df.index})
    n = len(days)
    i1 = int(n * SPLIT[0])
    i2 = int(n * (SPLIT[0] + SPLIT[1]))
    tr, va, te = set(days[:i1]), set(days[i1:i2]), set(days[i2:])
    pick = lambda s: df[[d.date() in s for d in df.index]]
    return pick(tr), pick(va), pick(te)


def run_slice(df):
    """Run strategy.py on one slice. Returns a metrics dict."""
    import importlib
    if "strategy" in sys.modules:
        del sys.modules["strategy"]
    sys.path.insert(0, str(script_dir))
    import strategy

    long_e, long_x, short_e, short_x = strategy.generate_signals(df)
    close = df["close"].astype(float)

    def as_bool(s):
        if not isinstance(s, pd.Series):
            s = pd.Series(s, index=close.index)
        return s.reindex(close.index).fillna(False).astype(bool)

    long_e, long_x = as_bool(long_e), as_bool(long_x)
    short_e, short_x = as_bool(short_e), as_bool(short_x)

    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=long_e, exits=long_x,
        short_entries=short_e, short_exits=short_x,
        size=LOT_SIZE, init_cash=INIT_CASH,
        fees=FEES, fixed_fees=FIXED_FEES, freq=INTERVAL,
    )
    tr = pf.trades
    n = int(tr.count())
    f = lambda v, d=0.0: d if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v
    return {
        "total_return": round(f(pf.total_return()) * 100, 2),
        "sharpe": round(f(pf.sharpe_ratio()), 4),
        "max_drawdown": round(abs(f(pf.max_drawdown())) * 100, 2),
        "win_rate": round(f(tr.win_rate()) * 100, 1) if n else 0.0,
        "total_trades": n,
        "profit_factor": round(f(tr.profit_factor()), 2) if n else 0.0,
        "bars": len(df),
        "sessions": len({d.date() for d in df.index}),
    }


def compute_score(m):
    """Composite on the VAL slice. Lower is better; 0.0 = all targets met."""
    ret_gap = max(0, TARGET_RETURN - m["total_return"]) / TARGET_RETURN
    shp_gap = max(0, TARGET_SHARPE - m["sharpe"]) / TARGET_SHARPE
    dd_gap = max(0, m["max_drawdown"] - TARGET_MAX_DD) / TARGET_MAX_DD
    ret_bonus = max(0, m["total_return"] - TARGET_RETURN) / TARGET_RETURN * 0.1
    shp_bonus = max(0, m["sharpe"] - TARGET_SHARPE) / TARGET_SHARPE * 0.1
    trade_penalty = 0.2 if m["total_trades"] < MIN_TRADES else 0.0
    return round(ret_gap + shp_gap + dd_gap - ret_bonus - shp_bonus + trade_penalty, 6)


def targets_met(m):
    return (m["total_return"] >= TARGET_RETURN
            and m["sharpe"] >= TARGET_SHARPE
            and m["max_drawdown"] <= TARGET_MAX_DD
            and m["total_trades"] >= MIN_TRADES)


def row(label, m):
    print(f"{label:8s} ret {m['total_return']:>8.2f}%  sharpe {m['sharpe']:>7.4f}  "
          f"maxDD {m['max_drawdown']:>6.2f}%  trades {m['total_trades']:>4d}  "
          f"win {m['win_rate']:>5.1f}%  PF {m['profit_factor']:>5.2f}  "
          f"({m['sessions']} sessions)")


if __name__ == "__main__":
    print(f"Loading {SYMBOL} {EXCHANGE} {INTERVAL} (source={SOURCE}, {LOOKBACK_DAYS}d)...")
    df = load_data()
    tr_df, va_df, te_df = split_data(df)
    print(f"Data: {len(df)} bars  {df.index[0]} .. {df.index[-1]}")
    print(f"Split: train {len(tr_df)} | val {len(va_df)} | test {len(te_df)} bars\n")

    try:
        m_tr, m_va, m_te = run_slice(tr_df), run_slice(va_df), run_slice(te_df)
    except Exception as e:
        print(f"\nCRASH: {e}")
        import traceback
        traceback.print_exc()
        print("\n--- Summary ---")
        print("score:          999.999999")
        print("all_targets:    NO")
        sys.exit(1)

    score = compute_score(m_va)
    all_met = targets_met(m_va)

    print("=" * 78)
    print(f"  NIFTY Autoresearch — {SYMBOL} {INTERVAL}")
    print("=" * 78)
    row("TRAIN", m_tr)
    row("VAL", m_va)
    row("TEST", m_te)

    print("\n--- Metrics (VAL — the scored slice) ---")
    for k in ("total_return", "sharpe", "max_drawdown", "win_rate", "total_trades", "profit_factor"):
        v = m_va[k]
        print(f"{k}:{'':<{max(1, 16 - len(k))}}{v}{'%' if k in ('total_return','max_drawdown','win_rate') else ''}")

    print("\n--- Targets (VAL) ---")
    print(f"Return >= {TARGET_RETURN}%:   {m_va['total_return']:.2f}%  "
          f"[{'MET' if m_va['total_return'] >= TARGET_RETURN else 'NOT MET'}]")
    print(f"Sharpe >= {TARGET_SHARPE}:     {m_va['sharpe']:.4f}  "
          f"[{'MET' if m_va['sharpe'] >= TARGET_SHARPE else 'NOT MET'}]")
    print(f"Max DD <= {TARGET_MAX_DD}%:    {m_va['max_drawdown']:.2f}%  "
          f"[{'MET' if m_va['max_drawdown'] <= TARGET_MAX_DD else 'NOT MET'}]")
    print(f"Trades >= {MIN_TRADES}:      {m_va['total_trades']}  "
          f"[{'MET' if m_va['total_trades'] >= MIN_TRADES else 'NOT MET'}]")

    # generalisation gap: train-vs-val divergence is the overfit tell
    gap = round(m_tr["total_return"] - m_va["total_return"], 2)
    print("\n--- Summary ---")
    print(f"score:          {score:.6f}")
    print(f"all_targets:    {'YES' if all_met else 'NO'}")
    print(f"train_val_gap:  {gap:+.2f}%   (large positive = fitted to train)")
    print(f"test_return:    {m_te['total_return']:+.2f}%   (never scored — informational)")
