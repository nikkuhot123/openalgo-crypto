"""
The signal is not the problem. The vehicle is. Which strike survives the hold?
==============================================================================
Every strategy measured in this repo has a small real edge in INDEX POINTS and
dies converting it to rupees:

    Red Bar     PF 1.20 walk-forward -> 1.05 on the unfitted window
    Renko PRO   PF 1.25 in points at 30m -> breakeven after option friction
    Judas       +4.73R over 25 trades, yet 2026-08-07 closed a trade with spot
                21 points IN FAVOUR for -Rs 1,157

Two costs do the killing, and they are separable:
  1. FRICTION, paid per round trip   -> ~1.9 index points before anything earns
  2. THETA, paid per hour held       -> the give-back that ate Rs 2,522 today

An ATM weekly is the worst possible vehicle for #2: maximum theta, minimum
delta. This asks whether moving along the strike ladder fixes it, holding the
SIGNAL completely fixed -- identical entry time, identical exit time, only the
contract changes. No new strategy, no refitting, no hindsight.

Deeper ITM buys delta (more of the move captured) and sheds theta as a fraction
of premium, but costs more in absolute friction because both the statutory
charge and the spread scale with premium. Which effect wins is an empirical
question with an answer available right now: these contracts have not expired.

Usage:
    ./venv/Scripts/python.exe backtesting/strike_selection.py
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent

LOT = 65
STEP = 50                 # NIFTY strike interval
OPT_COST_PCT = 0.12       # statutory, each way
SPREAD_PCT = 0.41         # measured; live book has shown 0.24-0.29


def client():
    from openalgo import api

    env = (ROOT / ".env").read_text()
    return api(
        api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
        host=env.split("OPENALGO_HOST=")[1].split()[0],
    )


def path_for(c, symbol, day, t0, t1):
    try:
        d = c.history(symbol=symbol, exchange="NFO", interval="1m",
                      start_date=day, end_date=day)
    except Exception:
        return None
    if not isinstance(d, pd.DataFrame) or d.empty:
        return None
    d = d.copy()
    d.index = pd.to_datetime(d.index).tz_localize(None)
    seg = d[(d.index >= pd.Timestamp(t0).floor("min")) & (d.index <= pd.Timestamp(t1).ceil("min"))]
    return seg if len(seg) >= 3 else None


def net_rs(entry, exit_, lot=LOT):
    """One lot, bought and sold, net of statutory charges and one spread."""
    gross = (exit_ - entry) * lot
    cost = (entry + exit_) * lot * OPT_COST_PCT / 100.0 + entry * lot * SPREAD_PCT / 100.0
    return gross - cost


def main():
    orders = pd.read_csv(HERE / "judas_orders.csv", parse_dates=["ts"])
    trips, open_pos = [], {}
    for _, r in orders.sort_values("ts").iterrows():
        if r["action"] == "BUY":
            open_pos.setdefault(r["symbol"], []).append(r)
        elif open_pos.get(r["symbol"]):
            b = open_pos[r["symbol"]].pop(0)
            trips.append({"symbol": r["symbol"], "entry_ts": b["ts"], "exit_ts": r["ts"]})
    trips = pd.DataFrame(trips)
    # only the contracts that have not expired out of the master
    trips = trips[trips["symbol"].str.contains("11AUG26")]

    c = client()
    rows = []
    for _, t in trips.iterrows():
        sym = t["symbol"]
        right = sym[-2:]
        base = int(sym.replace("NIFTY11AUG26", "")[:-2])
        day = pd.Timestamp(t["entry_ts"]).strftime("%Y-%m-%d")
        held = (pd.Timestamp(t["exit_ts"]) - pd.Timestamp(t["entry_ts"])).total_seconds() / 60

        for off in (-3, -2, -1, 0, 1, 2, 3):
            strike = base + off * STEP
            p = path_for(c, f"NIFTY11AUG26{strike}{right}", day, t["entry_ts"], t["exit_ts"])
            if p is None:
                continue
            e, x = float(p["close"].iloc[0]), float(p["close"].iloc[-1])
            peak = float(p["high"].max())
            # for a PE, a HIGHER strike is deeper in the money; for a CE, lower
            itm_steps = off if right == "PE" else -off
            rows.append({
                "trade": f"{day} {right}",
                "held_min": round(held),
                "strike": strike,
                "itm_steps": itm_steps,
                "entry": round(e, 2),
                "exit": round(x, 2),
                "mfe_pct": round((peak - e) / e * 100, 1),
                "end_pct": round((x - e) / e * 100, 1),
                "net_rs": round(net_rs(e, x)),
            })

    if not rows:
        print("nothing replayable")
        return 1
    d = pd.DataFrame(rows)
    d.to_csv(HERE / "strike_selection.csv", index=False)

    for trade, g in d.groupby("trade"):
        held = g["held_min"].iloc[0]
        print(f"\n=== {trade} | held {held:.0f} min | traded strike marked *")
        print(f"{'strike':>7} {'moneyness':>10} {'entry':>8} {'exit':>8} "
              f"{'MFE%':>7} {'end%':>7} {'net Rs':>9}")
        for _, r in g.sort_values("itm_steps").iterrows():
            mny = ("ATM" if r["itm_steps"] == 0 else
                   f"ITM{r['itm_steps']}" if r["itm_steps"] > 0 else f"OTM{-r['itm_steps']}")
            star = " *" if r["itm_steps"] == 0 else "  "
            print(f"{r['strike']:>7}{star}{mny:>8} {r['entry']:>8.2f} {r['exit']:>8.2f} "
                  f"{r['mfe_pct']:>+7.1f} {r['end_pct']:>+7.1f} {r['net_rs']:>+9,.0f}")

    print("\n\nTOTAL ACROSS ALL REPLAYABLE TRADES, by moneyness")
    print(f"{'moneyness':>10} {'trades':>7} {'total Rs':>10} {'avg Rs':>9} "
          f"{'avg MFE%':>9} {'avg end%':>9} {'wins':>6}")
    agg = d.groupby("itm_steps").agg(
        n=("net_rs", "size"), total=("net_rs", "sum"), avg=("net_rs", "mean"),
        mfe=("mfe_pct", "mean"), end=("end_pct", "mean"),
        wins=("net_rs", lambda s: (s > 0).sum()))
    for steps, r in agg.sort_index().iterrows():
        mny = ("ATM" if steps == 0 else f"ITM{steps}" if steps > 0 else f"OTM{-steps}")
        print(f"{mny:>10} {int(r['n']):>7} {r['total']:>+10,.0f} {r['avg']:>+9,.0f} "
              f"{r['mfe']:>+9.1f} {r['end']:>+9.1f} {int(r['wins'])}/{int(r['n']):>2}")

    best = agg["total"].idxmax()
    bname = "ATM" if best == 0 else f"ITM{best}" if best > 0 else f"OTM{-best}"
    atm = agg.loc[0, "total"] if 0 in agg.index else 0
    print(f"\nbest vehicle: {bname}  ({agg.loc[best, 'total']:+,.0f} vs ATM {atm:+,.0f}, "
          f"delta {agg.loc[best, 'total'] - atm:+,.0f})")
    print("n is 4 signals x 7 strikes -- the SIGNAL sample is still 4. This ranks\n"
          "vehicles for a fixed signal; it does not establish that the signal works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
