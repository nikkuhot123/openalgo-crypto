"""
Renko PRO -- EXIT + FREQUENCY sweep, entries held FIXED
=======================================================
Premise under test (user, 2026-08-19): "this indicator picks entry signals only,
ignore its exit logic -- the target and SL you need to tune and decide, as it
has a good entry system."

That is a fair criticism of the earlier sweep, twice over:

1. The port HARDCODED the two exits the Pine happens to ship -- stop at the
   previous candle, T2 at the Renko structure. The Pine's own ranking calls that
   target the WORST of the six it offers (T2 fill 5.8%, expectancy -0.41R) and
   ATR the best (23.9%). The 2026-08-19 result showed the symptom exactly: 34 of
   996 T2s filled, and those 34 carried 97% of net points. Tuning the entry
   while leaving the worst target in place tested the wrong half.
2. The earlier random-entry null randomised TIMING but inherited the strategy's
   own day, direction and EMA side. So it never tested whether the entry picks
   the right day or the right side -- it only tested bar precision.

So: entries are frozen at the engine's OWN defaults (confluence ON, EMA filter
ON, X/gap filters ON, brick 0.66%) and the entire exit surface is swept instead.

TRADE COUNT IS PRICED IN, not free (user, 2026-08-19: "also consider optimized
trade numbers of trade"). Selection is on net RUPEES after the measured option
friction -- delta 0.358, statutory 0.12% x2, spread 0.41% -- which costs about
Rs 43.72 per round trip, i.e. a 1.88-index-point hurdle EVERY trade. Ranking by
index points, as the last sweep did, treats a 1,000-trade book and a 200-trade
book as equivalent when they are not. MAX_TRADES_DAY and COOLDOWN_BARS are
swept as first-class parameters for the same reason.

PRE-REGISTERED, again fixed before any result is read:
  Select the single best config by IS net rupees (first 60% of sessions,
  NIFTY, minimum 40 IS trades). Then the same five gates as before, plus the
  stronger null the last run lacked:
    G3b  STRONG NULL -- entries drawn uniformly from ALL bars of ALL sessions,
         matched only on total count. Randomises day, direction AND timing, so
         it tests the entry system as a whole rather than its bar precision.
         Needs z > 2 on net rupees to claim the entry adds anything.

Usage:
    ./venv/Scripts/python.exe backtesting/renko_engine/renko_exit_sweep.py sweep --tf 15,30
    ./venv/Scripts/python.exe backtesting/renko_engine/renko_exit_sweep.py validate
"""
import argparse
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import renko_engine_backtest as R  # noqa: E402
from renko_sweep import bars, metrics, split_days  # noqa: E402

OUT = HERE / "exit_sweep_results.csv"
WINNER = HERE / "exit_sweep_winner.json"
MIN_IS_TRADES = 40
N_PERM = 200
Z_GATE = 2.0
TRANSFER = ["SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

# Conditional grid: a full cross product would be 100k+ runs, most of them
# duplicates (SL_ATR_MULT does nothing unless SL_TYPE is atr, and so on).
SL_OPTS = [("prev_candle", 1.5), ("red_bar_opposite", 1.5), ("fixed", 1.5),
           ("atr", 1.0), ("atr", 1.5), ("atr", 2.5)]
T2_OPTS = [("renko", 1.5, 4.0), ("atr", 1.5, 4.0), ("atr", 3.0, 4.0),
           ("fixed_rr", 1.5, 2.0), ("fixed_rr", 1.5, 4.0),
           ("next_level_capped", 1.5, 4.0), ("eod", 1.5, 4.0)]
T1_OPTS = [(False, 1.5), (True, 1.0), (True, 1.5), (True, 2.5)]
TRAIL_OPTS = ["none", "breakeven_1r", "atr", "ema"]
MAXTRADES = [1, 2, 3]
COOLDOWN = [0, 6]


def combos():
    out = []
    for (slt, sla), (tm, ta_, trr), (t1e, t1r), tr, mt, cd in itertools.product(
            SL_OPTS, T2_OPTS, T1_OPTS, TRAIL_OPTS, MAXTRADES, COOLDOWN):
        out.append({"SL_TYPE": slt, "SL_ATR_MULT": sla, "TARGET_MODE": tm,
                    "T2_ATR_MULT": ta_, "T2_RR": trr, "T1_ENABLE": t1e,
                    "T1_RR": t1r, "TRAIL_MODE": tr, "MAX_TRADES_DAY": mt,
                    "COOLDOWN_BARS": cd})
    return out


KEYS = list(combos()[0])


def net_rs(t, symbol="NIFTY"):
    """Net rupees after measured friction. Per-TRADE cost, so a config that
    fires more often must earn more to stand still."""
    if t is None or len(t) == 0:
        return 0.0, 0.0
    lot = R.LOT.get(symbol.upper(), 65)
    prem = t["entry"].mean() * R.PREMIUM_PCT / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    rs = t["pts"].values * R.DELTA * lot - cost
    return float(rs.sum()), float(cost / (R.DELTA * lot))


def _one(job):
    tf, cfg = job
    for k, v in cfg.items():
        setattr(R, k, v)
    df = bars("NIFTY", tf)
    cut = split_days(df)
    t = R.run(df[df["day"].values < cut], "NIFTY")
    m = metrics(t)
    rs, be = net_rs(t)
    return {"tf": tf, **cfg, **{f"is_{k}": v for k, v in m.items()},
            "is_rs": rs, "be_pts": be,
            "is_avg": (m["pts"] / m["n"]) if m["n"] else 0.0}


def do_sweep(tfs):
    cs = combos()
    jobs = [(tf, c) for tf in tfs for c in cs]
    print(f"exit configs {len(cs)} x tf {tfs} = {len(jobs)} runs | entries FROZEN at engine defaults")
    print("selection = IS net RUPEES after friction (trade count is a cost)\n")
    rows = []
    with ProcessPoolExecutor(max_workers=14) as ex:
        for i, r in enumerate(ex.map(_one, jobs, chunksize=8), 1):
            rows.append(r)
            if i % 1000 == 0:
                print(f"  {i}/{len(jobs)}")
    d = pd.DataFrame(rows)
    if OUT.exists():
        d = pd.concat([pd.read_csv(OUT), d], ignore_index=True)
    d.to_csv(OUT, index=False)

    ok = d[d["is_n"] >= MIN_IS_TRADES].copy()
    print(f"\n{len(ok)}/{len(d)} configs cleared n_IS >= {MIN_IS_TRADES}")
    print(f"positive in Rs in-sample: {(ok['is_rs'] > 0).sum()}/{len(ok)} "
          f"({100 * (ok['is_rs'] > 0).mean():.1f}%)")
    print(f"positive in POINTS in-sample: {(ok['is_pts'] > 0).sum()}/{len(ok)} "
          f"({100 * (ok['is_pts'] > 0).mean():.1f}%)   <- friction is the difference")
    top = ok.sort_values("is_rs", ascending=False).head(12)
    cols = ["tf", "SL_TYPE", "SL_ATR_MULT", "TARGET_MODE", "T2_ATR_MULT", "T2_RR",
            "T1_ENABLE", "T1_RR", "TRAIL_MODE", "MAX_TRADES_DAY", "COOLDOWN_BARS",
            "is_n", "is_win", "is_pf", "is_avg", "is_rs"]
    print("\n--- top 12 IN-SAMPLE by net Rs ---")
    print(top[cols].to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    w = top.iloc[0]
    cfg = {}
    for k in KEYS:
        v = w[k]
        cfg[k] = bool(v) if isinstance(cs[0][k], bool) else (
            int(v) if isinstance(cs[0][k], int) else (
                float(v) if isinstance(cs[0][k], float) else str(v)))
    WINNER.write_text(json.dumps({"tf": int(w["tf"]), "cfg": cfg,
                                  "is_rs": float(w["is_rs"]), "is_n": int(w["is_n"]),
                                  "is_avg": float(w["is_avg"])}, indent=2))
    print(f"\nwinner -> tf={int(w['tf'])}m {cfg}")
    return 0


# ---------------------------------------------------------------- validation

def strong_null_entries(n_trades, df, rng):
    """Random day, random direction, random timing -- the whole entry system.

    The earlier null inherited the strategy's chosen sessions and sides, so it
    could only ever measure bar precision. This one draws entries uniformly from
    every non-terminal bar in the history and assigns the side at random,
    matching ONLY the total trade count.
    """
    days = df["day"].values
    last = {d: np.where(days == d)[0][-1] for d in np.unique(days)}
    lastset = set(last.values())
    pool = np.array([i for i in range(len(df)) if i not in lastset])
    pick = rng.choice(pool, size=min(n_trades, len(pool)), replace=False)
    return {int(i): ("long" if rng.random() < 0.5 else "short") for i in pick}


def _perm_strong(seed):
    spec = json.loads(WINNER.read_text())
    for k, v in spec["cfg"].items():
        setattr(R, k, v)
    df = bars("NIFTY", spec["tf"])
    real = R.run(df, "NIFTY")
    ov = strong_null_entries(len(real), df, np.random.default_rng(seed))
    t = R.run(df, "NIFTY", entry_override=ov)
    rs, _ = net_rs(t)
    m = metrics(t)
    return {"rs": rs, "pts": m["pts"], "sharpe": m["sharpe"], "n": m["n"]}


def do_validate():
    spec = json.loads(WINNER.read_text())
    tf, cfg = spec["tf"], spec["cfg"]
    for k, v in cfg.items():
        setattr(R, k, v)
    print(f"WINNER tf={tf}m")
    for k in KEYS:
        print(f"   {k:16s} {cfg[k]}")
    df = bars("NIFTY", tf)
    cut = split_days(df)
    full = R.run(df, "NIFTY")
    oos = full[full["day"].values >= cut]
    rs_full, be = net_rs(full)
    rs_oos, _ = net_rs(oos)
    m_full, m_oos = metrics(full), metrics(oos)
    print(f"\nIS  net Rs {spec['is_rs']:+,.0f} over n={spec['is_n']} ({spec['is_avg']:+.2f} pts/trade)")
    print(f"friction breakeven {be:.2f} pts/trade\n")
    v = {}

    v["G1_oos"] = rs_oos > 0
    print(f"G1 OOS        n={m_oos['n']:4d} win={m_oos['win']:4.1f}% PF={m_oos['pf']:4.2f} "
          f"avg={m_oos['pts'] / max(m_oos['n'], 1):+5.2f}pts  net Rs {rs_oos:+,.0f} "
          f"-> {'PASS' if v['G1_oos'] else 'FAIL'}")

    print("G2 TRANSFER")
    npass = 0
    for s in TRANSFER:
        try:
            tt = R.run(bars(s, tf), s)
            r, _ = net_rs(tt, s)
            m = metrics(tt)
            good = r > 0 and m["n"] > 0
            npass += good
            print(f"     {s:11s} n={m['n']:4d} win={m['win']:4.1f}% PF={m['pf']:4.2f} "
                  f"net Rs {r:+,.0f}  {'+' if good else '-'}")
        except Exception as e:
            print(f"     {s:11s} unavailable ({type(e).__name__})")
    v["G2_transfer"] = npass >= 2
    print(f"   {npass}/{len(TRANSFER)} -> {'PASS' if v['G2_transfer'] else 'FAIL'}")

    print(f"G3b STRONG NULL  real net Rs {rs_full:+,.0f} | {N_PERM} permutations "
          f"(random day + direction + timing)")
    with ProcessPoolExecutor(max_workers=14) as ex:
        nulls = list(ex.map(_perm_strong, range(N_PERM), chunksize=2))
    nrs = np.array([x["rs"] for x in nulls])
    z_rs = (rs_full - nrs.mean()) / nrs.std() if nrs.std() > 0 else 0.0
    beat = int((nrs >= rs_full).sum())
    print(f"     null Rs mean={nrs.mean():+,.0f} sd={nrs.std():,.0f}  z={z_rs:+5.2f}")
    print(f"     nulls beating the real ENTRY outright: {beat}/{N_PERM}")
    v["G3b_strong_null"] = z_rs > Z_GATE
    print(f"   -> {'PASS' if v['G3b_strong_null'] else 'FAIL'} (need z > {Z_GATE})")

    v["G4_friction"] = rs_full > 0
    print(f"G4 FRICTION   net Rs {rs_full:+,.0f} after delta + statutory + spread "
          f"-> {'PASS' if v['G4_friction'] else 'FAIL'}")

    p = np.sort(full["pts"].values)[::-1]
    ktrim = max(1, int(round(0.05 * len(p))))
    avg_trim = float(p[ktrim:].mean()) if len(p) > ktrim else 0.0
    v["G5_conc_5pct"] = avg_trim > be
    print(f"G5 CONC.(5%)  dropping top {ktrim} of {len(p)} leaves {avg_trim:+.2f} pts/trade "
          f"vs {be:.2f} -> {'PASS' if v['G5_conc_5pct'] else 'FAIL'}")

    print("\n" + "=" * 68)
    for k, ok in v.items():
        print(f"  {k:20s} {'PASS' if ok else 'FAIL'}")
    print(f"\nVERDICT: {'EDGE SURVIVES -- forward-test candidate' if all(v.values()) else 'NO EDGE -- do not deploy'}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="sweep")
    ap.add_argument("--tf", default="15,30")
    a = ap.parse_args()
    sys.exit(do_sweep([int(x) for x in a.tf.split(",")]) if a.cmd == "sweep" else do_validate())
