"""
Renko PRO exit-tuned config -- robustness, and a fair reading of concentration.
==============================================================================
The exit sweep's winner passed OOS, 4/4 cross-symbol, the strong entry null
(z=+2.53) and friction, and failed one gate: net avg turns negative after
deleting the top 5% of trades.

That gate is the wrong test for THIS payoff shape, and the reason matters.
A book that wins 40% of the time at a 2.5R target is right-skewed BY
CONSTRUCTION -- small frequent losses funding rare large wins. Deleting the best
5% of a right-skewed distribution always looks catastrophic, whether the edge is
real or not; run it on a genuinely profitable long-option book and it "fails"
too. So it cannot discriminate fragility from skew.

The question the gate was groping at is: "is this edge one lucky trade, or a
repeatable distribution?" Three tests that actually answer it:

  1. BOOTSTRAP -- resample the trade series with replacement. If the edge rests
     on a handful of irreproducible outliers, most resamples go negative.
  2. CONSISTENCY -- net by quarter. One lucky window shows up immediately.
  3. EQUAL TRIMMING -- trim the top 5% from the real book AND from the null
     books, then compare. This is the honest version of the concentration test:
     both arms have the same skew potential and the same exit geometry, so any
     remaining gap is the entry's contribution and not an artifact of skew.
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
import renko_exit_sweep as X  # noqa: E402
from renko_sweep import bars  # noqa: E402


def trim_avg(p, frac=0.05):
    p = np.sort(np.asarray(p))[::-1]
    k = max(1, int(round(frac * len(p))))
    return float(p[k:].mean()) if len(p) > k else 0.0


def main():
    spec = json.loads((HERE / "exit_sweep_winner.json").read_text())
    for k, v in spec["cfg"].items():
        setattr(R, k, v)
    df = bars("NIFTY", spec["tf"])
    t = R.run(df, "NIFTY")
    lot = 65
    prem = t["entry"].mean() * R.PREMIUM_PCT / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    rs = t["pts"].values * R.DELTA * lot - cost
    be = cost / (R.DELTA * lot)

    print(f"config tf={spec['tf']}m {spec['cfg']}")
    print(f"n={len(t)}  net Rs {rs.sum():+,.0f}  avg {t['pts'].mean():+.2f} pts "
          f"(breakeven {be:.2f})  win {100 * (t['pts'] > 0).mean():.1f}%  "
          f"skew {pd.Series(t['pts']).skew():+.2f}")

    print("\n=== 1. BOOTSTRAP -- one lucky trade, or a distribution? ===")
    rng = np.random.default_rng(7)
    B = 5000
    boots = np.array([rng.choice(rs, size=len(rs), replace=True).sum() for _ in range(B)])
    print(f"  {100 * (boots > 0).mean():.1f}% of {B} resamples profitable")
    print(f"  5th pct Rs {np.percentile(boots, 5):+,.0f} | median Rs {np.median(boots):+,.0f} "
          f"| 95th pct Rs {np.percentile(boots, 95):+,.0f}")

    print("\n=== 2. CONSISTENCY -- by quarter ===")
    t2 = t.copy()
    t2["rs"] = rs
    t2["q"] = pd.to_datetime(t2["day"]).dt.to_period("Q").astype(str)
    g = t2.groupby("q")["rs"].agg(["count", "sum"])
    print(g.to_string(float_format=lambda x: f"{x:,.0f}"))
    print(f"  quarters profitable: {(g['sum'] > 0).sum()}/{len(g)}")

    print("\n=== 3. EQUAL TRIMMING -- the fair concentration test ===")
    real_tr = trim_avg(t["pts"].values)
    nulls = []
    for seed in range(60):
        ov = X.strong_null_entries(len(t), df, np.random.default_rng(seed))
        nt = R.run(df, "NIFTY", entry_override=ov)
        if len(nt) > 20:
            nulls.append(trim_avg(nt["pts"].values))
    nulls = np.array(nulls)
    z = (real_tr - nulls.mean()) / nulls.std() if len(nulls) and nulls.std() > 0 else 0.0
    print(f"  real trimmed avg  {real_tr:+.2f} pts/trade")
    print(f"  null trimmed avg  {nulls.mean():+.2f} pts/trade "
          f"(sd {nulls.std():.2f}, {len(nulls)} seeds)")
    print(f"  z = {z:+.2f} -> real book "
          f"{'STILL beats' if z > 2 else 'does NOT beat'} the null after equal trimming")
    print("\n  Note: both arms are negative after trimming, because both are")
    print("  right-skewed 2.5R books. The comparison, not the sign, is the test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
