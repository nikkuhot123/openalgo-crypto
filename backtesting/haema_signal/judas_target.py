"""
Judas Swing — is the 2R target leaving the runners short?
==========================================================
judas_trail.py replayed each trade from entry to its ACTUAL exit, which is
fine for measuring give-back but useless for this question: a trade that hit
the 2R target has a path that STOPS at 2R, so nothing after it is visible.

Here every path is extended to the session's squareoff (15:10, the pre-CAS
freeze the strategy already uses), regardless of when the live trade actually
closed. That makes the counterfactual "what if we had held" measurable.

Rules, all carrying the original -1R stop and the deployed break-even ratchet
(stop -> entry once +1R is shown):

    tgt_2_be1     current behaviour + the ratchet   (2R hard target)
    tgt_3_be1     wider hard target
    tgt_4_be1     wider still
    be1_eod       no target at all -- ride to 15:10
    be1_trailG_afterT   no hard target; once T*R is shown, trail G*R below
                        the running peak (a loose leash on the runner)

Reported in R per lot. n is small (25 live trades, one month), so the点 is
the SHAPE of the response to target width, not the third decimal.

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/judas_target.py
"""
import sys
from datetime import time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
ORDERS = HERE / "judas_orders.csv"
GEOM = HERE / "judas_geometry.csv"
DB = ROOT / "backtesting/data/market_cache.duckdb"
CACHE_1M_END = pd.Timestamp("2026-07-28").date()
EOD = dtime(15, 10)
BE_ARM = 1.0


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
            open_pos[sym] = r
        elif sym in open_pos:
            b = open_pos.pop(sym)
            trades.append({"symbol": sym, "exchange": r["exchange"], "qty": b["qty"],
                           "entry_ts": b["ts"], "exit_ts": r["ts"]})
    d = pd.DataFrame(trades)
    d["under"] = np.where(d["symbol"].str.startswith("SENSEX"), "SENSEX", "NIFTY")
    d["side"] = np.where(d["symbol"].str.endswith("CE"), 1, -1)
    return d


def spot_1m(under, day, c):
    if day <= CACHE_1M_END:
        con = duckdb.connect(str(DB), read_only=True)
        df = con.execute(
            "select timestamp, high, low, close from market_data "
            f"where symbol='{under}' and interval='1m' order by timestamp").df()
        con.close()
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
        df = df[df["ts"].dt.date == day]
        return df.set_index("ts")[["high", "low", "close"]]
    ex = "BSE_INDEX" if under == "SENSEX" else "NSE_INDEX"
    r = c.history(symbol=under, exchange=ex, interval="1m",
                  start_date=day.isoformat(), end_date=day.isoformat())
    if not isinstance(r, pd.DataFrame) or r.empty:
        return pd.DataFrame()
    r = r.copy()
    r.index = pd.to_datetime(r.index).tz_localize(None)
    return r[["high", "low", "close"]]


def run_rule(fav_hi, fav_lo, fav_end, R, target=None, trail_after=None, trail_g=None):
    """One pass with the -1R stop, the +1R break-even ratchet, and either a
    hard target or a loose trail armed after `trail_after` R."""
    peak, be_armed = 0.0, False
    for hi, lo in zip(fav_hi, fav_lo):
        peak = max(peak, hi)
        floor = 0.0 if be_armed else -R          # ratchet lifts the stop to entry
        if trail_after is not None and peak >= trail_after * R:
            floor = max(floor, peak - trail_g * R)
        if lo <= floor:
            return floor / R
        if target is not None and hi >= target * R:
            return target
        if not be_armed and peak >= BE_ARM * R:
            be_armed = True
    return fav_end / R


def main():
    orders = pd.read_csv(ORDERS, parse_dates=["ts"])
    trades = pair_trades(orders)
    geom = (pd.read_csv(GEOM, parse_dates=["entry_ts"]) if GEOM.exists()
            else pd.DataFrame(columns=["entry_ts", "entry_spot", "stop_pts"]))
    c = client()

    specs = {
        "tgt_2_be1 (current+ratchet)": dict(target=2.0),
        "tgt_3_be1": dict(target=3.0),
        "tgt_4_be1": dict(target=4.0),
        "be1_eod (no target)": dict(),
        "be1_trail1.0_after2": dict(trail_after=2.0, trail_g=1.0),
        "be1_trail1.5_after2": dict(trail_after=2.0, trail_g=1.5),
        "be1_trail1.0_after3": dict(trail_after=3.0, trail_g=1.0),
        "be1_trail0.5_after2": dict(trail_after=2.0, trail_g=0.5),
    }
    rows = []
    for _, t in trades.iterrows():
        day = pd.Timestamp(t["entry_ts"]).date()
        try:
            sp = spot_1m(t["under"], day, c)
        except Exception:
            continue
        if sp.empty:
            continue
        path = sp[(sp.index >= pd.Timestamp(t["entry_ts"]).floor("min")) &
                  (sp.index.time <= EOD)]
        if len(path) < 2:
            continue
        entry = float(path["close"].iloc[0])
        side = t["side"]
        fav_hi = side * ((path["high"] if side > 0 else path["low"]).values - entry)
        fav_lo = side * ((path["low"] if side > 0 else path["high"]).values - entry)
        fav_end = side * (float(path["close"].iloc[-1]) - entry)

        g = geom[(geom["entry_ts"].dt.floor("min") == pd.Timestamp(t["entry_ts"]).floor("min"))
                 & ((geom["entry_spot"] - entry).abs() / entry < 0.01)]
        R = float(g["stop_pts"].iloc[0]) if len(g) else 0.113 / 100.0 * entry
        rec = {"symbol": t["symbol"], "entry_ts": t["entry_ts"], "mins_to_eod": len(path),
               "mfe_R": round(float(np.max(fav_hi)) / R, 2)}
        for name, kw in specs.items():
            rec[name] = round(run_rule(fav_hi, fav_lo, fav_end, R, **kw), 3)
        rows.append(rec)

    d = pd.DataFrame(rows)
    if d.empty:
        sys.exit("nothing replayed")
    d.to_csv(HERE / "judas_target_paths.csv", index=False)

    print(f"replayed {len(d)} trades, each held to 15:10 regardless of the live exit\n")
    print("MFE measured to EOD (not to the live exit):")
    print(f"  mean {d['mfe_R'].mean():.2f}R | median {d['mfe_R'].median():.2f}R | "
          f">=2R {(d['mfe_R'] >= 2).sum()}/{len(d)} | >=3R {(d['mfe_R'] >= 3).sum()}/{len(d)} | "
          f">=4R {(d['mfe_R'] >= 4).sum()}/{len(d)}")

    cols = list(specs)
    tab = pd.DataFrame({
        "mean_R": d[cols].mean().round(3),
        "median_R": d[cols].median().round(3),
        "win_rate": (d[cols] > 0).mean().round(2),
        "worst_R": d[cols].min().round(2),
        "best_R": d[cols].max().round(2),
    }).sort_values("mean_R", ascending=False)
    print("\ntarget / runner rules, all with the -1R stop and the +1R ratchet:")
    print(tab.to_string())

    base = "tgt_2_be1 (current+ratchet)"
    best = tab.index[0]
    if best != base:
        diff = (d[best] - d[base]).values
        rng = np.random.default_rng(0)
        boot = [rng.choice(diff, len(diff), replace=True).mean() for _ in range(10000)]
        lo, hi = np.percentile(boot, [2.5, 97.5])
        print(f"\npaired: {best} minus {base} (n={len(diff)})")
        print(f"  mean {diff.mean():+.3f}R  95% CI [{lo:+.3f}, {hi:+.3f}]  "
              f"better on {int((diff > 0).sum())}, worse on {int((diff < 0).sum())}")


if __name__ == "__main__":
    main()
