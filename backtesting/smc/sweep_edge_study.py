"""
Does the liquidity-sweep selection actually predict anything?

Tests the single claim the SKB "BSL/SSL" infographic rests on:

    LIQUIDITY SWEEP  = price breaks a pool, then REVERSES  -> tradable
    REAL BREAKOUT    = price breaks a pool with momentum and CONTINUES -> not tradable

Both groups take out the same kind of pool, so comparing them is apples-to-apples
and isolates the predictive content of the *rejection close* itself. That removes
the mechanical bias you get by comparing sweeps against arbitrary bars (a sweep
bar is a local extreme by construction, so its high is trivially harder to exceed).

Measured quantity = exactly the credit strategy's win condition:
    P(hold) = P(the swept extreme is never exceeded again before 15:15)

If the infographic is right, sweeps must hold materially more often than
breakouts, consistently across instruments and years.

Data: DuckDB cache fed by harvest_state.db - 4 indices x ~777 trading days of 5m
bars (2023-04-05..2026-05-27), validated at 98.9% exact close-parity vs the live
broker feed. No option premiums needed: this is a price-path statistic.

Usage:
    ../venv/Scripts/python.exe backtesting/smc/sweep_edge_study.py
"""

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")

PIVOT_K = 2          # bars either side of a pivot   (same as the Volrix strategy)
LOOKBACK = 40        # candles scanned for pools
BUF = 0.0008         # the strategy's invalidation buffer past the swept extreme
ENTRY_START = "09:30"
ENTRY_END = "14:30"
EOD = "15:15"


def load(sym: str) -> pd.DataFrame:
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, open, high, low, close
        FROM market_data WHERE symbol = ? AND interval = '5m' ORDER BY timestamp
    """, [sym]).fetchdf()
    con.close()
    df = df.set_index("ts")
    return df[~df.index.duplicated(keep="first")]


def pools(high: np.ndarray, low: np.ndarray, i: int):
    """Confirmed pivot highs/lows in the LOOKBACK window ending at bar i.
    A pivot needs PIVOT_K bars either side, so the last PIVOT_K bars are never
    pivots - keeps the test look-ahead free."""
    start = max(PIVOT_K, i - LOOKBACK)
    ph, pl = [], []
    for j in range(start, i - PIVOT_K + 1):
        seg_h = high[j - PIVOT_K:j + PIVOT_K + 1]
        if high[j] >= seg_h.max():
            ph.append(high[j])
        seg_l = low[j - PIVOT_K:j + PIVOT_K + 1]
        if low[j] <= seg_l.min():
            pl.append(low[j])
    return ph, pl


def classify_day(day: pd.DataFrame, prev_hi: float, prev_lo: float):
    """Yield (group, side, held, mfe_pts, extreme) for every pool-taking event."""
    h = day["high"].to_numpy(float)
    l = day["low"].to_numpy(float)
    c = day["close"].to_numpy(float)
    times = day.index.strftime("%H:%M").to_numpy()
    out = []
    # forward-looking maxima/minima to EOD, computed once per day
    eod_mask = times <= EOD
    for i in range(LOOKBACK, len(day)):
        if not (ENTRY_START <= times[i] < ENTRY_END):
            continue
        ph, pl = pools(h, l, i)
        if prev_hi > 0:
            ph = ph + [prev_hi]
        if prev_lo > 0:
            pl = pl + [prev_lo]
        fwd = slice(i + 1, len(day))
        fwd_ok = eod_mask[i + 1:]
        if fwd_ok.sum() == 0:
            continue
        fwd_hi = h[fwd][fwd_ok]
        fwd_lo = l[fwd][fwd_ok]

        # --- buy-side pools taken out (above) ---
        took_up = [L for L in ph if h[i] > L]
        if took_up:
            E = h[i]
            held = bool(fwd_hi.max() <= E * (1 + BUF))
            mfe = float(E - fwd_lo.min())          # points earned by fading down
            grp = "sweep" if c[i] < max(took_up) else "breakout"
            out.append((grp, "up", held, mfe, E))

        # --- sell-side pools taken out (below) ---
        took_dn = [L for L in pl if l[i] < L]
        if took_dn:
            E = l[i]
            held = bool(fwd_lo.min() >= E * (1 - BUF))
            mfe = float(fwd_hi.max() - E)
            grp = "sweep" if c[i] > min(took_dn) else "breakout"
            out.append((grp, "dn", held, mfe, E))
    return out


def main():
    rows = []
    for sym in SYMBOLS:
        df = load(sym)
        if df.empty:
            print(f"{sym}: no data")
            continue
        days = [g for _, g in df.groupby(df.index.date)]
        prev_hi = prev_lo = 0.0
        n_days = 0
        for day in days:
            if len(day) > LOOKBACK + 5:
                for grp, side, held, mfe, E in classify_day(day, prev_hi, prev_lo):
                    rows.append({"symbol": sym, "year": day.index[0].year, "group": grp,
                                 "side": side, "held": held, "mfe": mfe})
                n_days += 1
            prev_hi = float(day["high"].max())
            prev_lo = float(day["low"].min())
        print(f"{sym:11s} {n_days:4d} days  {df.index[0].date()}..{df.index[-1].date()}")

    ev = pd.DataFrame(rows)
    if ev.empty:
        print("no events")
        return
    print(f"\ntotal pool-taking events: {len(ev):,}")

    def rate(d):
        return pd.Series({"events": len(d), "P_hold%": round(100 * d["held"].mean(), 1),
                          "mfe_med": round(d["mfe"].median(), 1)})

    print("\n=== headline: sweep vs real breakout (all instruments, all years) ===")
    print(ev.groupby("group").apply(rate, include_groups=False).to_string())

    print("\n=== per instrument ===")
    print(ev.groupby(["symbol", "group"]).apply(rate, include_groups=False).to_string())

    print("\n=== per year (is it stable through time?) ===")
    print(ev.groupby(["year", "group"]).apply(rate, include_groups=False).to_string())

    sw = ev[ev["group"] == "sweep"]["held"]
    bo = ev[ev["group"] == "breakout"]["held"]
    if len(sw) > 30 and len(bo) > 30:
        p1, p2 = sw.mean(), bo.mean()
        n1, n2 = len(sw), len(bo)
        p = (sw.sum() + bo.sum()) / (n1 + n2)
        se = (p * (1 - p) * (1 / n1 + 1 / n2)) ** 0.5
        z = (p1 - p2) / se if se > 0 else 0.0
        print(f"\n=== two-proportion z-test on P(hold) ===")
        print(f"  sweep    P(hold)={100*p1:.1f}%  n={n1:,}")
        print(f"  breakout P(hold)={100*p2:.1f}%  n={n2:,}")
        print(f"  edge={100*(p1-p2):+.1f}pp   z={z:.1f}"
              f"   {'SIGNIFICANT' if abs(z) > 3 else 'not significant'} (|z|>3)")

    ev.to_csv(Path(__file__).resolve().parent / "sweep_edge_events.csv", index=False)
    print(f"\nevents -> {Path(__file__).resolve().parent / 'sweep_edge_events.csv'}")


if __name__ == "__main__":
    main()
