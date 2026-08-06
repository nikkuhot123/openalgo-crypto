"""
Judas Swing — how much open profit exists, and what protects it (spot paths)
=============================================================================
judas_mfe.py replayed the four trades whose option contracts are still in the
master and found all four went green (MFE +6.8% to +16.6% of premium) and all
four closed red, handing back ~20% of entry premium on average. Four paths is
too thin to choose an exit rule on.

The strategy exits on SPOT levels (stop and target are spot prices, and the
monitor compares spot LTP against them), so a spot-path replay is both a
bigger sample and directly actionable. Every Judas round trip since
2026-07-14 is replayed on 1-minute index bars: duckdb for sessions up to
2026-07-28, single-day API calls after that (multi-day requests come back
corrupted -- see redbar_overnight.fetch_5m_live).

Everything is measured in R, where R = |entry_spot - stop_spot| taken from
the strategy's own "Entry geometry" log line. R is the natural unit here
because the live code already knows it at entry, so any rule expressed in R
is implementable without new state.

Rules replayed on the same paths:
    current   observed exit
    be_A      once favourable excursion >= A*R, stop moves to entry
    trail_A_G once fav >= A*R, stop trails G*R below the running peak
    tgt_A     take profit at A*R

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/judas_trail.py
"""
import re
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
ORDERS = HERE / "judas_orders.csv"
GEOM = HERE / "judas_geometry.csv"      # entry_ts,stop_pts,target_pts from logs
DB = ROOT / "backtesting/data/market_cache.duckdb"
CACHE_END = pd.Timestamp("2026-07-28").date()


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
    """1-minute index bars: duckdb where cached, single-day API after."""
    if day <= CACHE_END:
        con = duckdb.connect(str(DB), read_only=True)
        df = con.execute(
            "select timestamp, open, high, low, close from market_data "
            f"where symbol='{under}' and interval='1m' order by timestamp").df()
        con.close()
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
        df = df[df["ts"].dt.date == day]
        return df.set_index("ts")[["open", "high", "low", "close"]]
    ex = "BSE_INDEX" if under == "SENSEX" else "NSE_INDEX"
    r = c.history(symbol=under, exchange=ex, interval="1m",
                  start_date=day.isoformat(), end_date=day.isoformat())
    if not isinstance(r, pd.DataFrame) or r.empty:
        return pd.DataFrame()
    r = r.copy()
    r.index = pd.to_datetime(r.index).tz_localize(None)
    return r[["open", "high", "low", "close"]]


def rules(fav_hi, fav_lo, fav_end, R):
    """Outcome in R for each rule, given the favourable-excursion path."""
    out = {}
    peak = 0.0
    for A, G in ((0.5, 0.25), (0.5, 0.5), (0.75, 0.25), (0.75, 0.5), (1.0, 0.5)):
        peak, armed, res = 0.0, False, None
        for hi, lo in zip(fav_hi, fav_lo):
            peak = max(peak, hi)
            if not armed and peak >= A * R:
                armed = True
            if armed and lo <= peak - G * R:
                res = (peak - G * R) / R
                break
            if lo <= -R:                      # original stop still applies
                res = -1.0
                break
        out[f"trail_{A}_{G}"] = res if res is not None else fav_end / R
    for A in (0.5, 0.75, 1.0, 1.25, 1.5):
        armed, res = False, None
        for hi, lo in zip(fav_hi, fav_lo):
            if not armed and hi >= A * R:
                armed = True
            elif armed and lo <= 0:
                res = 0.0
                break
            if lo <= -R:
                res = -1.0
                break
        out[f"be_{A}"] = res if res is not None else fav_end / R
        res2 = None
        for hi, lo in zip(fav_hi, fav_lo):
            if hi >= A * R:
                res2 = A
                break
            if lo <= -R:
                res2 = -1.0
                break
        out[f"tgt_{A}"] = res2 if res2 is not None else fav_end / R

    # Two-lot / half-book: book HALF at A*R, run the rest on a break-even stop
    # (the desk's other suggestion). Needs 2x the premium capital per trade.
    for A in (0.75, 1.0, 1.5):
        booked, armed, rest = None, False, None
        for hi, lo in zip(fav_hi, fav_lo):
            if booked is None and hi >= A * R:
                booked, armed = A, True
                continue
            if booked is None and lo <= -R:
                booked, rest = -1.0, -1.0
                break
            if armed and lo <= 0:
                rest = 0.0
                break
        if booked is None:
            out[f"half_{A}"] = fav_end / R
        elif booked == -1.0:
            out[f"half_{A}"] = -1.0
        else:
            out[f"half_{A}"] = 0.5 * booked + 0.5 * (rest if rest is not None else fav_end / R)
    return out


def main():
    orders = pd.read_csv(ORDERS, parse_dates=["ts"])
    trades = pair_trades(orders)
    geom = (pd.read_csv(GEOM, parse_dates=["entry_ts"]) if GEOM.exists()
            else pd.DataFrame(columns=["entry_ts", "entry_spot", "stop_pts"]))
    print(f"{len(trades)} round trips | geometry available for {len(geom)}")

    c = client()
    rows = []
    for _, t in trades.iterrows():
        day = pd.Timestamp(t["entry_ts"]).date()
        try:
            sp = spot_1m(t["under"], day, c)
        except Exception as e:
            print(f"  {t['symbol']} {day}: spot fetch failed ({str(e)[:40]})")
            continue
        if sp.empty:
            continue
        path = sp[(sp.index >= pd.Timestamp(t["entry_ts"]).floor("min")) &
                  (sp.index <= pd.Timestamp(t["exit_ts"]).ceil("min"))]
        if len(path) < 2:
            continue
        entry = float(path["close"].iloc[0])
        side = t["side"]
        fav_hi = side * ((path["high"] if side > 0 else path["low"]).values - entry)
        fav_lo = side * ((path["low"] if side > 0 else path["high"]).values - entry)
        fav_end = side * (float(path["close"].iloc[-1]) - entry)

        # Match the log's geometry on BOTH minute and spot level: the two books
        # enter within seconds of each other (10:02:20 SENSEX / 10:02:26 NIFTY),
        # so a minute-only join hands SENSEX the NIFTY stop and vice versa.
        g = geom[(geom["entry_ts"].dt.floor("min") == pd.Timestamp(t["entry_ts"]).floor("min"))
                 & ((geom["entry_spot"] - entry).abs() / entry < 0.01)]
        # Fall back to the measured house value: the stop is 0.113% of spot
        # +/- 0.0145 across every logged trade (range 0.100-0.140%).
        R = float(g["stop_pts"].iloc[0]) if len(g) else 0.113 / 100.0 * entry
        rec = {"symbol": t["symbol"], "under": t["under"], "entry_ts": t["entry_ts"],
               "R_src": "log" if len(g) else "est",
               "mins": len(path), "entry_spot": round(entry, 2),
               "mfe_pts": round(float(np.max(fav_hi)), 1),
               "mae_pts": round(float(np.min(fav_lo)), 1),
               "end_pts": round(float(fav_end), 1), "R_pts": round(R, 1)}
        rec["mfe_pct"] = round(rec["mfe_pts"] / entry * 100, 3)
        if np.isfinite(R) and R > 0:
            rec["mfe_R"] = round(rec["mfe_pts"] / R, 2)
            rec["end_R"] = round(rec["end_pts"] / R, 2)
            rec.update({k: round(v, 3) for k, v in rules(fav_hi, fav_lo, fav_end, R).items()})
        rows.append(rec)

    d = pd.DataFrame(rows)
    d.to_csv(HERE / "judas_trail_paths.csv", index=False)
    print(f"replayed {len(d)} trades\n")
    cols = ["symbol", "entry_ts", "mins", "mfe_pts", "end_pts", "mfe_pct", "R_pts", "mfe_R", "end_R"]
    print(d[[c for c in cols if c in d.columns]].to_string(index=False))

    withR = d.dropna(subset=["mfe_R"]) if "mfe_R" in d.columns else pd.DataFrame()
    print(f"\nMFE in R (n={len(withR)}): mean {withR['mfe_R'].mean():.2f}R | "
          f"median {withR['mfe_R'].median():.2f}R | "
          f">=0.5R: {(withR['mfe_R'] >= 0.5).sum()}/{len(withR)} | "
          f">=1.0R: {(withR['mfe_R'] >= 1.0).sum()}/{len(withR)} | "
          f">=2.0R (target): {(withR['mfe_R'] >= 2.0).sum()}/{len(withR)}")
    print(f"outcome: mean {withR['end_R'].mean():+.2f}R | "
          f"wins {(withR['end_R'] > 0).sum()}/{len(withR)}")

    rule_cols = [c for c in d.columns if c.startswith(("trail_", "be_", "tgt_", "half_"))]
    if rule_cols and len(withR):
        tab = pd.DataFrame({
            "mean_R": withR[rule_cols].mean().round(3),
            "median_R": withR[rule_cols].median().round(3),
            "win_rate": (withR[rule_cols] > 0).mean().round(2),
            "worst_R": withR[rule_cols].min().round(2),
        }).sort_values("mean_R", ascending=False)
        tab.loc["current (actual)"] = [withR["end_R"].mean().round(3),
                                       withR["end_R"].median().round(3),
                                       round((withR["end_R"] > 0).mean(), 2),
                                       withR["end_R"].min().round(2)]
        print("\nexit rules on the same spot paths (R per trade):")
        print(tab.to_string())

        # n=25 is small, so the winner is checked pairwise against the status quo
        best = tab.drop("current (actual)")["mean_R"].idxmax()
        diff = (withR[best] - withR["end_R"]).values
        rng = np.random.default_rng(0)
        boot = [rng.choice(diff, len(diff), replace=True).mean() for _ in range(10000)]
        lo, hi = np.percentile(boot, [2.5, 97.5])
        print(f"\npaired check, {best} minus current (n={len(diff)}):")
        print(f"  mean improvement {diff.mean():+.3f}R  95% CI [{lo:+.3f}, {hi:+.3f}]")
        print(f"  better on {int((diff > 0).sum())} trades, worse on {int((diff < 0).sum())}, "
              f"unchanged on {int((diff == 0).sum())}")
        print(f"  in rupees at 1 lot NIFTY (R = {withR[withR.under=='NIFTY']['R_pts'].mean():.0f} "
              f"pts x 0.5 delta x 65): ~Rs {diff.mean() * 28 * 0.5 * 65:+,.0f} per trade")


if __name__ == "__main__":
    main()
