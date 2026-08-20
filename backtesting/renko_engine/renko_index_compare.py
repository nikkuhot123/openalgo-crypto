"""
Renko PRO exit-tuned config -- which index actually suits it, and why.
=====================================================================
Raw net rupees is not comparable across indices: lot size differs (NIFTY 65,
BANKNIFTY 30, SENSEX 20, FINNIFTY 65, MIDCPNIFTY 120) and so does premium, so
both the P&L per point AND the capital per lot move together. A bigger number
can just mean a bigger position.

So everything here is normalised three ways:
  - per-trade edge in POINTS relative to the friction hurdle for that symbol
  - R-multiple, which removes the symbol's volatility scale entirely
  - return on the capital actually tied up (premium x lot per position)

NIFTY is the FITTED symbol -- the config was selected on NIFTY in-sample -- so it
is the reference, not a peer. The honest comparison is: how much of NIFTY's
result do the untouched symbols reproduce?
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import renko_engine_backtest as R  # noqa: E402
from renko_sweep import bars  # noqa: E402

SYMS = ["NIFTY", "MIDCPNIFTY", "FINNIFTY", "BANKNIFTY", "SENSEX"]


def stats(sym, tf):
    t = R.run(bars(sym, tf), sym)
    if len(t) == 0:
        return None
    lot = R.LOT[sym]
    prem = t["entry"].mean() * R.PREMIUM_PCT / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    be = cost / (R.DELTA * lot)                    # friction hurdle in points
    rs = t["pts"].values * R.DELTA * lot - cost
    eq = np.cumsum(rs)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    risk = (t["entry"] - t["sl"]).abs().values
    rmult = np.where(risk > 0, t["pts"].values / risk, 0.0)
    cap = prem * lot                               # capital tied per 1 lot
    p = t["pts"].values
    gw, gl = p[p > 0].sum(), -p[p < 0].sum()
    return {
        "sym": sym, "n": len(t), "win": 100 * (p > 0).mean(),
        "pf": gw / gl if gl > 0 else np.inf,
        "avg_pts": p.mean(), "be_pts": be, "edge_x": p.mean() / be,
        "avg_R": rmult.mean(), "net_rs": rs.sum(), "rs_trade": rs.mean(),
        "dd": dd, "cap": cap, "roc": 100 * rs.sum() / cap if cap else 0.0,
        "sharpe": (rs.mean() / rs.std() * np.sqrt(len(rs))) if rs.std() > 0 else 0.0,
        "dd_x": rs.sum() / dd if dd > 0 else np.inf,
        "span": f"{t['day'].min():%Y-%m}..{t['day'].max():%Y-%m}",
    }


def main():
    spec = json.loads((HERE / "exit_sweep_winner.json").read_text())
    for k, v in spec["cfg"].items():
        setattr(R, k, v)
    tf = spec["tf"]
    print(f"exit-tuned config, {tf}m, IDENTICAL parameters on every symbol")
    print(f"  {spec['cfg']}\n")
    rows = [s for s in (stats(x, tf) for x in SYMS) if s]
    d = pd.DataFrame(rows)

    print("=== normalised comparison ===")
    print(f"{'sym':11s} {'n':>5s} {'win%':>6s} {'PF':>5s} {'avg pts':>8s} {'hurdle':>7s} "
          f"{'edge x':>7s} {'avg R':>6s} {'Rs/trade':>9s} {'net Rs':>10s} {'maxDD':>9s} "
          f"{'net/DD':>7s} {'cap/lot':>8s} {'RoC%':>8s} {'Sharpe':>7s}")
    for _, r in d.iterrows():
        print(f"{r['sym']:11s} {r['n']:5.0f} {r['win']:6.1f} {r['pf']:5.2f} "
              f"{r['avg_pts']:8.2f} {r['be_pts']:7.2f} {r['edge_x']:7.2f} {r['avg_R']:6.2f} "
              f"{r['rs_trade']:9,.0f} {r['net_rs']:10,.0f} {r['dd']:9,.0f} {r['dd_x']:7.2f} "
              f"{r['cap']:8,.0f} {r['roc']:8.1f} {r['sharpe']:7.2f}")

    print("\n=== reading it ===")
    d2 = d.sort_values("edge_x", ascending=False)
    print("  by per-trade edge vs its own friction hurdle (scale-free):")
    for _, r in d2.iterrows():
        print(f"    {r['sym']:11s} {r['edge_x']:5.2f}x hurdle   avg {r['avg_R']:+.2f}R   PF {r['pf']:.2f}")
    print("\n  by return on capital tied per lot:")
    for _, r in d.sort_values("roc", ascending=False).iterrows():
        print(f"    {r['sym']:11s} {r['roc']:+8.1f}%  on Rs {r['cap']:,.0f}/lot   "
              f"net/maxDD {r['dd_x']:.2f}   span {r['span']}")
    d.to_csv(HERE / "index_compare.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
