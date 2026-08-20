"""Could Judas rest its stop at the BROKER instead of holding it in-process?
==========================================================================
Judas's stop is a SPOT level, checked in-process every ~5s. A resting broker
order cannot watch spot -- it can only rest on the OPTION premium. So the
question is not "is a broker stop safer" (it obviously survives a crash) but:

    can Judas's spot stop be translated into a premium stop accurately enough
    that the translation does not cost more than the protection is worth?

Evidence used, all live:
  - `Monitoring Trade` lines  -> timestamp, symbol, spot, sl_spot, target
  - `PATH` lines             -> timestamp, symbol, premium, entry premium
  - `Entered Trade` / `Trade P&L` -> realised outcomes
  - `BREAK-EVEN ARMED`       -> when the ratchet moved the stop

Pairing Monitoring and PATH by nearest timestamp gives real (spot, premium)
samples inside live positions -- which is exactly what a spot->premium mapping
has to be built from.

What this measures:
  1. Realised dPremium/dSpot per trade, and how STABLE it is. An unstable delta
     means a fixed premium stop drifts away from the intended spot level.
  2. The premium implied at the stop by that delta, versus the premium actually
     observed nearest the stop. The gap is the translation error, in rupees.
  3. How often Judas was actually left unprotected -- SIGTERM/restart while a
     position was open. That is the benefit side of the trade-off.

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/judas_broker_stop.py [logdir]
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

LOGDIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/judas/jd")

TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
RE_MON = re.compile(
    TS + r".*Monitoring Trade: (\S+) \| Spot: ([\d.]+) \| SL: ([\d.]+)"
)
RE_PATH = re.compile(
    TS + r".*PATH (\S+) prem=([\d.]+) entry=([\d.]+)"
)
RE_ENTRY = re.compile(
    TS + r".*Entered Trade! Spot Entry: ([\d.]+) \| SL: ([\d.]+) \| Target: ([\d.]+)"
    r" \| Opt entry: ([\d.]+)"
)
RE_PNL = re.compile(TS + r".*Trade P&L: .?([-+]?[\d,]+\.?\d*)")
RE_BE = re.compile(TS + r".*BREAK-EVEN ARMED: ([\d.]+)R")
RE_SIGTERM = re.compile(TS + r".*SHUTDOWN SIGNAL RECEIVED")
RE_ACTIVE = re.compile(TS + r".*(Monitoring Trade|Entered Trade)")


def parse():
    mon, path, entries, sigterms, bes = [], [], [], [], []
    for f in sorted(LOGDIR.glob("judas_swing_strategy*.log")):
        sess = f.name
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for ln in txt.splitlines():
            m = RE_MON.search(ln)
            if m:
                mon.append((sess, datetime.fromisoformat(m.group(1)), m.group(2),
                            float(m.group(3)), float(m.group(4))))
                continue
            m = RE_PATH.search(ln)
            if m:
                path.append((sess, datetime.fromisoformat(m.group(1)), m.group(2),
                             float(m.group(3)), float(m.group(4))))
                continue
            m = RE_ENTRY.search(ln)
            if m:
                entries.append((sess, datetime.fromisoformat(m.group(1)),
                                float(m.group(2)), float(m.group(3)),
                                float(m.group(4)), float(m.group(5))))
                continue
            m = RE_BE.search(ln)
            if m:
                bes.append((sess, datetime.fromisoformat(m.group(1)), float(m.group(2))))
                continue
            m = RE_SIGTERM.search(ln)
            if m:
                sigterms.append((sess, datetime.fromisoformat(m.group(1))))
    return (pd.DataFrame(mon, columns=["sess", "ts", "sym", "spot", "sl"]),
            pd.DataFrame(path, columns=["sess", "ts", "sym", "prem", "entry_prem"]),
            pd.DataFrame(entries, columns=["sess", "ts", "entry_spot", "sl_spot",
                                           "target", "entry_prem"]),
            pd.DataFrame(bes, columns=["sess", "ts", "r"]),
            pd.DataFrame(sigterms, columns=["sess", "ts"]))


def main():
    mon, path, entries, bes, sig = parse()
    print(f"logs      : {len(list(LOGDIR.glob('judas*.log')))} sessions")
    print(f"monitoring: {len(mon):5d} lines  (spot + live stop)")
    print(f"PATH      : {len(path):5d} lines  (premium)")
    print(f"entries   : {len(entries):5d} | break-even arms: {len(bes)} | SIGTERMs: {len(sig)}")
    if mon.empty or path.empty:
        print("\nnot enough paired data")
        return 1

    # ---- 1. pair spot and premium by nearest timestamp, per symbol ----------
    rows = []
    for sym, p in path.groupby("sym"):
        m = mon[mon["sym"] == sym].sort_values("ts")
        if m.empty:
            continue
        p = p.sort_values("ts")
        j = pd.merge_asof(p, m[["ts", "spot", "sl"]], on="ts",
                          direction="nearest", tolerance=pd.Timedelta("20s"))
        j = j.dropna(subset=["spot"])
        j["sym"] = sym
        rows.append(j)
    if not rows:
        print("\nno pairs within tolerance")
        return 1
    j = pd.concat(rows, ignore_index=True)
    print(f"\npaired samples inside live positions: {len(j)} "
          f"across {j['sym'].nunique()} contracts")

    # ---- 2. realised dPremium/dSpot per contract ----------------------------
    print("\n=== 1. realised |dPremium/dSpot| per contract (the mapping a broker "
          "stop depends on) ===")
    print(f"{'contract':26s} {'n':>4s} {'spot rng':>9s} {'prem rng':>9s} "
          f"{'slope':>7s} {'R^2':>6s}")
    slopes = []
    for sym, g in j.groupby("sym"):
        if len(g) < 8:
            continue
        x, y = g["spot"].values, g["prem"].values
        if x.std() < 1e-9:
            continue
        b, a = np.polyfit(x, y, 1)
        pred = a + b * x
        ss = 1 - ((y - pred) ** 2).sum() / max(((y - y.mean()) ** 2).sum(), 1e-9)
        is_pe = sym.endswith("PE")
        slopes.append((sym, abs(b), ss, len(g), is_pe))
        print(f"{sym:26s} {len(g):4d} {x.max()-x.min():9.1f} {y.max()-y.min():9.1f} "
              f"{b:+7.3f} {ss:6.2f}")
    if slopes:
        mags = np.array([s[1] for s in slopes])
        print(f"\n  |slope| median {np.median(mags):.3f}  min {mags.min():.3f}  "
              f"max {mags.max():.3f}  spread {mags.max()/max(mags.min(),1e-9):.1f}x")
        print(f"  R^2 median {np.median([s[2] for s in slopes]):.2f}")

    # ---- 3. translation error at the stop ----------------------------------
    print("\n=== 2. what a premium stop would have been set to, vs reality ===")
    print("For each contract: take the FIRST paired sample as the reference the")
    print("strategy would have used, project the premium at sl_spot with that")
    print("contract's own realised slope, then compare against the premium")
    print("actually seen when spot came closest to sl_spot.")
    print(f"\n{'contract':26s} {'sl_spot':>9s} {'proj prem':>9s} {'actual':>8s} "
          f"{'err':>7s} {'err %':>7s}")
    errs = []
    for sym, g in j.groupby("sym"):
        if len(g) < 8:
            continue
        g = g.sort_values("ts")
        x, y = g["spot"].values, g["prem"].values
        if x.std() < 1e-9:
            continue
        b, a = np.polyfit(x, y, 1)
        sl = float(g["sl"].iloc[0])
        proj = a + b * sl
        k = int(np.argmin(np.abs(x - sl)))
        actual = y[k]
        if abs(x[k] - sl) > 0.35 * (x.max() - x.min() + 1e-9):
            continue                      # spot never came near the stop
        err = proj - actual
        errs.append((sym, err, 100 * err / max(actual, 1e-9)))
        print(f"{sym:26s} {sl:9.1f} {proj:9.2f} {actual:8.2f} "
              f"{err:+7.2f} {100*err/max(actual,1e-9):+6.1f}%")
    if errs:
        e = np.array([abs(x[1]) for x in errs])
        ep = np.array([abs(x[2]) for x in errs])
        print(f"\n  |error| median Rs {np.median(e):.2f}/unit  "
              f"({np.median(ep):.1f}% of premium)  worst Rs {e.max():.2f} ({ep.max():.1f}%)")
    else:
        print("  no contract had spot approach its stop closely enough to measure")

    # ---- 4. how often was the in-process stop actually absent? -------------
    print("\n=== 3. the benefit side: was Judas ever left unprotected? ===")
    unprotected = 0
    for _, s in sig.iterrows():
        same = mon[(mon["sess"] == s["sess"])]
        if same.empty:
            continue
        before = same[same["ts"] <= s["ts"]]
        after = same[same["ts"] > s["ts"]]
        # a position was being monitored right up to the shutdown and never after
        if not before.empty and (s["ts"] - before["ts"].max()).total_seconds() < 120 \
                and after.empty:
            unprotected += 1
            print(f"  {s['sess']}: SIGTERM at {s['ts']:%Y-%m-%d %H:%M} with a "
                  f"position monitored {(s['ts']-before['ts'].max()).total_seconds():.0f}s earlier")
    print(f"\n  sessions where a SIGTERM arrived with a live monitored position: "
          f"{unprotected} / {len(sig)} shutdowns")
    print("  (Judas's shutdown handler closes the position, so these were handled --")
    print("   the exposure is a CRASH or SIGKILL, where no handler runs at all.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def disaster_stop_study(logdir=None):
    """If a translated stop is too inaccurate, what about a DISASTER stop?

    Keep the precise spot stop in-process, and rest a deliberately WIDE premium
    stop at the broker whose only job is to fire when the process is gone. For
    that to be free, it must sit beyond the worst adverse premium excursion any
    normally-managed trade ever reaches -- otherwise it would pre-empt the real
    stop and change the strategy.
    """
    global LOGDIR
    if logdir:
        LOGDIR = Path(logdir)
    mon, path, entries, bes, sig = parse()
    if path.empty:
        print("no PATH data")
        return 1
    print("\n" + "=" * 74)
    print("=== 4. DISASTER STOP: worst adverse premium excursion per contract ===")
    print("A resting stop must sit BEYOND all of these to avoid pre-empting the")
    print("in-process spot stop.\n")
    print(f"{'contract':26s} {'n':>4s} {'entry':>8s} {'min prem':>9s} {'worst draw':>11s} "
          f"{'max prem':>9s} {'best run':>9s}")
    draws = []
    for sym, g in path.groupby("sym"):
        e = float(g["entry_prem"].iloc[0])
        if e <= 0:
            continue
        lo, hi = float(g["prem"].min()), float(g["prem"].max())
        dd = 100.0 * (lo - e) / e
        ru = 100.0 * (hi - e) / e
        draws.append((sym, dd, ru, len(g)))
        print(f"{sym:26s} {len(g):4d} {e:8.2f} {lo:9.2f} {dd:+10.1f}% {hi:9.2f} {ru:+8.1f}%")
    if not draws:
        return 1
    d = np.array([x[1] for x in draws])
    print(f"\n  worst adverse excursion across contracts: {d.min():.1f}%")
    print(f"  median adverse excursion               : {np.median(d):.1f}%")
    for lvl in (40, 50, 60, 70):
        n_hit = int((d <= -lvl).sum())
        print(f"  a resting stop at -{lvl}% of entry premium would have fired on "
              f"{n_hit}/{len(d)} contracts")
    print("\n  Judas's own hard risk cap for reference: a 1R spot move, and the")
    print("  measured mean outcome of the whole book is only +0.33R -- so any")
    print("  resting level that fires in normal operation is expensive.")
    return 0


if __name__ == "__main__" and "--disaster" in sys.argv:
    sys.exit(disaster_stop_study("/tmp/judas/jd"))
