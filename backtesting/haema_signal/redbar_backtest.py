"""
Red Bar / X-Candle — independent backtest on the long local history
====================================================================
The strategy file carries a Volrix backtest that inverts out of sample
(NIFTY PF 1.2 -> 0.5, SENSEX 1.3 -> 0.4) and says so plainly. But that ran on
~125 sessions, capped by a free-tier six-month history limit, and the file
asks for a longer test before the verdict is trusted either way.

Local DuckDB has NIFTY 5m back to 2023-04 (776 sessions, ~6x) and SENSEX from
2026-01. So re-run it there.

Faithfulness: this imports compute_red_bar_signal and _anchor_from from the
strategy module itself rather than reimplementing them, so the encoded rules
cannot drift from what would actually trade. Only the execution layer is
simulated here.

# Honest limit, stated up front: local data is SPOT. The Volrix run used real
# weekly option premiums, which is strictly better for P&L. This measures
# whether the SIGNAL has directional edge in index points, then translates with
# today's measured constants (delta 0.358, premium-based cost) rather than
# assumed ones. Theta is NOT modelled - two attempts at measuring it produced
# physically impossible values - so the option numbers here are OPTIMISTIC.

# ============================================================================
# RESULTS (run 2026-08-06, corrected exit semantics: wall-clock 90min max-hold,
# EOD > SL > target > max-hold priority, matching the live loop)
# ============================================================================
# NIFTY 776 sessions 2023-04-05..2026-05-27:
#   10m  PF 0.88  -38,340   | 15m PF 0.84  -56,000   | 30m PF 1.07  +22,259
#   45m  PF 1.03   +8,245   | 60m PF 1.04   +7,621
#   At the live default (30m): IS PF 1.07 / OOS PF 1.08 -- NO inversion, the
#   opposite of the file's Volrix verdict (1.2 -> 0.5, on ~125 later-window
#   sessions). But the edge is weak and regime-dependent: 2023 PF 0.88,
#   2024 PF 1.17, 2025 PF 0.95, 2026 PF 1.39 -> 2 of 4 years negative.
#   +32/trade net on ~1 lot; un-tradeable at current capital (wall identical
#   to overnight drift), and theta-blind.
#   Parameter sweep: PF flat 1.02-1.08 across FIB_HI 0.45-0.60, FIB_LO 0.40-0.50,
#   MAX_SL 0.30-1.00, RR 2-4 -> a robustness PLATEAU, not a tuned spike.
#   (It is not a no-op: trade counts do change slightly on some variants;
#   FIB levels barely gate because the [2.4] sideways rule already requires a
#   close beyond the anchor, so band shifts rarely flip direction.)
# SENSEX 79 sessions 2026-01-30..2026-05-27:
#   30m PF 0.86 -4,772 | 45m PF 1.13 +2,976 | 60m PF 0.98 -635
#   -> no usable evidence; agrees with the file that SENSEX's edge (1.3) did
#      NOT hold (this window: 0.86 at the production interval).
# VERDICT: the file's claim "does not survive two months forward" fails to
# replicate at 30m across 3.2 years of NIFTY spot (edge persists, weakly), and
# holds for SENSEX. But a PF ~1.07, 2/4 positive years, theta-ignored,
# capital-unreachable edge is NOT a deployable strategy. Keep off live; do not
# forward-test until either capital clears the ~25L barrier or the Greeks
# collector can price theta on the 284 max-hold / 67 EOD exits (51% of trades)
# that currently escape decay modelling.
# ============================================================================
"""
import importlib.util
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")

DB = "backtesting/data/market_cache.duckdb"
STRAT = Path("strategies/examples/red_bar_x_candle_strategy.py")

# Measured 2026-08-05, not assumed.
DELTA = 0.358
OPT_COST_PCT = 0.12       # % of premium turnover, Flattrade-validated
SPREAD_PCT = 0.41         # % of premium, measured NIFTY11AUG2624600CE
LOOKBACK_DAYS = 3         # EMA continuity, matching what the live script fetches
INTERVAL_MIN = 30         # live default INTERVAL (the strategy trades 30m bars)


def load_strategy():
    spec = importlib.util.spec_from_file_location("redbar", STRAT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_bars(symbol):
    con = duckdb.connect(DB, read_only=True)
    df = con.execute(
        "SELECT to_timestamp(timestamp)::TIMESTAMP ts, open, high, low, close "
        "FROM market_data WHERE symbol=? AND interval='5m' ORDER BY timestamp",
        [symbol],
    ).fetchdf()
    con.close()
    df = df.set_index("ts")
    df.index = pd.to_datetime(df.index)
    return df


def option_pnl(points, premium, lot):
    """Index points -> rupees on one option lot, net of real friction."""
    gross = points * DELTA * lot
    cost = (2 * premium * lot) * OPT_COST_PCT / 100.0 + premium * SPREAD_PCT / 100.0 * lot
    return gross - cost

def to_30m(df):
    """Resample 5m -> 30m anchored on the 09:15 open.

    The live strategy runs INTERVAL=30m (its own comment: "One 30m bar covers
    the whole 09:15-09:45 anchor"), and MIN_ANCHOR_BARS=1 assumes exactly that.
    Backtesting it on 5m bars silently tests a different system: more trigger
    candles, tighter stops, and an anchor built from six bars instead of one.
    """
    out = []
    for day, g in df.groupby(df.index.date):
        g = g.sort_index()
        origin = pd.Timestamp(f"{day} 09:15:00")
        r = g.resample("30min", origin=origin).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
        out.append(r)
    return pd.concat(out).sort_index() if out else df


def backtest(m, df, symbol, lot, premium_pct):
    """One trade per day, exits in the strategy's own priority order."""
    days = sorted({d.date() for d in df.index})
    trades = []

    for di, day in enumerate(days):
        if di < LOOKBACK_DAYS:
            continue
        window_start = days[di - LOOKBACK_DAYS]
        sl = df[(df.index.date >= window_start) & (df.index.date <= day)]
        today_bars = sl[sl.index.date == day]
        if len(today_bars) < 6:
            continue

        # previous session close, for the gap gate
        prev_day = df[df.index.date == days[di - 1]]
        prev_close = float(prev_day["close"].iloc[-1]) if len(prev_day) else None

        # walk the session; compute_red_bar_signal drops the forming bar, so
        # feeding it bars[:k+1] evaluates bar k as the completed trigger
        n_today = len(today_bars)
        base = len(sl) - n_today
        sig = None
        for k in range(n_today):
            upto = sl.iloc[: base + k + 1]
            s = m.compute_red_bar_signal(upto, day, None, prev_close)
            if s and s.get("signal"):
                sig = s
                trig_idx = k - 1          # the completed bar that fired it
                break
        if not sig:
            continue

        entry = sig["entry_spot"]
        slp = sig["sl_spot"]
        tgt = sig["target_spot"]
        risk = sig["risk"]
        side = 1 if sig["signal"] == "CE" else -1

        entry_ts = today_bars.index[trig_idx]   # bar of the trigger (open)
        entry_time = entry_ts + pd.Timedelta(minutes=INTERVAL_MIN)  # fill after bar close

        # forward path from the bar AFTER the trigger; live priority and timing
        fwd = today_bars.iloc[trig_idx + 1:]
        exit_px, reason, held = None, None, 0.0
        for ts, row in fwd.iterrows():
            held = (ts - entry_time).total_seconds() / 60.0
            if ts.time() >= m.EXIT_TIME:                    # EOD squareoff first
                exit_px, reason = row["close"], "EOD"
                break
            if side == 1:
                if row["low"] <= slp:                       # stop-limit on spot
                    exit_px, reason = slp, "SL"
                    break
                if row["high"] >= tgt:                     # target fill
                    exit_px, reason = tgt, "target"
                    break
            else:
                if row["high"] >= slp:
                    exit_px, reason = slp, "SL"
                    break
                if row["low"] <= tgt:
                    exit_px, reason = tgt, "target"
                    break
            if held >= m.MAX_HOLD_MINUTES:                 # max-hold after SL/target
                exit_px, reason = row["close"], "max-hold"
                break
        if exit_px is None:
            if len(fwd) == 0:
                continue
            exit_px, reason = fwd["close"].iloc[-1], "EOD"

        pts = (exit_px - entry) * side
        prem = entry * premium_pct / 100.0
        trades.append({
            "date": day, "dir": sig["signal"], "anchor": sig["anchor"],
            "entry": entry, "sl": slp, "target": tgt, "risk": risk,
            "exit": exit_px, "reason": reason, "pts": pts,
            "R": pts / risk if risk else 0.0,
            "rs": option_pnl(pts, prem, lot),
            "bars": int(held // INTERVAL_MIN) + 1,
            "entry_ts": entry_time, "exit_ts": ts,
        })
    return pd.DataFrame(trades)


def report(t, label):
    if t.empty:
        print(f"{label:34s} | no trades")
        return None
    n = len(t)
    w = (t["pts"] > 0).sum()
    gw = t.loc[t["rs"] > 0, "rs"].sum()
    gl = abs(t.loc[t["rs"] <= 0, "rs"].sum())
    pf = gw / gl if gl > 0 else float("inf")
    eq = t["rs"].cumsum()
    dd = (eq - eq.cummax()).min()
    sharpe = (t["rs"].mean() / t["rs"].std() * np.sqrt(252)) if t["rs"].std() else 0.0
    print(f"{label:34s} | T:{n:4d} W:{w / n * 100:5.1f}% "
          f"pts/trade {t['pts'].mean():+7.2f} R/trade {t['R'].mean():+5.2f} | "
          f"net Rs {t['rs'].sum():+9,.0f} ({t['rs'].mean():+6.0f}/trade) "
          f"PF {pf:4.2f} DD {dd:+8,.0f} Sharpe {sharpe:+5.2f}")
    return {"n": n, "pf": pf, "net": t["rs"].sum(), "sharpe": sharpe}


if __name__ == "__main__":
    m = load_strategy()
    print(f"Encoded params: FIB {m.FIB_LO}/{m.FIB_HI} | RR {m.RR} | "
          f"minSL {m.MIN_SL_PCT}% maxSL {m.MAX_SL_PCT}% | X-end {m.X_END} | "
          f"entry<{m.ENTRY_END} | exit {m.EXIT_TIME} | reanchor {m.REANCHOR_1245}")
    print(f"Gates: EMA10={m.REQUIRE_EMA10} EMA30={m.REQUIRE_EMA30} "
          f"CPR={m.REQUIRE_CPR} GAP={m.REQUIRE_GAP_GATE}")
    print()

    for symbol, lot, prem_pct in (("NIFTY", 65, 0.55), ("SENSEX", 20, 0.45)):
        df5 = load_bars(symbol)
        if df5.empty:
            print(f"{symbol}: no local data")
            continue
        df = to_30m(df5)
        sessions = len({d.date() for d in df.index})
        print("=" * 132)
        print(f" {symbol} — {len(df):,} 30m bars (from {len(df5):,} 5m), {sessions} sessions, "
              f"{df.index.min():%Y-%m-%d} to {df.index.max():%Y-%m-%d}")
        print("=" * 132)

        t = backtest(m, df, symbol, lot, prem_pct)
        if t.empty:
            print("  no trades\n")
            continue

        report(t, "FULL PERIOD")

        dts = sorted(t["date"].unique())
        mid = dts[len(dts) // 2]
        report(t[t["date"] <= mid], f"  in-sample  <= {mid}")
        report(t[t["date"] > mid], f"  OUT-OF-SAMPLE > {mid}")

        print()
        print("  exits:", dict(t["reason"].value_counts()))
        print("  by direction:")
        for d, g in t.groupby("dir"):
            print(f"    {d}: {len(g):4d} trades  {g['pts'].mean():+7.2f} pts  net Rs {g['rs'].sum():+9,.0f}")
        print("  by anchor:")
        for a, g in t.groupby("anchor"):
            print(f"    {a:6s}: {len(g):4d} trades  {g['pts'].mean():+7.2f} pts  net Rs {g['rs'].sum():+9,.0f}")

        print("  by year:")
        t["yr"] = pd.to_datetime(t["date"]).dt.year
        for y, g in t.groupby("yr"):
            print(f"    {y}: {len(g):4d} trades  {g['pts'].mean():+7.2f} pts/trade  "
                  f"net Rs {g['rs'].sum():+9,.0f}  PF {(g.loc[g['rs'] > 0, 'rs'].sum() / max(abs(g.loc[g['rs'] <= 0, 'rs'].sum()), 1)):.2f}")
        print()
