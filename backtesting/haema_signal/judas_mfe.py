"""
Judas Swing — does it give back its open profit? (real premium paths)
======================================================================
Complaint from the desk: the trade goes 7-10% in profit and then comes back
and stops out. This measures it instead of arguing about it.

Source of truth is the live order log (`db/openalgo.db.order_logs`, exported
to judas_orders.csv): every BUY/SELL the strategy actually sent since
2026-07-14. Orders are paired per symbol into round trips, then the option's
own 1-minute bars are replayed between entry and exit to get:

    MFE%  peak premium vs entry      (the open profit that existed)
    MAE%  worst premium vs entry
    end%  realised premium move
    give-back = MFE - end            (what the exit rule handed back)

Only contracts that have not expired can be replayed -- the broker drops
expired symbols from the master contract, so older trades are listed as
unfetchable rather than silently skipped.

Then it tests exit rules on those same paths, minute by minute:
    current   : the live rule (spot SL / spot target), i.e. observed outcome
    trail_X_Y : arm once premium >= +X%, then exit if it falls Y% from peak
    be_X      : move stop to break-even once premium >= +X%
    tgt_X     : take profit at +X%

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/judas_mfe.py
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
ORDERS = HERE / "judas_orders.csv"
OUT = HERE / "judas_mfe_paths.csv"


def client():
    from openalgo import api
    env = (ROOT / ".env").read_text()
    return api(api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
               host=env.split("OPENALGO_HOST=")[1].split()[0])


def pair_trades(df):
    """BUY -> next SELL on the same symbol = one round trip."""
    trades, open_pos = [], {}
    for _, r in df.sort_values("ts").iterrows():
        sym = r["symbol"]
        if r["action"] == "BUY":
            open_pos[sym] = r
        elif sym in open_pos:
            b = open_pos.pop(sym)
            trades.append({"symbol": sym, "exchange": r["exchange"], "qty": b["qty"],
                           "entry_ts": b["ts"], "exit_ts": r["ts"]})
    return pd.DataFrame(trades)


def replay(c, tr):
    """1-minute premium path between entry and exit, inclusive."""
    day = pd.Timestamp(tr["entry_ts"]).strftime("%Y-%m-%d")
    try:
        df = c.history(symbol=tr["symbol"], exchange=tr["exchange"],
                       interval="1m", start_date=day, end_date=day)
    except Exception:
        return None
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[(df.index >= pd.Timestamp(tr["entry_ts"]).floor("min")) &
              (df.index <= pd.Timestamp(tr["exit_ts"]).ceil("min"))]


def rules(path, entry):
    """Outcome (% of entry premium) for each exit rule on one path."""
    hi = path["high"].values
    lo = path["low"].values
    close = path["close"].values
    out = {}
    for arm, give in ((5, 3), (5, 5), (7, 3), (7, 5), (10, 5), (10, 7)):
        peak, armed, res = entry, False, None
        for i in range(len(close)):
            peak = max(peak, hi[i])
            if not armed and peak >= entry * (1 + arm / 100.0):
                armed = True
            if armed:
                stop = peak * (1 - give / 100.0)
                if lo[i] <= stop:
                    res = stop / entry - 1
                    break
        out[f"trail_{arm}_{give}"] = (res if res is not None
                                      else close[-1] / entry - 1) * 100
    for be in (5, 7):
        armed, res = False, None
        for i in range(len(close)):
            if not armed and hi[i] >= entry * (1 + be / 100.0):
                armed = True
            elif armed and lo[i] <= entry:
                res = 0.0
                break
        out[f"be_{be}"] = (res if res is not None else close[-1] / entry - 1) * 100
    for tgt in (5, 7, 10):
        hit = (hi >= entry * (1 + tgt / 100.0)).any()
        out[f"tgt_{tgt}"] = tgt if hit else (close[-1] / entry - 1) * 100
    return out


def main():
    if not ORDERS.exists():
        sys.exit(f"missing {ORDERS} (export it from the VPS order_logs first)")
    orders = pd.read_csv(ORDERS, parse_dates=["ts"])
    trades = pair_trades(orders)
    print(f"paired {len(trades)} round trips from {len(orders)} orders\n")

    c = client()
    rows, unfetchable = [], []
    for _, tr in trades.iterrows():
        path = replay(c, tr)
        if path is None or path.empty or len(path) < 2:
            unfetchable.append(tr["symbol"])
            continue
        entry = float(path["close"].iloc[0])
        if entry <= 0:
            continue
        rec = {
            "symbol": tr["symbol"], "entry_ts": tr["entry_ts"], "exit_ts": tr["exit_ts"],
            "mins": len(path), "qty": tr["qty"], "prem_entry": round(entry, 2),
            "prem_peak": round(float(path["high"].max()), 2),
            "prem_low": round(float(path["low"].min()), 2),
            "prem_exit": round(float(path["close"].iloc[-1]), 2),
        }
        rec["mfe_pct"] = round((rec["prem_peak"] / entry - 1) * 100, 2)
        rec["mae_pct"] = round((rec["prem_low"] / entry - 1) * 100, 2)
        rec["end_pct"] = round((rec["prem_exit"] / entry - 1) * 100, 2)
        rec["giveback_pct"] = round(rec["mfe_pct"] - rec["end_pct"], 2)
        peak_i = int(path["high"].values.argmax())
        rec["mins_to_peak"] = peak_i
        rec.update({k: round(v, 2) for k, v in rules(path, entry).items()})
        rows.append(rec)

    if not rows:
        print("no replayable trades -- every contract has expired out of the master")
        print("unfetchable:", sorted(set(unfetchable)))
        return
    d = pd.DataFrame(rows)
    d.to_csv(OUT, index=False)

    print(f"replayed {len(d)} trades; {len(set(unfetchable))} contracts expired "
          f"and cannot be replayed\n")
    cols = ["symbol", "entry_ts", "mins", "prem_entry", "prem_peak", "prem_exit",
            "mfe_pct", "end_pct", "giveback_pct", "mins_to_peak"]
    print(d[cols].to_string(index=False))

    print(f"\nMFE >= 7% at some point: {(d['mfe_pct'] >= 7).sum()}/{len(d)}")
    won = d[d["mfe_pct"] >= 7]
    if len(won):
        print(f"  of those, ended negative: {(won['end_pct'] < 0).sum()}/{len(won)}")
        print(f"  mean give-back: {won['giveback_pct'].mean():.1f}% of entry premium")
    print(f"mean MFE {d['mfe_pct'].mean():+.2f}% | mean end {d['end_pct'].mean():+.2f}% "
          f"| mean give-back {d['giveback_pct'].mean():+.2f}%")

    print("\nexit rules replayed on the SAME paths (mean % of entry premium per trade):")
    rule_cols = [c for c in d.columns if c.startswith(("trail_", "be_", "tgt_"))]
    tab = pd.DataFrame({
        "mean_pct": d[rule_cols].mean().round(2),
        "median_pct": d[rule_cols].median().round(2),
        "win_rate": (d[rule_cols] > 0).mean().round(3),
        "worst": d[rule_cols].min().round(2),
    }).sort_values("mean_pct", ascending=False)
    tab.loc["current (actual)"] = [d["end_pct"].mean().round(2),
                                   d["end_pct"].median().round(2),
                                   round((d["end_pct"] > 0).mean(), 3),
                                   d["end_pct"].min().round(2)]
    print(tab.to_string())
    print("\nnote: rules are evaluated on 1-minute OHLC, so an intrabar stop is "
          "assumed fillable at the trail level -- optimistic by up to one bar's range.")


if __name__ == "__main__":
    main()
