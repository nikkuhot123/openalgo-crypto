"""Why does OpenAlgo (index points) say strong and Volrix (real premiums) say no?
================================================================================
Two candidate explanations, and they need separating before either result can be
trusted:

  A. REGIME. The offline run covers 2023-04..2026-05 (3.3y). The Volrix run
     covers 2026-02..2026-08. The quarterly breakdown already flagged 2026Q2 as
     the WORST quarter of the entire offline sample (-Rs 12,269), so the Volrix
     window sits on the offline model's weakest patch. If the offline engine is
     ALSO negative on that window, the disagreement is about WHEN, not about
     premiums.

  B. PREMIUM TRANSLATION. The offline engine converts index points to rupees with
     a constant delta (0.358) and a flat premium assumption (0.45% of index). If
     the realised rupees per index point are far below DELTA*lot, the translation
     is the problem regardless of window.

This script measures both:
  1. Offline results restricted to the EXACT overlap window, every index.
  2. The offline model's own claim in rupees for that window, so it can be put
     next to the Volrix number directly.
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

W_START = "2026-02-20"
SYMS = ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]


def block(sym, tf, lo, hi, label):
    t = R.run(bars(sym, tf), sym)
    if len(t) == 0:
        return None
    t = t.copy()
    t["day"] = pd.to_datetime(t["day"])
    w = t[(t["day"] >= pd.Timestamp(lo)) & (t["day"] <= pd.Timestamp(hi))]
    if len(w) == 0:
        return None
    lot = R.LOT[sym]
    prem = w["entry"].mean() * R.PREMIUM_PCT / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    rs = w["pts"].values * R.DELTA * lot - cost
    eq = np.cumsum(rs)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    p = w["pts"].values
    gw, gl = p[p > 0].sum(), -p[p < 0].sum()
    return {"label": label, "sym": sym, "n": len(w),
            "win": 100 * (p > 0).mean(), "pf": gw / gl if gl > 0 else np.inf,
            "pts": p.sum(), "avg": p.mean(), "rs": rs.sum(), "dd": dd,
            "span": f"{w['day'].min():%Y-%m-%d}..{w['day'].max():%Y-%m-%d}"}


def main():
    spec = json.loads((HERE / "exit_sweep_winner.json").read_text())
    for k, v in spec["cfg"].items():
        setattr(R, k, v)
    tf = spec["tf"]
    end = str(bars("NIFTY", tf).index[-1].date())
    print(f"exit-tuned config {tf}m | local data ends {end}")
    print(f"overlap window with the Volrix run: {W_START}..{end}\n")

    print(f"{'sym':11s} {'window':24s} {'n':>4s} {'win%':>6s} {'PF':>5s} "
          f"{'pts':>9s} {'avg':>7s} {'model Rs':>11s} {'maxDD':>10s}")
    rows = []
    for s in SYMS:
        for label, lo, hi in (("FULL 2023-04..", "2000-01-01", "2100-01-01"),
                              ("OVERLAP only", W_START, end)):
            r = block(s, tf, lo, hi, label)
            if r:
                rows.append(r)
                print(f"{s:11s} {r['label']:24s} {r['n']:4d} {r['win']:6.1f} {r['pf']:5.2f} "
                      f"{r['pts']:+9.1f} {r['avg']:+7.2f} {r['rs']:+11,.0f} {r['dd']:10,.0f}")
        print()

    print("=" * 92)
    print("The comparison that matters -- SAME WINDOW, model rupees vs Volrix real premiums:")
    print(f"{'sym':11s} {'OpenAlgo model Rs':>19s} {'Volrix real Rs':>16s} {'gap':>12s}")
    volrix = {"NIFTY": -48533.0, "SENSEX": 18483.0}   # 0.25% slip + costs, 02-20..08-19
    for s in ("NIFTY", "SENSEX"):
        r = block(s, tf, W_START, end, "OVERLAP only")
        if r and s in volrix:
            print(f"{s:11s} {r['rs']:+19,.0f} {volrix[s]:+16,.0f} {volrix[s] - r['rs']:+12,.0f}")
    print("\nNote: the Volrix column spans 02-20..08-19; the model column stops at")
    print(f"{end} because local 5m cache ends there. Matched-window Volrix runs follow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
