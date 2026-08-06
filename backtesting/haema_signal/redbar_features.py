"""
Red Bar / X-Candle — feature-augmented trade log for regime-gate discovery
==========================================================================
Purpose: find what separates the winning years (2024, 2026) from the losing
ones (2023, 2025) using ONLY features computable at entry time, then test
doors chronologically (IS 2023-24 -> OOS 2025-26). Protects against tuning
thresholds on the OOS period: the gate is chosen on IS and reported on OOS
exactly once.

This file emits every trade with its features to a CSV; analysis is a second
pass (so gates can be tested without re-running the engine).
"""
import importlib.util
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")
sys.path.insert(0, str(Path(__file__).parent))

import redbar_backtest as rb
import redbar_trail_backtest as rt

OUT = Path(__file__).parent / "redbar_trades_features.csv"


def out_path(symbol):
    return Path(__file__).parent / f"redbar_trades_features_{symbol}.csv"


def load_5m(symbol):
    con = duckdb.connect(rb.DB, read_only=True)
    df = con.execute(
        "SELECT to_timestamp(timestamp)::TIMESTAMP ts, open, high, low, close "
        "FROM market_data WHERE symbol=? AND interval='5m' ORDER BY timestamp",
        [symbol],
    ).fetchdf()
    con.close()
    df = df.set_index("ts")
    df.index = pd.to_datetime(df.index)
    return df


def daily_features(df):
    """Per-session features known before the trade's entry time."""
    d = df.resample("1D").agg({"open": "first", "high": "max", "low": "min",
                               "close": "last"}).dropna()
    d["ret"] = d["close"].pct_change()
    d["rv5"] = d["ret"].rolling(5).std() * np.sqrt(252)   # 5-day realized vol
    d["mom5"] = d["close"].pct_change(5)                  # 5-day momentum
    d["gap"] = d["open"] / d["close"].shift(1) - 1        # session gap %
    d["range_pct"] = (d["high"] - d["low"]) / d["close"].shift(1)
    d["prev_ret"] = d["ret"].shift(1)
    return d


def build_trades(symbol, min_hold_minutes=90):
    m = rb.load_strategy()
    df5 = load_5m(symbol)
    df30 = rt.resample(df5, 30)
    daily = daily_features(df5)
    days = sorted({x.date() for x in df5.index})

    rows = []
    for di, day in enumerate(days):
        if di < rb.LOOKBACK_DAYS:
            continue
        window_start = days[di - rb.LOOKBACK_DAYS]
        sl = df30[(df30.index.date >= window_start) & (df30.index.date <= day)]
        tb = sl[sl.index.date == day]
        if len(tb) < 6:
            continue
        prev_day = df30[df30.index.date == days[di - 1]]
        prev_close = float(prev_day["close"].iloc[-1]) if len(prev_day) else None

        sig = None
        n = len(tb)
        base = len(sl) - n
        for k in range(n):
            s = m.compute_red_bar_signal(sl.iloc[: base + k + 1], day, None, prev_close)
            if s and s.get("signal"):
                sig, trig_idx = s, k - 1
                break
        if not sig:
            continue

        entry = sig["entry_spot"]
        slp = sig["sl_spot"]
        tgt = sig["target_spot"]
        risk = sig["risk"]
        side = 1 if sig["signal"] == "CE" else -1
        entry_time = tb.index[trig_idx] + pd.Timedelta(minutes=30)
        fx = None
        for ts, row in tb.iloc[trig_idx + 1:].iterrows():
            held = (ts - entry_time).total_seconds() / 60.0
            if ts.time() >= m.EXIT_TIME:
                fx, reason = row["close"], "EOD"
                break
            hit = row["low"] <= slp if side == 1 else row["high"] >= slp
            if hit:
                fx, reason = slp, "SL"
                break
            hit = row["high"] >= tgt if side == 1 else row["low"] <= tgt
            if hit:
                fx, reason = tgt, "target"
                break
            if held >= max(min_hold_minutes, 90):
                fx, reason = row["close"], "max-hold"
                break
        if fx is None:
            if len(tb.iloc[trig_idx + 1:]) == 0:
                continue
            fx, reason = tb["close"].iloc[-1], "EOD"

        pts = (fx - entry) * side
        prem = entry * 0.0055
        cost = (2 * prem * 65) * rb.OPT_COST_PCT / 100.0 + prem * 65 * rb.SPREAD_PCT / 100.0
        rs = pts * rb.DELTA * 65 - cost
        drow = daily.loc[pd.Timestamp(day)] if pd.Timestamp(day) in daily.index else None
        rows.append({
            "date": day, "dir": sig["signal"], "anchor": sig["anchor"],
            "entry": entry, "risk": risk, "pts": pts, "rs": rs, "reason": reason,
            "entry_h": entry_time.hour + entry_time.minute / 60,
            "rv5": drow["rv5"] if drow is not None else np.nan,
            "mom5": drow["mom5"] if drow is not None else np.nan,
            "gap": (tb["open"].iloc[0] / prev_close - 1) if prev_close else np.nan,
            "range_pct": drow["range_pct"] if drow is not None else np.nan,
            "xt_range_pts": sig["x_high"] - sig["x_low"],
        })
    t = pd.DataFrame(rows)
    return t


if __name__ == "__main__":
    specific = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"
    t = build_trades(specific)
    t.to_csv(out_path(specific), index=False)
    print(f"{specific}: {len(t)} trades with features -> {out_path(specific)}")