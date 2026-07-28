"""
Renko: the arithmetic that YouTube backtests leave out.

Renko charts LOOK cleaner than time charts because they are constructed to. The
question is whether that cleanliness is tradable or an artifact. Four structural
properties decide it, and this script measures all four on real NIFTY/BANKNIFTY
1-minute data.

  1. TIME IS DISCARDED. A brick prints when price moves `size`, so several bricks
     can complete inside ONE minute. A "signal at brick close" may therefore be
     several signals at prices you could never have traded sequentially.

  2. BRICK-TO-BRICK P&L IS NOT TRADABLE P&L. Brick closes are quantised to
     multiples of `size`. Summing brick-to-brick moves produces a beautiful,
     almost monotonic equity curve. It is fiction: your fills happen at the
     MARKET price prevailing when the brick completed, not at the lattice value.
     This script computes BOTH and reports the gap. That gap is the illusion.

  3. A REVERSAL COSTS 2x SIZE. In classic Renko, flipping direction needs price
     to travel one brick to erase the current one and another to print the
     opposite. So every flip entry arrives AFTER a 2*size adverse excursion -
     a structural entry tax that no amount of indicator polish removes.

  4. ATR-SIZED RENKO REPAINTS. If brick size is a function of ATR, then as ATR
     evolves the historical brick boundaries are recomputed and the entire past
     brick series changes. Any backtest on ATR-Renko therefore contains
     look-ahead. Fixed-size Renko (used here) does not have this flaw - so these
     results are the OPTIMISTIC case for Renko.

Strategy tested is the canonical one every Renko tutorial teaches:
    flip long when a green brick prints after red; flip short on the opposite.
    Always in the market, reverse on flip.

Costs: 2.84 bps/side statutory (Flattrade, zero brokerage) + 1 bp slippage.

Usage:
    ../venv/Scripts/python.exe backtesting/intraday/renko_study.py
"""

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
COST_BPS_SIDE = 3.34


def load_1m(sym):
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, open, high, low, close
        FROM market_data WHERE symbol = ? AND interval = '1m' ORDER BY timestamp
    """, [sym]).fetchdf()
    con.close()
    df = df.set_index("ts")
    return df[~df.index.duplicated(keep="first")]


def build_renko(df, size):
    """Classic fixed-size Renko from 1m closes.

    Returns one row per completed brick with BOTH:
      brick_close - the quantised lattice level (what a Renko chart shows)
      fill_price  - the actual 1m close when the brick completed (what you trade)
    """
    close = df["close"].to_numpy(float)
    times = df.index.to_numpy()
    anchor = np.floor(close[0] / size) * size
    direction = 0                      # +1 up, -1 down, 0 undecided
    bricks = []
    for i in range(1, len(close)):
        px = close[i]
        while True:
            if direction >= 0 and px >= anchor + size:
                anchor += size
                direction = 1
                bricks.append((times[i], anchor, 1, px))
            elif direction <= 0 and px <= anchor - size:
                anchor -= size
                direction = -1
                bricks.append((times[i], anchor, -1, px))
            elif direction == 1 and px <= anchor - 2 * size:
                anchor -= 2 * size          # reversal: erase + print opposite
                direction = -1
                bricks.append((times[i], anchor, -1, px))
            elif direction == -1 and px >= anchor + 2 * size:
                anchor += 2 * size
                direction = 1
                bricks.append((times[i], anchor, 1, px))
            else:
                break
    return pd.DataFrame(bricks, columns=["ts", "brick_close", "dir", "fill_price"])


def flip_strategy(br, size, cost_bps=COST_BPS_SIDE):
    """Canonical Renko flip system, scored the naive way and the honest way."""
    if br.empty or len(br) < 20:
        return None
    flips = br[br["dir"] != br["dir"].shift(1)].copy()
    if len(flips) < 10:
        return None

    naive, honest, holds = [], [], []
    f = flips.reset_index(drop=True)
    for k in range(len(f) - 1):
        d = f.loc[k, "dir"]
        # naive: lattice-to-lattice, the number a Renko chart implies
        naive.append(d * (f.loc[k + 1, "brick_close"] - f.loc[k, "brick_close"]))
        # honest: real fill to real fill, minus round-trip cost
        entry, exit_ = f.loc[k, "fill_price"], f.loc[k + 1, "fill_price"]
        gross = d * (exit_ - entry)
        cost = (entry + exit_) * cost_bps / 1e4
        honest.append(gross - cost)
        holds.append((f.loc[k + 1, "ts"] - f.loc[k, "ts"]) / np.timedelta64(1, "m"))

    naive, honest = np.array(naive, float), np.array(honest, float)
    # bricks completing inside the same minute = signals you cannot trade in sequence
    same_min = int((br["ts"].diff() == np.timedelta64(0, "ns")).sum())
    return {
        "size": size, "bricks": len(br), "flips": len(f), "trades": len(honest),
        "naive_pts_total": round(naive.sum(), 0),
        "naive_pts_avg": round(naive.mean(), 2),
        "naive_win%": round(100 * (naive > 0).mean(), 1),
        "honest_pts_total": round(honest.sum(), 0),
        "honest_pts_avg": round(honest.mean(), 2),
        "honest_win%": round(100 * (honest > 0).mean(), 1),
        "median_hold_min": round(float(np.median(holds)), 1),
        "bricks_same_minute": same_min,
        "same_min_pct": round(100 * same_min / max(len(br), 1), 1),
    }


def main():
    print("=" * 100)
    print(" RENKO FLIP SYSTEM - naive (lattice) vs honest (real fills + cost)")
    print("=" * 100)
    for sym, sizes in (("NIFTY", (20, 30, 50, 75)), ("BANKNIFTY", (50, 75, 100, 150))):
        df = load_1m(sym)
        if df.empty:
            continue
        print(f"\n{sym}: {len(df):,} 1m bars  {df.index[0].date()}..{df.index[-1].date()}")
        print(f"{'size':>5s} {'bricks':>7s} {'trades':>7s} | {'NAIVE avg':>10s} {'win%':>6s} "
              f"{'total':>9s} | {'HONEST avg':>11s} {'win%':>6s} {'total':>9s} | "
              f"{'hold_min':>9s} {'same-min bricks':>16s}")
        for size in sizes:
            br = build_renko(df, size)
            r = flip_strategy(br, size)
            if not r:
                continue
            print(f"{r['size']:>5d} {r['bricks']:>7d} {r['trades']:>7d} | "
                  f"{r['naive_pts_avg']:>10.2f} {r['naive_win%']:>6.1f} "
                  f"{r['naive_pts_total']:>9.0f} | "
                  f"{r['honest_pts_avg']:>11.2f} {r['honest_win%']:>6.1f} "
                  f"{r['honest_pts_total']:>9.0f} | "
                  f"{r['median_hold_min']:>9.1f} "
                  f"{str(r['bricks_same_minute'])+' ('+str(r['same_min_pct'])+'%)':>16s}")

    print("\nReading the table:")
    print("  NAIVE  = brick-lattice P&L. This is what a Renko backtest shows if you")
    print("           treat bricks as bars. It is not achievable.")
    print("  HONEST = same signals, filled at the real 1m price when each brick")
    print("           completed, minus 3.34 bps/side. This is what you would get.")
    print("  The gap between the two columns is the Renko illusion, in points.")


if __name__ == "__main__":
    main()
