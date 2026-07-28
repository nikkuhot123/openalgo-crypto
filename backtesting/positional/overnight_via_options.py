"""
Can the overnight-drift edge be harvested with ~Rs 40,000 using OPTIONS?

The futures route is closed by margin: one NIFTY lot needs ~Rs 2.34L of
SPAN+Exposure to carry overnight, and India has no overnight leverage product.
An option, however, costs only its premium - roughly Rs 19,000 for one ATM lot -
so it is the only affordable way to express the trade.

The question is whether the gap edge survives OPTION mechanics for one night:
  + delta < 1, so you capture only part of the index gap
  - one night of theta
  - option bid/ask, far wider than the future's

This is tested on REAL option premiums, not a delta model: harvest_options_archive.db
holds genuine NIFTY option 1m OHLC with strike/expiry/OI. Its trustworthy span is
2026-02..2026-05 (earlier rows carry corrupt timestamps - options expiring 2026-06
with bars dated 2023 - and it is unreadable past rowid ~27.19M), so this is a
~4-month, ~80-session test. Small, but real premiums beat a model every time.

Rules mirror the validated futures book exactly:
    entry 15:20, exit 09:20 next session, only when the daily MA-ensemble trend
    filter is long (the gate that turned -27% DD into single digits).

Moneyness is swept: ATM through ITM. Deeper ITM has higher delta and lower
theta-as-%-of-premium, which is the theoretically correct choice for a one-night
directional hold - but it costs more premium, so affordability is reported too.

Usage:
    ../venv/Scripts/python.exe backtesting/positional/overnight_via_options.py
"""

import sqlite3
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ARCHIVE = Path("harvest_options_archive.db")
CACHE = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
LOT = 75
LOOKBACKS = (50, 75, 100, 150, 200)
OPT_SLIP_PCT = 0.5          # per side, % of premium - realistic for NIFTY weeklies
CAPITAL = 40000
GOOD_FROM, GOOD_TO = "2026-02-01", "2026-05-30"    # trustworthy span of the archive


def trend_gate():
    """Daily MA-ensemble long/flat signal, causal (uses closes through t-1)."""
    con = duckdb.connect(str(CACHE), read_only=True)
    d = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, close
        FROM market_data WHERE symbol='NIFTY' AND interval='D' ORDER BY timestamp
    """).fetchdf()
    con.close()
    if len(d) < max(LOOKBACKS) + 5:
        # fall back to 5m -> daily if the D series is short
        con = duckdb.connect(str(CACHE), read_only=True)
        d = con.execute("""
            SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, close
            FROM market_data WHERE symbol='NIFTY' AND interval='5m' ORDER BY timestamp
        """).fetchdf()
        con.close()
        d = d.set_index("ts").resample("1D").last().dropna().reset_index()
    d = d.set_index("ts")
    flags = [(d["close"] > d["close"].rolling(n).mean()).astype(float) for n in LOOKBACKS]
    sig = pd.concat(flags, axis=1).mean(axis=1).shift(1)
    return {ts.date(): float(v) for ts, v in sig.dropna().items()}


def load_option_snaps():
    """Premium at 15:20 and 09:20 for every NIFTY CE in the trustworthy window."""
    con = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True)
    q = """
        SELECT substr(timestamp,1,10) AS d, substr(timestamp,12,5) AS hm,
               tradingsymbol, expiry, strike, close
        FROM options_bars_full
        WHERE rowid < 27000000 AND underlying='NIFTY' AND interval='minute'
          AND option_type='CE'
          AND substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) <= ?
          AND substr(timestamp,12,5) IN ('15:20','09:20')
    """
    rows = con.execute(q, (GOOD_FROM, GOOD_TO)).fetchall()
    con.close()
    df = pd.DataFrame(rows, columns=["d", "hm", "sym", "expiry", "strike", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    return df.dropna()


def spot_at_1520():
    con = duckdb.connect(str(CACHE), read_only=True)
    d = con.execute("""
        SELECT strftime(to_timestamp(timestamp)::TIMESTAMP,'%Y-%m-%d') AS d,
               strftime(to_timestamp(timestamp)::TIMESTAMP,'%H:%M') AS hm, close
        FROM market_data WHERE symbol='NIFTY' AND interval='1m'
    """).fetchdf()
    con.close()
    d = d[d["hm"] == "15:20"]
    return dict(zip(d["d"], d["close"].astype(float)))


def main():
    gate = trend_gate()
    spot = spot_at_1520()
    opt = load_option_snaps()
    if opt.empty:
        print("no option rows in the trustworthy window - cannot test")
        return
    print(f"option rows: {len(opt):,}  days: {opt['d'].nunique()}  "
          f"{opt['d'].min()}..{opt['d'].max()}")

    ev = opt.pivot_table(index=["d", "sym", "expiry", "strike"], columns="hm",
                         values="close", aggfunc="last").reset_index()
    if "15:20" not in ev.columns or "09:20" not in ev.columns:
        print("missing one of the two timestamps")
        return

    days = sorted(ev["d"].unique())
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}

    print(f"\n{'moneyness':>10s} {'trades':>7s} {'premium':>9s} {'affordable':>11s} "
          f"{'avg_pnl':>9s} {'win%':>6s} {'total':>9s} {'ret_on_cap':>11s}")
    print("-" * 84)

    for offset, label in ((0, "ATM"), (-100, "ITM100"), (-200, "ITM200"), (-400, "ITM400")):
        trades = []
        for d in days[:-1]:
            if gate.get(pd.Timestamp(d).date(), 0) <= 0:
                continue                                  # trend filter says stand aside
            s = spot.get(d)
            if not s:
                continue
            want = round((s + offset) / 50) * 50           # 50-pt strike grid
            today = ev[(ev["d"] == d) & (ev["strike"] == want)]
            if today.empty:
                continue
            # nearest expiry strictly after today, so it survives the night
            today = today[today["expiry"] > d].sort_values("expiry")
            if today.empty:
                continue
            row = today.iloc[0]
            entry = row.get("15:20")
            if not entry or entry <= 0 or np.isnan(entry):
                continue
            tomo = ev[(ev["d"] == nxt[d]) & (ev["sym"] == row["sym"])]
            if tomo.empty:
                continue
            exit_ = tomo.iloc[0].get("09:20")
            if not exit_ or np.isnan(exit_):
                continue
            slip = (entry + exit_) * OPT_SLIP_PCT / 100
            pnl = (exit_ - entry - slip) * LOT
            trades.append({"d": d, "entry": entry, "exit": exit_, "pnl": pnl,
                           "cost": entry * LOT})
        if not trades:
            print(f"{label:>10s} {'0':>7s}   (no qualifying trades)")
            continue
        t = pd.DataFrame(trades)
        prem = t["cost"].mean()
        print(f"{label:>10s} {len(t):>7d} {prem:>9,.0f} "
              f"{'YES' if prem <= CAPITAL else 'NO':>11s} "
              f"{t['pnl'].mean():>9,.0f} {100*(t['pnl']>0).mean():>6.1f} "
              f"{t['pnl'].sum():>9,.0f} {100*t['pnl'].sum()/CAPITAL:>10.1f}%")

    print(f"\nnotes: premium = avg cost of 1 lot ({LOT} qty) at entry; "
          f"affordable vs Rs {CAPITAL:,} capital.")
    print(f"       option slippage {OPT_SLIP_PCT}%/side applied on both legs.")
    print("       trend gate = daily MA-ensemble long/flat, same as the futures book.")


if __name__ == "__main__":
    main()
