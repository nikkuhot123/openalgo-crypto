"""
Judas: the break-even ratchet guards SPOT. The money is in the PREMIUM.
=======================================================================
2026-08-07 produced the cleanest possible demonstration. Long 24600PE:

    entry 11:40   127.50
    peak  14:15   148.50   +16.5%   +Rs 1,365
    exit  15:10   109.70   -14.0%   -Rs 1,157      gave back Rs 2,522

The ratchet ARMED at 14:12:43 -- two minutes before the premium peak, which
is excellent timing -- and then never fired, because it moves a stop on SPOT
to the entry spot (24578.60) and spot never returned there. Spot closed at
24557.30, still 21 points IN FAVOUR of the long PE. Direction was right for
three and a half hours and the trade still lost, because nothing in the exit
ladder watches what the position is actually worth.

So: would a premium-based exit have beaten the current one, across trades and
not just on the one that hurt today?

Unit is % of entry premium, which maps straight to rupees (pct/100 * entry *
qty). Rules all keep the existing spot SL and EOD flat; they only add an
earlier exit.

Only contracts still in the broker master can be replayed -- Judas trades the
front weekly, so the sample is whatever has not expired.

Usage:
    ./venv/Scripts/python.exe backtesting/judas_premium_exit.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent
ORDERS = HERE / "judas_orders.csv"


def client():
    from openalgo import api

    env = (ROOT / ".env").read_text()
    return api(
        api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
        host=env.split("OPENALGO_HOST=")[1].split()[0],
    )


def pair_trades(df):
    trades, open_pos = [], {}
    for _, r in df.sort_values("ts").iterrows():
        sym = r["symbol"]
        if r["action"] == "BUY":
            open_pos.setdefault(sym, []).append(r)
        elif open_pos.get(sym):
            b = open_pos[sym].pop(0)
            trades.append(
                {
                    "symbol": sym,
                    "exchange": r["exchange"],
                    "qty": b["qty"],
                    "entry_ts": b["ts"],
                    "exit_ts": r["ts"],
                }
            )
    return pd.DataFrame(trades)


def simulate(hi, lo, cl, entry):
    """Return {rule: pct-of-entry outcome} for one 1-minute premium path."""
    out = {"current": (cl[-1] - entry) / entry * 100.0}

    # premium break-even: once +A% is shown, exit if it comes back to entry
    for a in (5.0, 8.0, 12.0):
        armed, res = False, None
        for h, lw in zip(hi, lo, strict=False):
            if not armed and (h - entry) / entry * 100.0 >= a:
                armed = True
            elif armed and lw <= entry:
                res = 0.0
                break
        out[f"be_{a:g}pct"] = res if res is not None else out["current"]

    # premium trail: once +A% shown, exit G% below the running peak
    for a, g in ((5.0, 5.0), (8.0, 5.0), (8.0, 8.0), (12.0, 8.0)):
        peak, armed, res = entry, False, None
        for h, lw in zip(hi, lo, strict=False):
            peak = max(peak, h)
            if not armed and (peak - entry) / entry * 100.0 >= a:
                armed = True
            if armed:
                stop = peak * (1 - g / 100.0)
                if lw <= stop:
                    res = (stop - entry) / entry * 100.0
                    break
        out[f"trail_{a:g}_{g:g}"] = res if res is not None else out["current"]

    # flat premium target
    for t in (10.0, 15.0, 20.0):
        res = None
        for h in hi:
            if (h - entry) / entry * 100.0 >= t:
                res = t
                break
        out[f"tgt_{t:g}pct"] = res if res is not None else out["current"]

    # time stop: exit N minutes after entry
    for n in (30, 60, 90, 120):
        idx = min(n, len(cl) - 1)
        out[f"time_{n}m"] = (cl[idx] - entry) / entry * 100.0
    return out


def main():
    orders = pd.read_csv(ORDERS, parse_dates=["ts"])
    trades = pair_trades(orders)
    print(f"{len(orders)} Judas orders -> {len(trades)} round trips")

    c = client()
    rows, dead = [], []
    for _, t in trades.iterrows():
        day = pd.Timestamp(t["entry_ts"]).strftime("%Y-%m-%d")
        try:
            d = c.history(
                symbol=t["symbol"], exchange=t["exchange"],
                interval="1m", start_date=day, end_date=day,
            )
        except Exception:
            dead.append(t["symbol"])
            continue
        if not isinstance(d, pd.DataFrame) or d.empty:
            dead.append(t["symbol"])
            continue
        d = d.copy()
        d.index = pd.to_datetime(d.index).tz_localize(None)
        p = d[
            (d.index >= pd.Timestamp(t["entry_ts"]).floor("min"))
            & (d.index <= pd.Timestamp(t["exit_ts"]).ceil("min"))
        ]
        if len(p) < 3:
            continue
        entry = float(p["close"].iloc[0])
        hi, lo, cl = p["high"].values, p["low"].values, p["close"].values
        peak_i = int(np.argmax(hi))
        rec = {
            "symbol": t["symbol"],
            "entry_ts": t["entry_ts"],
            "mins": len(p),
            "entry": round(entry, 2),
            "mfe_pct": round((hi.max() - entry) / entry * 100, 1),
            "peak_min": peak_i,
            "mae_pct": round((lo.min() - entry) / entry * 100, 1),
            "qty": int(t["qty"]),
        }
        rec.update({k: round(v, 2) for k, v in simulate(hi, lo, cl, entry).items()})
        rows.append(rec)

    if not rows:
        print(f"nothing replayable. expired: {sorted(set(dead))}")
        return 0
    d = pd.DataFrame(rows)
    d.to_csv(HERE / "judas_premium_paths.csv", index=False)
    print(f"replayed {len(d)} | {len(set(dead))} contracts expired out of the master\n")
    show = ["symbol", "entry_ts", "mins", "entry", "mfe_pct", "peak_min", "mae_pct", "current"]
    print(d[show].to_string(index=False))

    print(
        f"\nMFE mean {d['mfe_pct'].mean():+.1f}% median {d['mfe_pct'].median():+.1f}% | "
        f"peak at minute: median {d['peak_min'].median():.0f} of {d['mins'].median():.0f} held"
    )
    give = d["mfe_pct"] - d["current"]
    print(f"give-back from peak: mean {give.mean():.1f}pp median {give.median():.1f}pp")

    cols = [c for c in d.columns if c.startswith(("be_", "trail_", "tgt_", "time_"))] + ["current"]
    # rupees uses each trade's own qty
    rs = {c: (d[c] / 100.0 * d["entry"] * d["qty"]) for c in cols}
    tab = pd.DataFrame(
        {
            "mean_pct": d[cols].mean().round(2),
            "median_pct": d[cols].median().round(2),
            "win_rate": (d[cols] > 0).mean().round(2),
            "worst_pct": d[cols].min().round(2),
            "total_Rs": pd.Series({c: v.sum() for c, v in rs.items()}).round(0),
        }
    ).sort_values("total_Rs", ascending=False)
    print("\nexit rules on the same premium paths:")
    print(tab.to_string())

    best = tab.index[0]
    if best != "current" and len(d) > 2:
        diff = (rs[best] - rs["current"]).values
        rng = np.random.default_rng(0)
        boot = [rng.choice(diff, len(diff), replace=True).mean() for _ in range(10000)]
        lo_, hi_ = np.percentile(boot, [2.5, 97.5])
        print(f"\npaired: {best} minus current (n={len(diff)})")
        print(
            f"  mean Rs{diff.mean():+,.0f}/trade  95% CI [Rs{lo_:+,.0f}, Rs{hi_:+,.0f}]  "
            f"better {int((diff > 0).sum())} / worse {int((diff < 0).sum())}"
        )
        print("\n  n is small; treat as a direction to test, not a mandate to ship.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
