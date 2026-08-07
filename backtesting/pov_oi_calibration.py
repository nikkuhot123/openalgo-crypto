"""
POV Wall-Squeeze — port the OI gates from absolute counts to percent-of-OI
===========================================================================
POV trades NIFTY and never trades SENSEX. Cause (measured 2026-08-07): both
OI gates are absolute constants calibrated to NIFTY's open-interest scale,
and SENSEX carries ~18-33x less OI on the same moneyness.

    PRE_OI_MIN       = 50000   pre-gate: sum of positive OI change over the
                               last PRE_LOOKBACK candles must exceed this,
                               else the evaluator returns score 0 immediately
    OI_ABS_THRESHOLD = 30000   condition c2: |OI change| must be BELOW this

On 2026-08-07 those two constants behaved in opposite directions per book:

    gate                     NIFTY      SENSEX
    pre-gate  >= 50,000      86% pass   34% pass  (0/6 legs at the sampled poll)
    c2        <  30,000      16% pass   86% pass

Normalised by each contract's own OI the two books are nearly identical
(median |dOI|/OI 1.69% vs 1.84%), so the divergence is pure scale. Fixing
only the pre-gate would be worse than leaving it: SENSEX would still collect
c2 for free 86% of the time and reach the 4/5 entry bar on three of the
remaining four conditions -- a looser strategy than the one that works.

This script derives percent-of-OI equivalents from real 5-minute option bars
over several weeks, choosing the percentages that REPRODUCE NIFTY's current
pass rates. NIFTY must not change behaviour; it is the working book.

Option history is safe to request over long ranges (verified: a 07-24..08-07
request returns bars identical to a single-day request for the same session).
That is NOT true of the index endpoint -- see redbar_overnight.fetch_5m_live.

Usage:
    ./venv/Scripts/python.exe backtesting/pov_oi_calibration.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PRE_OI_MIN = 50_000
OI_ABS_THRESHOLD = 30_000
PRE_LOOKBACK = 4

# Current + next weekly on each book, ATM +/- 2 strikes, both sides -- the
# exact legs POV tracks.
BOOKS = {
    "NIFTY": {"exchange": "NFO", "step": 50, "atm": 24650,
              "expiries": ["11AUG26", "18AUG26", "25AUG26"]},
    "SENSEX": {"exchange": "BFO", "step": 100, "atm": 78600,
               "expiries": ["13AUG26", "20AUG26"]},
}
START, END = "2026-07-01", "2026-08-07"


def client():
    from openalgo import api
    env = (ROOT / ".env").read_text()
    return api(api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
               host=env.split("OPENALGO_HOST=")[1].split()[0])


def leg_symbols(book, cfg):
    out = []
    for exp in cfg["expiries"]:
        for i in (-2, -1, 0, 1, 2):
            strike = cfg["atm"] + i * cfg["step"]
            for side in ("CE", "PE"):
                out.append(f"{book}{exp}{strike}{side}")
    return out


def collect(c, book, cfg):
    """pos4/OI and |dOI|/OI samples plus their absolute counterparts."""
    pre_abs, pre_rel, c2_abs, c2_rel, nbars = [], [], [], [], 0
    for sym in leg_symbols(book, cfg):
        try:
            d = c.history(symbol=sym, exchange=cfg["exchange"], interval="5m",
                          start_date=START, end_date=END)
        except Exception:
            continue
        if not isinstance(d, pd.DataFrame) or d.empty or "oi" not in d.columns:
            continue
        d = d[d["oi"] > 0]
        if len(d) < PRE_LOOKBACK + 2:
            continue
        nbars += len(d)
        chg = d["oi"].diff().fillna(0)
        pos4 = chg.clip(lower=0).rolling(PRE_LOOKBACK).sum().dropna()
        oi4 = d["oi"].reindex(pos4.index).clip(lower=1)
        pre_abs += list(pos4.values)
        pre_rel += list((pos4 / oi4).values)
        oi = d["oi"].clip(lower=1)
        c2_abs += list(chg.abs().values)
        c2_rel += list((chg.abs() / oi).values)
    return (np.array(pre_abs), np.array(pre_rel),
            np.array(c2_abs), np.array(c2_rel), nbars)


def main():
    c = client()
    data = {}
    for book, cfg in BOOKS.items():
        print(f"collecting {book} ...", flush=True)
        data[book] = collect(c, book, cfg)
        print(f"  {data[book][4]:,} bars over {len(leg_symbols(book, cfg))} legs")

    print(f"\n{'book':7s} {'pre>=50k':>9s} {'c2<30k':>8s}   "
          f"{'med pos4/oi':>12s} {'med |doi|/oi':>13s}")
    for book, (pa, pr, ca, cr, _) in data.items():
        print(f"{book:7s} {100*(pa >= PRE_OI_MIN).mean():8.0f}% "
              f"{100*(ca < OI_ABS_THRESHOLD).mean():7.0f}%   "
              f"{100*np.median(pr):11.2f}% {100*np.median(cr):12.2f}%")

    # Calibrate on NIFTY: pick percentages that reproduce its CURRENT rates.
    npa, npr, nca, ncr, _ = data["NIFTY"]
    pre_target = (npa >= PRE_OI_MIN).mean()
    c2_target = (nca < OI_ABS_THRESHOLD).mean()
    pre_pct = float(np.percentile(npr, 100 * (1 - pre_target)))
    c2_pct = float(np.percentile(ncr, 100 * c2_target))

    print(f"\nNIFTY current pass rates -> pre {100*pre_target:.0f}%, c2 {100*c2_target:.0f}%")
    print(f"percent-of-OI equivalents:")
    print(f"  PRE_OI_PCT = {pre_pct*100:.3f}%   (pos4 >= this share of OI)")
    print(f"  OI_PCT_MAX = {c2_pct*100:.3f}%   (|dOI| < this share of OI)")

    print(f"\n{'book':7s} {'pre old':>8s} {'pre new':>8s}   {'c2 old':>7s} {'c2 new':>7s}")
    for book, (pa, pr, ca, cr, _) in data.items():
        print(f"{book:7s} {100*(pa >= PRE_OI_MIN).mean():7.0f}% {100*(pr >= pre_pct).mean():7.0f}%   "
              f"{100*(ca < OI_ABS_THRESHOLD).mean():6.0f}% {100*(cr < c2_pct).mean():6.0f}%")

    print("\nJoint pre-gate AND c2 (the OI-driven part of the entry test):")
    for book, (pa, pr, ca, cr, _) in data.items():
        n = min(len(pa), len(ca))
        old = ((pa[:n] >= PRE_OI_MIN) & (ca[:n] < OI_ABS_THRESHOLD)).mean()
        new = ((pr[:n] >= pre_pct) & (cr[:n] < c2_pct)).mean()
        print(f"  {book:7s} old {100*old:5.1f}%   new {100*new:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
