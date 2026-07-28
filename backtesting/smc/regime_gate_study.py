"""
Does the Markov regime state predict the thing that ruins an option seller?

Context from this repo's measured history:
  - directional SMC/ICT signals: E[R] = -0.007R, t = -0.9, n = 35,874  -> dead
  - blind short ATM straddle:    -31% over 3 months                    -> dead
  - the one lever named as untested in smc/FINDINGS.md: a REGIME filter,
    because a premium seller's losses concentrate on trend / high-range days.

markov-hedge-fund-method (Roan / @RohOnChain) supplies the classifier and
reports 81-88% regime persistence on NIFTY and SENSEX, so the state is
predictable enough to gate on.

A short straddle does not care about direction; it dies from RANGE. So the
testable claim is:

    Sideways days have materially smaller realised range and smaller
    open->close excursion than Bull/Bear days.

Labelling is strictly causal: the regime for day t uses the 20-day rolling
return through day t-1 only. No lookahead.

Reports, per regime:
  - realised range (high-low)/open              <- what a straddle seller pays
  - |close-open|/open                            <- directional damage
  - max adverse excursion max(high-open, open-low)/open
  - P(|move| < X) for a grid of breakeven widths <- the seller's win probability

Usage:
    ../venv/Scripts/python.exe backtesting/smc/regime_gate_study.py
"""

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
WINDOW = 20        # same as the skill default
THRESH = 0.02      # same as the skill default
STATES = {0: "Bear", 1: "Sideways", 2: "Bull"}


def daily_from_5m(sym: str) -> pd.DataFrame:
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, open, high, low, close
        FROM market_data WHERE symbol = ? AND interval = '5m' ORDER BY timestamp
    """, [sym]).fetchdf()
    con.close()
    if df.empty:
        return df
    df = df.set_index("ts")
    d = df.resample("1D").agg({"open": "first", "high": "max",
                               "low": "min", "close": "last"}).dropna()
    return d


def label_causal(close: pd.Series) -> pd.Series:
    """Regime for day t from the rolling return through t-1 (shift(1) = no lookahead)."""
    roll = close.pct_change(WINDOW).shift(1)
    lab = pd.Series(1, index=close.index, dtype=float)
    lab[roll > THRESH] = 2
    lab[roll < -THRESH] = 0
    lab[roll.isna()] = np.nan
    return lab


def main():
    frames = []
    for sym in SYMBOLS:
        d = daily_from_5m(sym)
        if d.empty or len(d) < WINDOW + 30:
            print(f"  {sym}: insufficient")
            continue
        d["regime"] = label_causal(d["close"])
        d = d.dropna(subset=["regime"])
        d["symbol"] = sym
        d["range_pct"] = (d["high"] - d["low"]) / d["open"] * 100
        d["move_pct"] = (d["close"] - d["open"]).abs() / d["open"] * 100
        d["mae_pct"] = np.maximum(d["high"] - d["open"], d["open"] - d["low"]) / d["open"] * 100
        frames.append(d)
        print(f"  {sym:11s} {len(d):4d} days  {d.index[0].date()}..{d.index[-1].date()}")

    ev = pd.concat(frames)
    ev["state"] = ev["regime"].map(STATES)
    print(f"\ntotal day-observations: {len(ev):,}")

    def stats(g):
        return pd.Series({
            "days": len(g),
            "range%_mean": round(g["range_pct"].mean(), 3),
            "range%_med": round(g["range_pct"].median(), 3),
            "move%_mean": round(g["move_pct"].mean(), 3),
            "mae%_mean": round(g["mae_pct"].mean(), 3),
            "range%_p90": round(g["range_pct"].quantile(0.90), 3),
        })

    print("\n=== realised range and excursion by regime (all instruments) ===")
    tbl = ev.groupby("state").apply(stats, include_groups=False)
    print(tbl.reindex(["Bear", "Sideways", "Bull"]).to_string())

    print("\n=== per instrument: mean realised range% by regime ===")
    piv = ev.pivot_table(index="symbol", columns="state", values="range_pct", aggfunc="mean").round(3)
    print(piv.reindex(columns=["Bear", "Sideways", "Bull"]).to_string())

    print("\n=== per year: mean realised range% by regime (stability) ===")
    ev["year"] = ev.index.year
    piv2 = ev.pivot_table(index="year", columns="state", values="range_pct", aggfunc="mean").round(3)
    print(piv2.reindex(columns=["Bear", "Sideways", "Bull"]).to_string())

    print("\n=== seller win probability: P(|close-open|/open < X) by regime ===")
    print(f"  {'X%':>6s} " + "".join(f"{s:>12s}" for s in ("Bear", "Sideways", "Bull")))
    for x in (0.20, 0.30, 0.40, 0.50, 0.75, 1.00):
        row = f"  {x:6.2f} "
        for s in ("Bear", "Sideways", "Bull"):
            g = ev[ev["state"] == s]
            row += f"{100*(g['move_pct'] < x).mean():11.1f}%"
        print(row)

    # significance: Sideways vs trending (Bull+Bear) on realised range
    sw = ev[ev["state"] == "Sideways"]["range_pct"]
    tr = ev[ev["state"] != "Sideways"]["range_pct"]
    diff = tr.mean() - sw.mean()
    se = (sw.var(ddof=1) / len(sw) + tr.var(ddof=1) / len(tr)) ** 0.5
    print(f"\n=== Sideways vs trending (Bull+Bear), realised range ===")
    print(f"  Sideways mean {sw.mean():.3f}%  n={len(sw):,}")
    print(f"  Trending mean {tr.mean():.3f}%  n={len(tr):,}")
    print(f"  difference {diff:+.3f}pp  se={se:.3f}  t={diff/se:.1f}"
          f"  {'SIGNIFICANT' if abs(diff/se) > 3 else 'NOT significant'} (|t|>3)")
    ev.to_csv(Path(__file__).resolve().parent / "regime_gate_days.csv")


if __name__ == "__main__":
    main()
