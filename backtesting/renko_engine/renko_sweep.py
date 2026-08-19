"""
Dr Devendra Smart Renko Engine PRO -- parameter sweep under a PRE-REGISTERED protocol
=====================================================================================
The earlier study (wiki/research/renko_pro_backtest.md) tested the Pine's SHIPPED
defaults across 5 indices x 5 timeframes and rejected it. The untested surface is
tuning: does any parameter set carry a real edge?

That question is dangerous. Sweeping until something looks good is how the last
four price-pattern studies in this repo manufactured headlines that died on
contact -- most recently the Stochastic run, where 16 of 162 configs were
profitable (roughly what pure noise produces), the winner showed PF 1.21 /
Sharpe 0.81, and it failed both cross-symbol transfer and IS/OOS.

So the protocol below is fixed BEFORE any result is looked at. It is written here,
in the script, so it cannot be quietly revised afterwards to fit what came out.

SELECTION (one winner, no peeking at OOS)
  - Sweep on NIFTY only, IN-SAMPLE window only = first 60% of sessions.
  - Rank by IS net points. Require n_IS >= 60 so a 4-trade fluke cannot win.
  - Exactly ONE config is carried forward. No "best of each timeframe".

VALIDATION -- the winner must pass ALL FIVE. Any single failure = no edge.
  G1  OOS: NIFTY last 40% of sessions, net points > 0.
  G2  TRANSFER: >= 2 of {SENSEX, BANKNIFTY, FINNIFTY, MIDCPNIFTY} net > 0 on
      full history with the IDENTICAL config. A parameter set that only works on
      the symbol it was fitted to is a curve-fit by definition.
  G3  NULL: beat 200 random-entry permutations at z > 2.0 on BOTH net points and
      trade Sharpe. Entries are randomised in TIMING ONLY, within the same
      session, same side, same EMA side, same per-day count -- so the null
      isolates exactly what the red-bar trigger claims to add. This reproduces
      the test the Pine's own header reports failing (z(sharpe) = +0.14,
      z(win) = +0.77, 2 of 4 null seeds beat it outright).
  G4  FRICTION: net POSITIVE in rupees after the measured option translation
      (DELTA 0.358, statutory 0.12% x2, spread 0.41%). Index points are not
      tradeable; this system buys options.
  G5  CONCENTRATION: net points still > 0 after deleting the best 5 trades. The
      default 30m config failed exactly here -- 5 of 435 trades carried 51% of
      all points.

Usage:
    ./venv/Scripts/python.exe backtesting/renko_engine/renko_sweep.py sweep
    ./venv/Scripts/python.exe backtesting/renko_engine/renko_sweep.py validate
"""
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

OUT = HERE / "sweep_results.csv"
WINNER = HERE / "sweep_winner.json"

IS_FRAC = 0.60
MIN_IS_TRADES = 60
N_PERM = 200
Z_GATE = 2.0
TRANSFER_SYMBOLS = ["SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

# ---- the grid ---------------------------------------------------------------
# Chosen as MECHANISM knobs, not a fishing expedition: brick size sets the Renko
# step and therefore the T2 distance and the room gate; tolerance sets what
# "sits on a level" means; room and T1 set the trade geometry; the two filter
# flags decide whether the engine's own confluence/trend gates help at all.
GRID = {
    "PCT_INDEX":      [0.33, 0.50, 0.66, 1.00],
    "LEVEL_TOL":      [4.0, 8.0, 16.0],
    "MIN_TARGET_R":   [1.0, 1.5, 2.0, 3.0],
    "T1_RR":          [1.0, 1.5, 2.5],
    "FILTER_EMA":     [True, False],
    "REQUIRE_CONFLUENCE": [True, False],
}
TFS = [5, 15, 30]

_BARS = {}


def bars(symbol, tf):
    key = (symbol, tf)
    if key not in _BARS:
        _BARS[key] = R.load_bars(symbol, tf)
    return _BARS[key]


def split_days(df):
    """IS/OOS boundary date at the IS_FRAC quantile of SESSIONS (not bars)."""
    d = np.unique(df["day"].values)
    return d[int(len(d) * IS_FRAC)]


def apply_cfg(cfg):
    for k, v in cfg.items():
        setattr(R, k, v)


def metrics(t):
    if t is None or len(t) == 0:
        return {"n": 0, "pts": 0.0, "pf": 0.0, "sharpe": 0.0, "win": 0.0}
    p = t["pts"].values
    gw, gl = p[p > 0].sum(), -p[p < 0].sum()
    return {
        "n": len(p),
        "pts": float(p.sum()),
        "pf": float(gw / gl) if gl > 0 else float("inf"),
        "sharpe": float(p.mean() / p.std() * np.sqrt(len(p))) if len(p) > 1 and p.std() > 0 else 0.0,
        "win": float(100.0 * (p > 0).mean()),
    }


def _one(job):
    """Worker: one config x one timeframe, IS window only."""
    tf, cfg = job
    apply_cfg(cfg)
    df = bars("NIFTY", tf)
    cut = split_days(df)
    t = R.run(df[df["day"].values < cut], "NIFTY")
    m = metrics(t)
    return {"tf": tf, **cfg, **{f"is_{k}": v for k, v in m.items()}}


def do_sweep():
    keys = list(GRID)
    combos = [dict(zip(keys, v)) for v in itertools.product(*(GRID[k] for k in keys))]
    jobs = [(tf, c) for tf in TFS for c in combos]
    print(f"configs {len(combos)} x tf {len(TFS)} = {len(jobs)} runs, IS = first {IS_FRAC:.0%} of sessions")
    rows = []
    with ProcessPoolExecutor(max_workers=14) as ex:
        for i, r in enumerate(ex.map(_one, jobs, chunksize=4), 1):
            rows.append(r)
            if i % 200 == 0:
                print(f"  {i}/{len(jobs)}")
    d = pd.DataFrame(rows)
    d.to_csv(OUT, index=False)

    ok = d[d["is_n"] >= MIN_IS_TRADES].copy()
    print(f"\n{len(ok)}/{len(d)} configs cleared n_IS >= {MIN_IS_TRADES}")
    print(f"profitable in-sample: {(ok['is_pts'] > 0).sum()}/{len(ok)} "
          f"({100 * (ok['is_pts'] > 0).mean():.1f}%)  <- compare to pure chance")
    top = ok.sort_values("is_pts", ascending=False).head(10)
    cols = ["tf", *keys, "is_n", "is_win", "is_pf", "is_pts", "is_sharpe"]
    print("\n--- top 10 IN-SAMPLE (selection is by is_pts, rank 1 only) ---")
    print(top[cols].to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    w = top.iloc[0]
    cfg = {k: (bool(w[k]) if isinstance(GRID[k][0], bool) else float(w[k])) for k in keys}
    WINNER.write_text(json.dumps({"tf": int(w["tf"]), "cfg": cfg,
                                  "is": {c: float(w[f"is_{c}"]) for c in ("n", "pts", "pf", "sharpe", "win")}}, indent=2))
    print(f"\nwinner -> {WINNER.name}: tf={int(w['tf'])}m {cfg}")
    return 0


# ---------------------------------------------------------------- validation

def null_entries(t, df, rng):
    """Random entry TIMING, holding side / session / EMA side / count fixed."""
    ema_s = df["close"].ewm(span=R.EMA_SLOW, adjust=False).mean().values
    c = df["close"].values
    days = df["day"].values
    idx_by_day = {}
    for i, dd in enumerate(days):
        idx_by_day.setdefault(dd, []).append(i)
    ov = {}
    for dd, grp in t.groupby("day"):
        cand_idx = idx_by_day.get(dd, [])
        if not cand_idx:
            continue
        last = cand_idx[-1]
        for side in grp["side"].tolist():
            pool = [i for i in cand_idx
                    if i != last and i not in ov
                    and (c[i] > ema_s[i] if side == "long" else c[i] < ema_s[i])]
            if pool:
                ov[int(rng.choice(pool))] = side
    return ov


def _perm(seed):
    cfg = json.loads(WINNER.read_text())
    apply_cfg(cfg["cfg"])
    df = bars("NIFTY", cfg["tf"])
    real = R.run(df, "NIFTY")
    ov = null_entries(real, df, np.random.default_rng(seed))
    return metrics(R.run(df, "NIFTY", entry_override=ov))


def rupees(t, symbol):
    """Measured option translation -- the hurdle index points never face."""
    if len(t) == 0:
        return 0.0
    lot = R.LOT.get(symbol.upper(), 65)
    prem = t["entry"].mean() * R.PREMIUM_PCT / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    return float((t["pts"] * R.DELTA * lot - cost).sum())


def do_validate():
    spec = json.loads(WINNER.read_text())
    tf, cfg = spec["tf"], spec["cfg"]
    print(f"WINNER  tf={tf}m  {cfg}")
    print(f"IS      {spec['is']}\n")
    apply_cfg(cfg)
    df = bars("NIFTY", tf)
    cut = split_days(df)
    full = R.run(df, "NIFTY")
    oos = full[full["day"].values >= cut]
    verdict = {}

    m_oos = metrics(oos)
    verdict["G1_oos"] = m_oos["pts"] > 0
    print(f"G1 OOS        n={m_oos['n']:4d} win={m_oos['win']:4.1f}% PF={m_oos['pf']:4.2f} "
          f"pts={m_oos['pts']:+8.1f} Sharpe={m_oos['sharpe']:+5.2f}  -> {'PASS' if verdict['G1_oos'] else 'FAIL'}")

    print("G2 TRANSFER")
    passes = 0
    for s in TRANSFER_SYMBOLS:
        try:
            ts_ = R.run(bars(s, tf), s)
            m = metrics(ts_)
            good = m["pts"] > 0 and m["n"] > 0
            passes += good
            print(f"     {s:11s} n={m['n']:4d} win={m['win']:4.1f}% PF={m['pf']:4.2f} "
                  f"pts={m['pts']:+8.1f}  {'+' if good else '-'}")
        except Exception as e:
            print(f"     {s:11s} unavailable ({type(e).__name__})")
    verdict["G2_transfer"] = passes >= 2
    print(f"   {passes}/{len(TRANSFER_SYMBOLS)} positive -> {'PASS' if verdict['G2_transfer'] else 'FAIL'}")

    m_full = metrics(full)
    print(f"G3 NULL       real pts={m_full['pts']:+8.1f} Sharpe={m_full['sharpe']:+5.2f} | {N_PERM} permutations")
    with ProcessPoolExecutor(max_workers=14) as ex:
        nulls = list(ex.map(_perm, range(N_PERM), chunksize=2))
    np_pts = np.array([x["pts"] for x in nulls])
    np_shp = np.array([x["sharpe"] for x in nulls])
    z_pts = (m_full["pts"] - np_pts.mean()) / np_pts.std() if np_pts.std() > 0 else 0.0
    z_shp = (m_full["sharpe"] - np_shp.mean()) / np_shp.std() if np_shp.std() > 0 else 0.0
    beat = int((np_pts >= m_full["pts"]).sum())
    print(f"     null pts  mean={np_pts.mean():+8.1f} sd={np_pts.std():7.1f}  z={z_pts:+5.2f}")
    print(f"     null shrp mean={np_shp.mean():+8.2f} sd={np_shp.std():7.2f}  z={z_shp:+5.2f}")
    print(f"     nulls beating the real trigger outright: {beat}/{N_PERM}")
    verdict["G3_null"] = z_pts > Z_GATE and z_shp > Z_GATE
    print(f"   -> {'PASS' if verdict['G3_null'] else 'FAIL'} (need both z > {Z_GATE})")

    rs = rupees(full, "NIFTY")
    verdict["G4_friction"] = rs > 0
    print(f"G4 FRICTION   net Rs {rs:+,.0f} on 1 lot after delta + statutory + spread "
          f"-> {'PASS' if verdict['G4_friction'] else 'FAIL'}")

    p = np.sort(full["pts"].values)[::-1]
    ex5 = float(p[5:].sum())
    verdict["G5_concentration"] = ex5 > 0
    print(f"G5 CONCENTR.  total {p.sum():+.1f} pts; top5 = {p[:5].sum():+.1f} "
          f"({100 * p[:5].sum() / p.sum():.0f}%); without them {ex5:+.1f} "
          f"-> {'PASS' if verdict['G5_concentration'] else 'FAIL'}")

    # ---- G6, added POST-HOC and labelled as such -------------------------
    # G5 as pre-registered used a fixed "top 5 trades", calibrated against the
    # earlier 435-trade study where 5 trades = 1.1% of the sample. On this
    # 996-trade run 5 trades is 0.5%, so the same threshold tests a quarter as
    # much and G5 passed while the book was in fact carried by 34 trades (3.4%)
    # holding 97% of net points. The scale-invariant form is top 5%, measured
    # against the friction hurdle rather than against zero -- surviving to
    # +0.17 pts/trade is not survival when a round trip costs 1.88.
    #
    # This did NOT decide the verdict: G3 had already failed. It is recorded so
    # the mis-specification is visible instead of buried.
    lot = R.LOT["NIFTY"]
    prem = full["entry"].mean() * R.PREMIUM_PCT / 100.0
    cost = (2 * prem * lot) * R.OPT_COST_PCT / 100.0 + prem * R.SPREAD_PCT / 100.0 * lot
    be = cost / (R.DELTA * lot)
    ktrim = max(1, int(round(0.05 * len(p))))
    trimmed = p[ktrim:]
    avg_trim = float(trimmed.mean()) if len(trimmed) else 0.0
    verdict["G6_concentration_5pct"] = avg_trim > be
    print(f"G6 CONC. (5%) breakeven={be:.2f} pts/trade; dropping top {ktrim} of {len(p)} "
          f"leaves {avg_trim:+.2f} pts/trade -> "
          f"{'PASS' if verdict['G6_concentration_5pct'] else 'FAIL'}   [post-hoc gate]")
    print("\n" + "=" * 66)
    for k, v in verdict.items():
        print(f"  {k:20s} {'PASS' if v else 'FAIL'}")
    allp = all(verdict.values())
    print(f"\nVERDICT: {'EDGE SURVIVES -- forward-test candidate' if allp else 'NO EDGE -- do not deploy'}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sweep"
    sys.exit(do_sweep() if cmd == "sweep" else do_validate())
