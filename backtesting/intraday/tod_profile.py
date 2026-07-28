"""
Where inside the session does intraday drift actually live?

Established facts from this repo (do not re-litigate):
  * Overnight (prev_close -> open) carries ALL the risk-adjusted drift:
    NIFTY Sharpe 2.68, SENSEX 3.57.
  * The intraday session (open -> close) has NEGATIVE Sharpe: -1.13 / -1.58.
  * Directional SMC/ICT intraday signals are dead: E[R] = -0.007R, t=-0.9,
    n=35,874 events.

But "intraday is negative" is an AGGREGATE. It does not mean every minute of the
session is negative. If the negative drift is concentrated in specific buckets
(e.g. the first 30 minutes as the overnight gap mean-reverts) while other buckets
are positive, then a time-of-day system is exploitable in a way that a
whole-session position is not.

This script maps that profile with no model and no parameters:
  1. Mean / Sharpe / hit-rate of every 5-minute bucket, pooled over all days.
  2. Same, split by whether the day GAPPED UP or DOWN (tests gap-fade directly:
     if the overnight drift is 'already delivered' at the open, the first buckets
     after a gap-up should be systematically weak).
  3. Same, split by weekday (captures expiry-day effects: NIFTY expires Thu,
     SENSEX Tue).
  4. Cumulative intraday path, so we can SEE where money is made or lost.

Cost reference: 2.84 bps/side statutory on index futures (Flattrade, zero
brokerage) + slippage. A one-way intraday trade therefore needs > ~7 bps of edge
per round trip to be worth doing. Every number below is printed in bps so it can
be compared against that bar directly.

Data: local DuckDB cache, 5m bars, 4 NSE indices, ~777 sessions.

Usage:
    ../venv/Scripts/python.exe backtesting/intraday/tod_profile.py
"""

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
ANN = 252
COST_ROUNDTRIP_BPS = 6.7      # 2.84*2 + ~1bp slippage


def load_5m(sym):
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, open, high, low, close
        FROM market_data WHERE symbol = ? AND interval = '5m' ORDER BY timestamp
    """, [sym]).fetchdf()
    con.close()
    if df.empty:
        return df
    df = df.set_index("ts")
    df = df[~df.index.duplicated(keep="first")]
    df["day"] = df.index.date
    df["hm"] = df.index.strftime("%H:%M")
    return df


def bucket_table(df):
    """Per-bar return in bps, plus per-day context (gap, weekday)."""
    df = df.copy()
    df["ret_bps"] = df["close"].pct_change() * 1e4
    # first bar of each day: return from previous close is the OVERNIGHT gap, not
    # an intraday move - null it so we only ever measure inside-session moves
    first_mask = df.groupby("day").cumcount() == 0
    day_open = df.groupby("day")["open"].transform("first")
    prev_close = df.groupby("day")["close"].transform("first").shift(1)
    df.loc[first_mask, "ret_bps"] = np.nan
    # gap sign per day: open vs previous day's close
    dayfirst = df[first_mask][["open"]].copy()
    dayfirst["prev_close"] = df.groupby("day")["close"].last().shift(1).values
    dayfirst["gap_bps"] = (dayfirst["open"] / dayfirst["prev_close"] - 1) * 1e4
    gap_map = dict(zip(dayfirst.index.date, dayfirst["gap_bps"]))
    df["gap_bps"] = [gap_map.get(d, np.nan) for d in df["day"]]
    df["weekday"] = df.index.day_name()
    return df.dropna(subset=["ret_bps"])


def profile(df, label):
    g = df.groupby("hm")["ret_bps"]
    out = pd.DataFrame({"n": g.size(), "mean_bps": g.mean().round(2),
                        "hit%": (g.apply(lambda s: 100 * (s > 0).mean())).round(1)})
    sd = g.std()
    out["t"] = (g.mean() / (sd / np.sqrt(g.size()))).round(2)
    print(f"\n--- {label}: 5-min bucket profile (bps) ---")
    print(f"{'time':>6s} {'n':>6s} {'mean':>7s} {'hit%':>6s} {'t':>7s}   bar")
    for hm, r in out.iterrows():
        bar = ("+" if r["mean_bps"] > 0 else "-") * min(int(abs(r["mean_bps"]) * 2), 40)
        flag = " *" if abs(r["t"]) > 2.5 else ""
        print(f"{hm:>6s} {int(r['n']):>6d} {r['mean_bps']:>7.2f} {r['hit%']:>6.1f} "
              f"{r['t']:>7.2f}   {bar}{flag}")
    return out


def block_stats(df, label, blocks):
    """Aggregate returns over named time blocks -> is any block tradable net of cost?"""
    print(f"\n--- {label}: aggregated blocks vs the {COST_ROUNDTRIP_BPS} bps cost bar ---")
    print(f"{'block':>16s} {'days':>6s} {'mean_bps':>9s} {'sharpe':>7s} {'t':>7s} "
          f"{'net_bps':>8s}  verdict")
    for name, (a, b) in blocks.items():
        sel = df[(df["hm"] >= a) & (df["hm"] < b)]
        per_day = sel.groupby(["symbol", "day"])["ret_bps"].sum()
        if len(per_day) < 200:
            continue
        m, sd = per_day.mean(), per_day.std()
        t = m / (sd / np.sqrt(len(per_day)))
        sh = (m / sd) * np.sqrt(ANN) if sd > 0 else 0
        net = abs(m) - COST_ROUNDTRIP_BPS
        verdict = ("TRADABLE " + ("SHORT" if m < 0 else "LONG")) if (net > 0 and abs(t) > 2.5) \
            else ("edge < cost" if abs(t) > 2.5 else "not significant")
        print(f"{name:>16s} {len(per_day):>6d} {m:>9.2f} {sh:>7.2f} {t:>7.2f} "
              f"{net:>8.2f}  {verdict}")


def main():
    allsym = []
    for sym in SYMBOLS:
        raw = load_5m(sym)
        if raw.empty:
            continue
        df = bucket_table(raw)
        df["symbol"] = sym
        allsym.append(df)
        print(f"{sym:11s} {df['day'].nunique():4d} sessions  "
              f"{min(df['day'])}..{max(df['day'])}")
    pool = pd.concat(allsym)
    print(f"\npooled: {len(pool):,} bar-observations, {pool['day'].nunique()} sessions, "
          f"{pool['symbol'].nunique()} instruments")

    profile(pool, "ALL instruments pooled")

    blocks = {
        "open_15m": ("09:15", "09:30"),
        "open_30m": ("09:15", "09:45"),
        "first_hour": ("09:15", "10:15"),
        "mid_morning": ("10:15", "11:30"),
        "lunch": ("11:30", "13:00"),
        "afternoon": ("13:00", "14:30"),
        "last_hour": ("14:30", "15:30"),
        "last_30m": ("15:00", "15:30"),
        "full_session": ("09:15", "15:30"),
    }
    block_stats(pool, "ALL pooled", blocks)

    # --- gap conditioning: does a gap-up get faded? ---
    up = pool[pool["gap_bps"] > 20]
    dn = pool[pool["gap_bps"] < -20]
    print(f"\ngap-up days (>+20bps): {up['day'].nunique()}   "
          f"gap-down days (<-20bps): {dn['day'].nunique()}")
    block_stats(up, "after GAP UP", blocks)
    block_stats(dn, "after GAP DOWN", blocks)

    # --- weekday conditioning (expiry effects) ---
    for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        sub = pool[pool["weekday"] == wd]
        if sub["day"].nunique() < 40:
            continue
        per_day = sub[(sub["hm"] >= "09:15") & (sub["hm"] < "15:30")].groupby(["symbol", "day"])["ret_bps"].sum()
        m, sd = per_day.mean(), per_day.std()
        t = m / (sd / np.sqrt(len(per_day)))
        print(f"  {wd:9s} full-session mean {m:>8.2f} bps  t={t:>6.2f}  "
              f"n={len(per_day)}")

    # --- per instrument sanity on the strongest block ---
    print("\n--- per-instrument check on open_30m (the classic gap-reversion window) ---")
    for sym in SYMBOLS:
        s = pool[pool["symbol"] == sym]
        if s.empty:
            continue
        pd_ = s[(s["hm"] >= "09:15") & (s["hm"] < "09:45")].groupby("day")["ret_bps"].sum()
        m, sd = pd_.mean(), pd_.std()
        t = m / (sd / np.sqrt(len(pd_)))
        print(f"  {sym:11s} mean {m:>8.2f} bps  t={t:>6.2f}  n={len(pd_)}")


if __name__ == "__main__":
    main()
