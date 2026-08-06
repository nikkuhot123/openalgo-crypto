"""
Judas Swing — is one-trade-per-day right, or is it leaving money behind?
=========================================================================
The cap is not a config knob; it is hardcoded (`state = "DONE"` after any
exit, judas_swing_strategy.py:804). This checks whether that is load-bearing
or merely conservative, by replaying the signal definition itself.

The signal (compute_judas_signal):
    OR          = high/low of 09:15..OR_END
    swept_high  = any candle in (OR_END, SWEEP_END) trades above OR high
    swept_low   = ditto below OR low                       <- STICKY day flags
    PE  if swept_high and close < or_high                  <- a CONDITION,
    CE  elif swept_low and close > or_low                     not an event

Because the flags are sticky and the test is a condition, the signal stays
TRUE for every later candle that closes back inside the range. So the
question is not "should we allow a 2nd trade" but "how many times would this
same condition re-fire in a day if nothing stopped it".

Measured per session:
    fire_bars   candles in the entry window that would emit a signal
    both_swept  did the day sweep BOTH sides (the only case where a genuinely
                different second setup could exist)
    pe_locked   on both-swept days, is the CE branch reachable at all?
                (PE is tested first, so it wins whenever price sits below
                 or_high -- the CE branch is dead code for most of the day)

Usage:
    ./venv/Scripts/python.exe backtesting/haema_signal/judas_frequency.py
"""
import sys
from datetime import time as dtime
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
DB = ROOT / "backtesting/data/market_cache.duckdb"
# the 5m table in duckdb ends 2026-05-27 (only the 1m table reaches 07-28),
# so anything later must come from single-day API calls
CACHE_END = pd.Timestamp("2026-05-27").date()

OR_END = dtime(9, 45)
# NIFTY registration values (SENSEX uses 10:30 / 13:00; both are checked)
BOOKS = {"NIFTY": (dtime(12, 0), dtime(14, 0)), "SENSEX": (dtime(10, 30), dtime(13, 0))}


def client():
    from openalgo import api
    env = (ROOT / ".env").read_text()
    return api(api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
               host=env.split("OPENALGO_HOST=")[1].split()[0])


def spot_5m(under, day, c):
    if day <= CACHE_END:
        con = duckdb.connect(str(DB), read_only=True)
        df = con.execute(
            "select timestamp, open, high, low, close from market_data "
            f"where symbol='{under}' and interval='5m' order by timestamp").df()
        con.close()
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
        df = df[df["ts"].dt.date == day]
        return df.set_index("ts")[["open", "high", "low", "close"]]
    ex = "BSE_INDEX" if under == "SENSEX" else "NSE_INDEX"
    r = c.history(symbol=under, exchange=ex, interval="5m",
                  start_date=day.isoformat(), end_date=day.isoformat())
    if not isinstance(r, pd.DataFrame) or r.empty:
        return pd.DataFrame()
    r = r.copy()
    r.index = pd.to_datetime(r.index).tz_localize(None)
    return r[["open", "high", "low", "close"]]


def analyse(df, sweep_end, entry_end):
    """Replay the signal condition candle by candle for one session."""
    if df.empty:
        return None
    o = df[df.index.time < OR_END]
    if o.empty:
        return None
    or_high, or_low = float(o["high"].max()), float(o["low"].min())

    swept_high = swept_low = False
    fires, dirs, first_fire = 0, [], None
    for ts, row in df[df.index.time >= OR_END].iterrows():
        t = ts.time()
        if t < sweep_end:                       # flags latch inside sweep window
            if row["high"] > or_high:
                swept_high = True
            if row["low"] < or_low:
                swept_low = True
        if not (OR_END <= t < entry_end):
            continue
        sig = None
        if swept_high and row["close"] < or_high:
            sig = "PE"
        elif swept_low and row["close"] > or_low:
            sig = "CE"
        if sig:
            fires += 1
            dirs.append(sig)
            if first_fire is None:
                first_fire = (ts, sig)
    return {"or_high": or_high, "or_low": or_low,
            "swept_high": swept_high, "swept_low": swept_low,
            "both_swept": swept_high and swept_low,
            "fire_bars": fires, "dirs": dirs,
            "first": first_fire}


def main():
    orders = pd.read_csv(HERE / "judas_orders.csv", parse_dates=["ts"])
    days = sorted({d.date() for d in orders["ts"]})
    c = client()
    rows = []
    for under, (sweep_end, entry_end) in BOOKS.items():
        for day in days:
            try:
                df = spot_5m(under, day, c)
            except Exception:
                continue
            r = analyse(df, sweep_end, entry_end)
            if not r:
                continue
            pe = r["dirs"].count("PE")
            ce = r["dirs"].count("CE")
            rows.append({"under": under, "date": day, "both_swept": r["both_swept"],
                         "swept_hi": r["swept_high"], "swept_lo": r["swept_low"],
                         "fire_bars": r["fire_bars"], "PE_bars": pe, "CE_bars": ce,
                         "first": r["first"][1] if r["first"] else None})
    d = pd.DataFrame(rows)
    if d.empty:
        sys.exit("no sessions replayed")
    d.to_csv(HERE / "judas_frequency.csv", index=False)

    print(d.to_string(index=False))
    print(f"\nsessions replayed: {len(d)}")
    fired = d[d["fire_bars"] > 0]
    print(f"sessions where the signal fires at all: {len(fired)}/{len(d)}")
    print(f"  candles per firing session: mean {fired['fire_bars'].mean():.1f} | "
          f"median {fired['fire_bars'].median():.0f} | max {fired['fire_bars'].max()}")
    print(f"  i.e. WITHOUT the one-trade-per-day rule the strategy would re-enter "
          f"~{fired['fire_bars'].median():.0f} times a day on the same setup")
    both = d[d["both_swept"]]
    print(f"\nboth sides swept (only case a genuine 2nd setup could exist): "
          f"{len(both)}/{len(d)} sessions")
    if len(both):
        dead = both[(both["PE_bars"] > 0) & (both["CE_bars"] == 0)]
        print(f"  of those, CE branch never reachable (PE tested first): "
              f"{len(dead)}/{len(both)}")
        print(f"  mean PE bars {both['PE_bars'].mean():.1f} vs CE bars "
              f"{both['CE_bars'].mean():.1f}")


if __name__ == "__main__":
    main()
