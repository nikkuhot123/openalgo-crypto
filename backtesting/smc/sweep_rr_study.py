"""
Given the sweep selection edge is real (+8.1pp, z=25, see sweep_edge_study.py),
is there ANY payoff structure that makes it profitable?

sweep_edge_study measured P(hold)=40.9% for sweeps. A structure paying 1.25:1
(the credit model's 50%-target / 40%-stop) needs 44.4% to break even - hence the
measured PF 0.9. But the median favourable excursion was 55 points against a
~19-point stop, implying a much larger payoff is physically available.

So this walks the actual bar path for every event and asks: for a fade entered at
the sweep close, stopped just beyond the swept extreme, what is the expectancy in
R-multiples for a target at k x risk?

Conservative wherever ambiguous:
  - if stop and target both fall inside one bar, the STOP is taken first
  - unresolved by 15:15 -> exit at that bar's close (no free carry)
  - entry at the sweep candle's close (no look-ahead, no better fill assumed)

Pure price-path study on the index: no option premium, no slippage model. It
bounds what the SIGNAL can support before any instrument choice.

Usage:
    ../venv/Scripts/python.exe backtesting/smc/sweep_rr_study.py
"""

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")

PIVOT_K, LOOKBACK, BUF = 2, 40, 0.0008
ENTRY_START, ENTRY_END, EOD = "09:30", "14:30", "15:15"
KS = (1.0, 1.5, 2.0, 2.5, 3.0)


def load(sym):
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, open, high, low, close
        FROM market_data WHERE symbol = ? AND interval = '5m' ORDER BY timestamp
    """, [sym]).fetchdf().set_index("ts")
    con.close()
    return df[~df.index.duplicated(keep="first")]


def pools(high, low, i):
    start = max(PIVOT_K, i - LOOKBACK)
    ph, pl = [], []
    for j in range(start, i - PIVOT_K + 1):
        if high[j] >= high[j - PIVOT_K:j + PIVOT_K + 1].max():
            ph.append(high[j])
        if low[j] <= low[j - PIVOT_K:j + PIVOT_K + 1].min():
            pl.append(low[j])
    return ph, pl


def resolve(side, entry, stop, target, fh, fl, close_eod):
    """Walk the forward path. Returns realised R-multiple."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for j in range(len(fh)):
        if side == "up":                      # faded down (bearish)
            if fh[j] >= stop:                 # stop first when both in one bar
                return -1.0
            if fl[j] <= target:
                return (entry - target) / risk
        else:                                 # faded up (bullish)
            if fl[j] <= stop:
                return -1.0
            if fh[j] >= target:
                return (target - entry) / risk
    pnl = (entry - close_eod) if side == "up" else (close_eod - entry)
    return pnl / risk


def main():
    rows = []
    for sym in SYMBOLS:
        df = load(sym)
        if df.empty:
            continue
        prev_hi = prev_lo = 0.0
        for _, day in df.groupby(df.index.date):
            if len(day) <= LOOKBACK + 5:
                prev_hi, prev_lo = float(day["high"].max()), float(day["low"].min())
                continue
            h = day["high"].to_numpy(float)
            lo_ = day["low"].to_numpy(float)
            c = day["close"].to_numpy(float)
            tm = day.index.strftime("%H:%M").to_numpy()
            for i in range(LOOKBACK, len(day)):
                if not (ENTRY_START <= tm[i] < ENTRY_END):
                    continue
                nxt = [j for j in range(i + 1, len(day)) if tm[j] <= EOD]
                if not nxt:
                    continue
                ph, pl = pools(h, lo_, i)
                if prev_hi > 0:
                    ph = ph + [prev_hi]
                if prev_lo > 0:
                    pl = pl + [prev_lo]
                fh, fl = h[nxt], lo_[nxt]
                ceod = c[nxt[-1]]

                took_up = [L for L in ph if h[i] > L]
                if took_up:
                    grp = "sweep" if c[i] < max(took_up) else "breakout"
                    entry, stop = c[i], h[i] * (1 + BUF)
                    risk = stop - entry
                    if risk > 0:
                        rec = {"symbol": sym, "year": day.index[0].year, "group": grp}
                        for k in KS:
                            rec[f"R{k}"] = resolve("up", entry, stop, entry - k * risk, fh, fl, ceod)
                        rec["risk_pts"] = risk
                        rows.append(rec)

                took_dn = [L for L in pl if lo_[i] < L]
                if took_dn:
                    grp = "sweep" if c[i] > min(took_dn) else "breakout"
                    entry, stop = c[i], lo_[i] * (1 - BUF)
                    risk = entry - stop
                    if risk > 0:
                        rec = {"symbol": sym, "year": day.index[0].year, "group": grp}
                        for k in KS:
                            rec[f"R{k}"] = resolve("dn", entry, stop, entry + k * risk, fh, fl, ceod)
                        rec["risk_pts"] = risk
                        rows.append(rec)
            prev_hi, prev_lo = float(day["high"].max()), float(day["low"].min())
        print(f"  {sym} done")

    ev = pd.DataFrame(rows).dropna()
    print(f"\nevents: {len(ev):,}   (median risk {ev['risk_pts'].median():.1f} pts)")

    def summary(d):
        out = {"n": len(d)}
        for k in KS:
            col = d[f"R{k}"]
            out[f"E[R]@{k}"] = round(col.mean(), 3)
            out[f"win%@{k}"] = round(100 * (col > 0).mean(), 1)
        return pd.Series(out)

    print("\n=== expectancy in R-multiples, sweep vs breakout (all data) ===")
    print(ev.groupby("group").apply(summary, include_groups=False).to_string())

    sw = ev[ev["group"] == "sweep"]
    print("\n=== sweeps: per instrument, E[R] by target ===")
    print(sw.groupby("symbol").apply(
        lambda d: pd.Series({f"E[R]@{k}": round(d[f'R{k}'].mean(), 3) for k in KS}
                            | {"n": len(d)}), include_groups=False).to_string())
    print("\n=== sweeps: per year, E[R] by target (stability) ===")
    print(sw.groupby("year").apply(
        lambda d: pd.Series({f"E[R]@{k}": round(d[f'R{k}'].mean(), 3) for k in KS}
                            | {"n": len(d)}), include_groups=False).to_string())

    best = max(KS, key=lambda k: sw[f"R{k}"].mean())
    col = sw[f"R{best}"]
    se = col.std() / np.sqrt(len(col))
    print(f"\n=== best target k={best} ===")
    print(f"  E[R]={col.mean():+.4f}R  se={se:.4f}  t={col.mean()/se:.1f}  n={len(col):,}")
    print(f"  win rate {100*(col>0).mean():.1f}%   median risk {sw['risk_pts'].median():.1f} pts")
    print(f"  -> per trade in points: {col.mean()*sw['risk_pts'].median():+.2f}")
    ev.to_csv(Path(__file__).resolve().parent / "sweep_rr_events.csv", index=False)


if __name__ == "__main__":
    main()
