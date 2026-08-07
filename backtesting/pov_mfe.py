"""
POV Wall-Squeeze — does it give back open profit, and would a stop rule help?
==============================================================================
The same question answered for Judas, but POV is a different animal and the
Judas answer must NOT be assumed to carry over:

                    Judas                    POV
    exit basis      spot levels              PREMIUM levels
    target          2R                       T1 ~1.65R (t2/t3 computed, unused)
    max hold        none (EOD)               45 minutes
    trades/day      1                        several, several legs
    R               ~0.11% of spot           ~3.7 premium points (~4% of premium)

Judas's break-even ratchet won because its P&L was pure tail: 4 of 25 trades
made +11.34R out of +4.73R total, so any upside cap destroyed the mean while
truncating losses cost nothing. POV books at 1.65R on a 45-minute clock, so
it may be the mirror image -- many small wins, no fat tail -- in which case
break-even would scratch winners and a TRAILING stop could help instead.

Method: pair every POV order into round trips, replay each on the option's
own 1-minute bars, and measure MFE / MAE / realised in R, where
R = entry_premium - stop_premium from the strategy's own "Trade entered" log
line (median 3.68 pts; the house value is used where the log has rotated).

Only contracts still in the master can be replayed -- the broker drops
expired symbols -- so this covers the recent window, not all 80 orders.

Usage:
    ./venv/Scripts/python.exe backtesting/pov_mfe.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent
ORDERS = HERE / "pov_orders.csv"
GEOM = HERE / "pov_geometry.csv"
R_FALLBACK_PCT = 0.04          # median R / entry premium across logged trades


def client():
    from openalgo import api
    env = (ROOT / ".env").read_text()
    return api(api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
               host=env.split("OPENALGO_HOST=")[1].split()[0])


def pair_trades(df):
    trades, open_pos = [], {}
    for _, r in df.sort_values("ts").iterrows():
        sym = r["symbol"]
        if r["action"] == "BUY":
            open_pos.setdefault(sym, []).append(r)
        elif open_pos.get(sym):
            b = open_pos[sym].pop(0)
            trades.append({"symbol": sym, "exchange": r["exchange"], "qty": b["qty"],
                           "entry_ts": b["ts"], "exit_ts": r["ts"]})
    return pd.DataFrame(trades)


def rules(hi, lo, close, entry, R):
    """Outcome in R for each candidate exit rule on one premium path.

    Every rule keeps the original stop at -1R and POV's 45-minute clock is
    implicit in the path length (paths run entry -> actual exit).
    """
    out = {}
    # what actually happened
    out["current"] = (close[-1] - entry) / R

    # hard target at T1 = 1.65R (POV's own), and wider ones
    for t in (1.65, 2.5, 3.5):
        res = None
        for h, lw in zip(hi, lo):
            if lw <= entry - R:
                res = -1.0
                break
            if h >= entry + t * R:
                res = t
                break
        out[f"tgt_{t}"] = res if res is not None else (close[-1] - entry) / R

    # break-even once +A R is shown (the Judas fix)
    for a in (0.5, 1.0):
        armed, res = False, None
        for h, lw in zip(hi, lo):
            if not armed and h >= entry + a * R:
                armed = True
            elif armed and lw <= entry:
                res = 0.0
                break
            if lw <= entry - R:
                res = -1.0
                break
        out[f"be_{a}"] = res if res is not None else (close[-1] - entry) / R

    # trail G*R below the running peak once +A R is shown
    for a, g in ((0.5, 0.5), (1.0, 0.5), (1.0, 1.0)):
        peak, armed, res = 0.0, False, None
        for h, lw in zip(hi, lo):
            peak = max(peak, h - entry)
            if not armed and peak >= a * R:
                armed = True
            if armed and lw <= entry + peak - g * R:
                res = (peak - g * R) / R
                break
            if lw <= entry - R:
                res = -1.0
                break
        out[f"trail_{a}_{g}"] = res if res is not None else (close[-1] - entry) / R
    return out


def main():
    orders = pd.read_csv(ORDERS, parse_dates=["ts"])
    trades = pair_trades(orders)
    geom = pd.read_csv(GEOM, parse_dates=["entry_ts"]) if GEOM.exists() else pd.DataFrame()
    print(f"{len(orders)} POV orders -> {len(trades)} round trips "
          f"| geometry for {len(geom)}")

    c = client()
    rows, unfetchable = [], set()
    for _, t in trades.iterrows():
        day = pd.Timestamp(t["entry_ts"]).strftime("%Y-%m-%d")
        try:
            d = c.history(symbol=t["symbol"], exchange=t["exchange"],
                          interval="1m", start_date=day, end_date=day)
        except Exception:
            unfetchable.add(t["symbol"])
            continue
        if not isinstance(d, pd.DataFrame) or d.empty:
            unfetchable.add(t["symbol"])
            continue
        d = d.copy()
        d.index = pd.to_datetime(d.index).tz_localize(None)
        p = d[(d.index >= pd.Timestamp(t["entry_ts"]).floor("min")) &
              (d.index <= pd.Timestamp(t["exit_ts"]).ceil("min"))]
        if len(p) < 2:
            continue
        entry = float(p["close"].iloc[0])
        g = geom[geom["symbol"] == t["symbol"]] if len(geom) else geom
        R = float(entry - g["sl"].iloc[0]) if len(g) else entry * R_FALLBACK_PCT
        if R <= 0:
            R = entry * R_FALLBACK_PCT
        hi, lo, cl = p["high"].values, p["low"].values, p["close"].values
        rec = {"symbol": t["symbol"], "entry_ts": t["entry_ts"], "mins": len(p),
               "entry": round(entry, 2), "R": round(R, 2),
               "mfe_R": round((hi.max() - entry) / R, 2),
               "mae_R": round((lo.min() - entry) / R, 2)}
        rec.update({k: round(v, 3) for k, v in rules(hi, lo, cl, entry, R).items()})
        rows.append(rec)

    if not rows:
        print(f"nothing replayable; expired contracts: {sorted(unfetchable)}")
        return 0
    d = pd.DataFrame(rows)
    d.to_csv(HERE / "pov_mfe_paths.csv", index=False)
    print(f"replayed {len(d)} trades; {len(unfetchable)} contracts expired out of the master\n")
    print(d[["symbol", "entry_ts", "mins", "entry", "R", "mfe_R", "mae_R", "current"]].to_string(index=False))

    print(f"\nMFE: mean {d['mfe_R'].mean():.2f}R median {d['mfe_R'].median():.2f}R | "
          f">=1.65R (T1) {(d['mfe_R'] >= 1.65).sum()}/{len(d)} | "
          f">=2.5R {(d['mfe_R'] >= 2.5).sum()}/{len(d)}")

    cols = [c for c in d.columns if c.startswith(("tgt_", "be_", "trail_"))] + ["current"]
    tab = pd.DataFrame({
        "mean_R": d[cols].mean().round(3),
        "median_R": d[cols].median().round(3),
        "win_rate": (d[cols] > 0).mean().round(2),
        "worst_R": d[cols].min().round(2),
    }).sort_values("mean_R", ascending=False)
    print("\nexit rules on the same premium paths (R per trade):")
    print(tab.to_string())

    best = tab.index[0]
    if best != "current" and len(d) > 2:
        diff = (d[best] - d["current"]).values
        rng = np.random.default_rng(0)
        boot = [rng.choice(diff, len(diff), replace=True).mean() for _ in range(10000)]
        lo_, hi_ = np.percentile(boot, [2.5, 97.5])
        print(f"\npaired: {best} minus current (n={len(diff)})")
        print(f"  mean {diff.mean():+.3f}R  95% CI [{lo_:+.3f}, {hi_:+.3f}]  "
              f"better on {int((diff > 0).sum())}, worse on {int((diff < 0).sum())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
