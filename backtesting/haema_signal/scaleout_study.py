"""
Should we run 2 lots — bank one at T1, trail the runner?
========================================================
Scaling out is not free. On a positive-expectancy strategy it MECHANICALLY
lowers expectancy, because the winners you cut short are the ones paying for
the losers. It buys variance reduction with mean.

It only wins if, CONDITIONAL on reaching T1, the move keeps going often enough
that the runner's tail pays for the lot you no longer hold to T1... and, more
brutally, only if you can afford 2 lots at all.

So measure, on real NIFTY/SENSEX 5m data:
  1. MFE distribution in R, conditional on the trade reaching +1R
  2. Head-to-head: 2 lots to T1  vs  1 banked at T1 + 1 trailed
  3. The capital wall

Signal = the deployed HA-EMA rule: previous-day Heikin-Ashi bias picks the
side, entry when a 5m close breaks the 34-EMA channel, SL at the signal
candle's opposite extreme, R = |entry - SL|.
Underlying points throughout; option premium adds theta and spread that make
every conclusion here strictly worse, never better.
"""
import duckdb
import numpy as np
import pandas as pd

DB = "backtesting/data/market_cache.duckdb"
EMA_PERIOD = 34
ENTRY_START, ENTRY_END = "09:45", "14:30"
EOD = "15:10"          # post-CAS exit
MAX_R = 8.0


def ema(vals, period):
    a = np.asarray(vals, dtype=float)
    out = np.empty_like(a)
    k = 2.0 / (period + 1)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def load(symbol):
    con = duckdb.connect(DB, read_only=True)
    df = con.execute(
        "SELECT to_timestamp(timestamp)::TIMESTAMP ts, open, high, low, close "
        "FROM market_data WHERE symbol=? AND interval='5m' ORDER BY timestamp",
        [symbol],
    ).fetchdf()
    con.close()
    df["date"] = df["ts"].dt.date
    df["hm"] = df["ts"].dt.strftime("%H:%M")
    return df


def ha_bias(df):
    d = df.groupby("date").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    )
    n = len(d)
    ho, hc = np.empty(n), np.empty(n)
    ho[0] = (d["open"].iloc[0] + d["close"].iloc[0]) / 2
    hc[0] = d.iloc[0].mean()
    for i in range(1, n):
        hc[i] = d.iloc[i][["open", "high", "low", "close"]].mean()
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    return pd.Series(np.where(hc > ho, "GREEN", "RED"), index=d.index).shift(1).to_dict()


def collect_trades(df, bias_map):
    """One trade per day. Record the full forward path in R multiples."""
    df = df.reset_index(drop=True)
    eh = ema(df["high"].values, EMA_PERIOD)
    el = ema(df["low"].values, EMA_PERIOD)
    hi, lo, cl = df["high"].values, df["low"].values, df["close"].values
    hm, dates = df["hm"].values, df["date"].values

    trades, used = [], set()
    for i in range(EMA_PERIOD + 1, len(df) - 1):
        d = dates[i]
        if d in used or not (ENTRY_START <= hm[i] <= ENTRY_END):
            continue
        b = bias_map.get(d)
        if b is None or (isinstance(b, float) and np.isnan(b)):
            continue

        if b == "GREEN" and cl[i] > eh[i]:
            side, entry, sl = 1, cl[i], lo[i]
        elif b == "RED" and cl[i] < el[i]:
            side, entry, sl = -1, cl[i], hi[i]
        else:
            continue

        r = abs(entry - sl)
        if r <= 0:
            continue
        used.add(d)

        # forward path within the same session, to EOD
        path = []
        for j in range(i + 1, len(df)):
            if dates[j] != d or hm[j] > EOD:
                break
            fav = (hi[j] - entry) / r if side == 1 else (entry - lo[j]) / r
            adv = (entry - lo[j]) / r if side == 1 else (hi[j] - entry) / r
            path.append((fav, adv, (cl[j] - entry) / r * side))
        if path:
            trades.append({"date": d, "side": side, "entry": entry, "R": r, "path": path})
    return trades


def simulate(tr, t1_r, trail_r):
    """Return (pnl_2lots_to_T1, pnl_scaleout) in R per lot-pair."""
    hit_t1 = stopped = False
    peak = 0.0
    trail_stop = -1.0          # initial stop = -1R
    runner_exit = None
    last = tr["path"][-1][2]

    for fav, adv, close_r in tr["path"]:
        # stop first — conservative when both touch in one bar
        if not hit_t1 and adv >= 1.0:
            stopped = True
            break
        if not hit_t1 and fav >= t1_r:
            hit_t1 = True
            peak = fav
            trail_stop = 0.0    # runner to breakeven once T1 banked
            continue
        if hit_t1:
            peak = max(peak, fav)
            trail_stop = max(trail_stop, peak - trail_r)
            if -adv <= trail_stop <= fav and adv >= -trail_stop:
                runner_exit = trail_stop
                break

    if stopped:
        return -2.0, -2.0                       # both lots stopped
    if not hit_t1:
        return 2 * last, 2 * last               # neither reached T1, EOD out
    if runner_exit is None:
        runner_exit = last                      # runner held to EOD
    return 2 * t1_r, t1_r + runner_exit


def main():
    print("=" * 100)
    print(" SCALE-OUT STUDY — 2 lots to T1  vs  bank 1 at T1 + trail the runner")
    print("=" * 100)

    allt = {}
    for sym in ("NIFTY", "SENSEX"):
        df = load(sym)
        tr = collect_trades(df, ha_bias(df))
        allt[sym] = tr
        sessions = df["date"].nunique()
        print(f"\n{sym}: {len(tr)} trades over {sessions} sessions "
              f"({df['ts'].min():%Y-%m-%d} to {df['ts'].max():%Y-%m-%d})")

        mfe = np.array([max(p[0] for p in t["path"]) for t in tr])
        mfe = np.clip(mfe, 0, MAX_R)
        reached = (mfe >= 1.0).sum()
        print(f"  reached +1R: {reached}/{len(tr)} ({reached / len(tr) * 100:.1f}%)")
        if reached:
            sub = mfe[mfe >= 1.0]
            print("  GIVEN it reached +1R, how much further did it actually go?")
            for lvl in (1.5, 2.0, 2.5, 3.0, 4.0):
                n = (sub >= lvl).sum()
                print(f"    -> also reached +{lvl:.1f}R : {n:3d}/{reached} ({n / reached * 100:5.1f}%)")
            print(f"    median MFE | reached 1R : {np.median(sub):.2f}R")

    print("\n" + "=" * 100)
    print(" HEAD TO HEAD (R per 2-lot pair, before costs)")
    print("=" * 100)
    print(f"{'symbol':8s} {'T1':>5s} {'trail':>6s} {'2-lot@T1':>11s} {'scale-out':>11s} {'delta':>9s}  verdict")
    print("-" * 100)
    for sym, tr in allt.items():
        if not tr:
            continue
        for t1 in (1.0, 1.5, 2.0):
            for trail in (0.5, 1.0):
                a = np.array([simulate(t, t1, trail) for t in tr])
                flat, scaled = a[:, 0].mean(), a[:, 1].mean()
                dl = scaled - flat
                print(f"{sym:8s} {t1:5.1f} {trail:6.1f} {flat:+11.3f} {scaled:+11.3f} {dl:+9.3f}  "
                      f"{'scale-out better' if dl > 0 else 'flat 2-lot better'}")

    print("\n" + "=" * 100)
    print(" THE CAPITAL WALL")
    print("=" * 100)
    cap = 31146.0   # last good funds() read; broker session is revoked right now
    print(f"  available capital (last known) : Rs {cap:,.0f}")
    for name, lot, prem in (("NIFTY", 75, 270.0), ("SENSEX", 20, 300.0)):
        one = lot * prem
        print(f"  {name:7s} lot {lot:3d} x ~Rs{prem:6.0f} premium = Rs {one:8,.0f}/lot | "
              f"2 lots Rs {one * 2:8,.0f} = {one * 2 / cap * 100:5.1f}% of capital"
              f"{'   AFFORDABLE' if one * 2 <= cap else '   >>> CANNOT AFFORD'}")


if __name__ == "__main__":
    main()
