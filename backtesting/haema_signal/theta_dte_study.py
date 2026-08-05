"""
Does SENSEX near-expiry theta justify extending the expiry-day skip?
====================================================================
judas_swing_strategy skips entries on expiry day for NIFTY only. The code
note says SENSEX was deliberately excluded because "its Thu expiry is its
best day in live logs".

One measurement on 2026-08-05 contradicted that: SENSEX 1-DTE theta came in at
-Rs 3.80/min/lot against NIFTY's -Rs 0.08/min/lot at 4 DTE. One measurement is
not evidence - that is the mistake made twice already today. So measure the
whole DTE curve for both indices.

Method: for each option contract and each session, regress the option's 1-minute
price change on the index's 1-minute change:

    d(option) = delta * d(spot) + theta_per_minute

The intercept isolates time decay from directional movement. Run it per session
so theta can be plotted against days-to-expiry.
"""
import numpy as np
import pandas as pd
from openalgo import api

API_KEY = "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a"
HOST = "https://openalgo.inikhilesh.com"

client = api(api_key=API_KEY, host=HOST)

START, END = "2026-07-27", "2026-08-06"
SESSION_LO, SESSION_HI = "09:20", "15:10"   # inside CAS-safe hours


def spot_series(sym, exch):
    df = client.history(symbol=sym, exchange=exch, interval="1m",
                        start_date=START, end_date=END)
    return df["close"] if isinstance(df, pd.DataFrame) and not df.empty else None


def theta_by_session(opt_sym, opt_exch, spot, lot, expiry):
    """Per-session (delta, theta/min) for one contract."""
    df = client.history(symbol=opt_sym, exchange=opt_exch, interval="1m",
                        start_date=START, end_date=END)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    j = pd.DataFrame({"opt": df["close"], "spot": spot}).dropna()
    hm = j.index.strftime("%H:%M")
    j = j[(hm >= SESSION_LO) & (hm <= SESSION_HI)]

    out = []
    for day, g in j.groupby(j.index.date):
        d = g.diff().dropna()
        if len(d) < 60:
            continue
        A = np.vstack([d["spot"].values, np.ones(len(d))]).T
        delta, theta = np.linalg.lstsq(A, d["opt"].values, rcond=None)[0]
        dte = (expiry - day).days
        if dte < 0:
            continue
        out.append({
            "date": day, "dte": dte, "delta": delta,
            "theta_min": theta, "theta_rs_min": theta * lot,
            "premium": g["opt"].iloc[0],
            "theta_pct_hr": theta * 60 / g["opt"].iloc[0] * 100 if g["opt"].iloc[0] else np.nan,
        })
    return out


def main():
    print("Fetching index series...")
    nifty = spot_series("NIFTY", "NSE_INDEX")
    sensex = spot_series("SENSEX", "BSE_INDEX")

    contracts = [
        ("NIFTY11AUG2624600CE", "NFO", nifty, 65, pd.Timestamp("2026-08-11").date(), "NIFTY"),
        ("NIFTY11AUG2624600PE", "NFO", nifty, 65, pd.Timestamp("2026-08-11").date(), "NIFTY"),
        ("SENSEX06AUG2678600CE", "BFO", sensex, 20, pd.Timestamp("2026-08-06").date(), "SENSEX"),
        ("SENSEX06AUG2678600PE", "BFO", sensex, 20, pd.Timestamp("2026-08-06").date(), "SENSEX"),
        ("SENSEX13AUG2678600CE", "BFO", sensex, 20, pd.Timestamp("2026-08-13").date(), "SENSEX"),
    ]

    rows = []
    for sym, exch, spot, lot, exp, idx in contracts:
        if spot is None:
            continue
        for r in theta_by_session(sym, exch, spot, lot, exp):
            r.update(symbol=sym, index=idx, lot=lot)
            rows.append(r)

    if not rows:
        print("No data.")
        return
    df = pd.DataFrame(rows)

    print()
    print("=" * 104)
    print(" THETA vs DAYS-TO-EXPIRY  (intercept of d_option ~ delta*d_spot, per session)")
    print("=" * 104)
    print(f"{'index':7s} {'symbol':22s} {'date':11s} {'DTE':>4s} {'delta':>7s} "
          f"{'theta Rs/min':>13s} {'%prem/hr':>9s} {'2h hold':>9s}")
    print("-" * 104)
    for _, r in df.sort_values(["index", "dte", "date"]).iterrows():
        print(f"{r['index']:7s} {r['symbol']:22s} {str(r['date']):11s} {r['dte']:4d} "
              f"{r['delta']:7.3f} {r['theta_rs_min']:+13.2f} {r['theta_pct_hr']:+8.2f}% "
              f"{r['theta_rs_min'] * 120:+9,.0f}")

    print()
    print("=" * 104)
    print(" AGGREGATED BY INDEX AND DTE BUCKET")
    print("=" * 104)
    df["bucket"] = pd.cut(df["dte"], [-1, 0, 1, 3, 7, 99],
                          labels=["0 (expiry)", "1", "2-3", "4-7", "8+"])
    g = df.groupby(["index", "bucket"], observed=True).agg(
        n=("theta_rs_min", "size"),
        theta=("theta_rs_min", "mean"),
        delta=("delta", "mean"),
        pct_hr=("theta_pct_hr", "mean"),
    ).reset_index()
    print(f"{'index':8s} {'DTE':12s} {'n':>3s} {'theta Rs/min/lot':>18s} "
          f"{'delta':>7s} {'%prem/hr':>9s} {'cost of 2h hold':>17s}")
    print("-" * 104)
    for _, r in g.iterrows():
        print(f"{r['index']:8s} {str(r['bucket']):12s} {r['n']:3d} {r['theta']:+18.2f} "
              f"{r['delta']:7.3f} {r['pct_hr']:+8.2f}% {r['theta'] * 120:+17,.0f}")

    print()
    print("=" * 104)
    print(" VERDICT")
    print("=" * 104)
    for idx in ("NIFTY", "SENSEX"):
        sub = df[df["index"] == idx]
        near = sub[sub["dte"] <= 1]["theta_rs_min"]
        far = sub[sub["dte"] >= 3]["theta_rs_min"]
        if len(near) and len(far):
            print(f"  {idx:7s} near-expiry (DTE<=1) mean Rs {near.mean():+7.2f}/min/lot over {len(near)} sessions")
            print(f"  {' ':7s} far      (DTE>=3) mean Rs {far.mean():+7.2f}/min/lot over {len(far)} sessions")
            if far.mean() != 0:
                print(f"  {' ':7s} near-expiry decay is {abs(near.mean() / far.mean()):.1f}x the far-dated rate")
        elif len(near):
            print(f"  {idx:7s} near-expiry only: Rs {near.mean():+7.2f}/min/lot ({len(near)} sessions) — no far-dated comparison")
        else:
            print(f"  {idx:7s} insufficient coverage")
        print()


if __name__ == "__main__":
    main()
