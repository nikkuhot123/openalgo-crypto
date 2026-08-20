"""Can the "other indexes" be forward-tested? Honest economics, measured inputs.
==============================================================================
The section-6b index ranking priced EVERY index as if a weekly ATM option
existed, at 0.45% of spot with delta 0.358. Both inputs are now measured against
real Volrix fills, and neither holds for the indices in question:

  measured, 2026-02..2026-08, real premiums
  ----------------------------------------------------------------
  realised |dOpt| per favourable index point   0.533 NIFTY (weekly)
                                               0.520 SENSEX (weekly)
                                               0.516 BANKNIFTY (monthly)
      -> ~0.52 everywhere, not 0.358. The model UNDERSTATED rupees per point
         by ~46%, which is why SENSEX real beat SENSEX model.

  MONTHLY ATM premium as % of spot             1.469%  (BANKNIFTY, n=138)
      -> 3.26x the 0.45% weekly figure the model assumed.

That second number is what decides the question. BANKNIFTY, FINNIFTY and
MIDCPNIFTY have NO weekly options (verified on the 2026-03-04 chain: BANKNIFTY
nearest expiry is monthly 2026-03-30). So for those three the only tradeable
instrument carries 3.26x the premium -- which triples both the friction hurdle
and the capital tied per lot, while delta only rises from the assumed 0.358 to
the measured 0.52.

This recomputes each index's per-trade edge against ITS OWN correct hurdle.
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

DELTA_MEAS = 0.52          # measured across weekly and monthly, all three symbols
PREM_WEEKLY = 0.45         # % of spot, model assumption, consistent with weekly fills
PREM_MONTHLY = 1.469       # % of spot, MEASURED on 138 real monthly BANKNIFTY fills

# Which expiry each index can actually trade, from the live chain
EXPIRY = {"NIFTY": "weekly", "SENSEX": "weekly",
          "BANKNIFTY": "monthly", "FINNIFTY": "monthly", "MIDCPNIFTY": "monthly"}
VOLRIX_OK = {"NIFTY", "BANKNIFTY", "SENSEX"}


def econ(sym, tf):
    t = R.run(bars(sym, tf), sym)
    if len(t) == 0:
        return None
    lot = R.LOT[sym]
    prem_pct = PREM_WEEKLY if EXPIRY[sym] == "weekly" else PREM_MONTHLY
    prem = t["entry"].mean() * prem_pct / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    rs_pt = DELTA_MEAS * lot
    hurdle = cost / rs_pt
    p = t["pts"].values
    rs = p * rs_pt - cost
    eq = np.cumsum(rs)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    cap = prem * lot
    return {"sym": sym, "exp": EXPIRY[sym], "n": len(p), "avg": p.mean(),
            "hurdle": hurdle, "edge": p.mean() / hurdle, "net": rs.sum(),
            "dd": dd, "cap": cap, "ndd": rs.sum() / dd if dd > 0 else np.inf,
            "verified": "yes" if sym in VOLRIX_OK else "NO"}


def main():
    spec = json.loads((HERE / "exit_sweep_winner.json").read_text())
    for k, v in spec["cfg"].items():
        setattr(R, k, v)
    tf = spec["tf"]
    print(f"exit-tuned config {tf}m | delta {DELTA_MEAS} (measured) | "
          f"weekly prem {PREM_WEEKLY}% / monthly prem {PREM_MONTHLY}% (measured)\n")
    print(f"{'sym':11s} {'expiry':8s} {'n':>4s} {'avg pts':>8s} {'hurdle':>7s} "
          f"{'edge x':>7s} {'cap/lot':>9s} {'net Rs':>11s} {'net/DD':>7s} {'real-prem?':>10s}")
    rows = []
    for s in ("NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        r = econ(s, tf)
        if r:
            rows.append(r)
            print(f"{r['sym']:11s} {r['exp']:8s} {r['n']:4d} {r['avg']:8.2f} {r['hurdle']:7.2f} "
                  f"{r['edge']:7.2f} {r['cap']:9,.0f} {r['net']:+11,.0f} {r['ndd']:7.2f} "
                  f"{r['verified']:>10s}")

    print("\n--- what changed vs the section-6b ranking ---")
    print("  6b priced all five as weekly ATM at 0.45% / delta 0.358. Corrected:")
    for r in rows:
        if r["exp"] == "monthly":
            print(f"    {r['sym']:11s} hurdle {r['hurdle']:.2f} pts (was ~{r['hurdle']/3.26*0.358/DELTA_MEAS:.2f}) "
                  f"-> edge {r['edge']:.2f}x, capital {r['cap']:,.0f}/lot")
    print("\n  Only NIFTY and SENSEX have weekly options and Volrix real-premium")
    print("  verification. FINNIFTY / MIDCPNIFTY have NEITHER -- unsupported on")
    print("  Volrix, so every number above for them is model-only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
