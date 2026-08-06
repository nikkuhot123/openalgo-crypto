"""
Red Bar / X-Candle — 2-lot split-exit backtest (₹100k account model)
====================================================================
User spec: two lots per signal; lot A exits at the strategy's 3R target;
lot B stays in and trails to capture the maximum move.

Model, on the same faithful signal engine as redbar_backtest.py (imports the
strategy's own compute_red_bar_signal):

  - entry: both lots at the trigger bar close, 1 trade/day (MAX_TRADES_PER_DAY=1)
  - lot A stop: the strategy's sl_spot at all times
  - lot B stop: sl_spot until the trail activates, then a chandelier stop
    trail = peak_close - k*R (CE) / peak_close + k*R (PE), k swept
    activation: cumulative favourable move >= trail_start*R (0 = from entry,
    1 = after T1), ratchet ONLY on bar closes (the live loop monitors LTP at
    poll granularity; closes are the conservative reading), stop never looser
    than the strategy's SL
  - fills: at the stop/target level (stop-limit semantics), EOD/max-hold at
    bar close; exit priority EOD > stops > target > max-hold as in the live loop
  - costs: statutory 0.12% of premium x 2 (entry+exit) + 0.41% spread, per lot;
    2 lots => ~Rs 114/trade round trip at current ATM premium
  - P&L: spot points x measured delta 0.358 x lot. Theta NOT modelled
    (honest bias: OPTIMISTIC for the trailing leg, which holds past target).

Account frame: Rs 100,000. 2 NIFTY lots @ ~Rs 6.9k premium = ~14% exposure;
no margin breach is possible in this history (premium max reported). The point
of the Rs 100k frame is only to say the position is comfortably affordable -
this file does NOT model scaling beyond the fixed 2 lots.

# ============================================================================
# RESULTS (run 2026-08-06, 2 lots, Rs 100k frame, same signal engine)
# ============================================================================
# NIFTY 776 sessions 30m:
#   baseline both lots @ 3R target, hold 90m (live cfg): +44,517  PF 1.07
#     (exactly 2x the 1-lot +22,259 -> harness verified)
#   + B trails 2R from TARGET, hold 90m:                   +47,256  PF 1.08
#   + B trails 2R from TARGET, hold to EOD 15:10:        +111,270  PF 1.15
#   --mod contribution: the 2R trail adds ~+2.7k (nothing);
#     the +67k jump comes from LIFTING MAX_HOLD_MINUTES=90,
#     which doubles A's target hits (57 -> 109).
#   HONESTY TRAP: 297/694 B-leg exits ride 0DTE to 15:10 and alone gross
#   +675,616 (SL losers offset -564,346). Those 297 afternoon positions are
#   pure theta exposure that this spot model does NOT charge. MAX_HOLD_MINUTES
#   exists exactly because 0DTE theta bleeds. Removing it is not an edge --
#   it is a single-session-optimistic artifact. The 90m-hold comparison
#   (+47k vs +44.5k) is the defensible number: trailing adds ~nothing.
#   Year split at 90m: 23 -13.7k | 24 +33.7k | 25 -10.6k | 26 +35.1k
#   -> 2 of 4 years negative; no regime stability. Not deployable.
# SENSEX 79 sessions: every variant negative (best -1.8k, PF 0.97 at 1R-from-ENTRY).
# ============================================================================
"""
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("OPENALGO_API_KEY", "backtest-dummy")
sys.path.insert(0, str(Path(__file__).parent))

import redbar_backtest as rb  # faithful 1-lot harness (signal import, loads, costs)

DELTA = rb.DELTA
LOOKBACK_DAYS = rb.LOOKBACK_DAYS
INTERVAL_MIN = rb.INTERVAL_MIN


def resample(df, mins=30):
    out = []
    for day, g in df.groupby(df.index.date):
        g = g.sort_index()
        origin = pd.Timestamp(f"{day} 09:15:00")
        out.append(g.resample(f"{mins}min", origin=origin).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna())
    return pd.concat(out).sort_index()


def lot_cost(prem, lot):
    return (2 * prem * lot) * rb.OPT_COST_PCT / 100.0 + prem * lot * rb.SPREAD_PCT / 100.0


def backtest_2lot(m, df, symbol, lot, prem_pct, trail_k, trail_start=3.0):
    """Two lots; lot A exits at the strategy's 3R target. Lot B:
      - trail_k=None: B mirrors A exactly (both-at-target baseline).
      - otherwise: B holds the strategy SL until the trail activates, then
        chandelier-trails k*R off the peak/trough close. Activation:
        cumulative favourable move >= trail_start*R. trail_start=3.0 = "from
        the target bar" (the user's literal spec: A books at the target, B
        stays in and trails to capture the maximum beyond it); 0.0 = live
        from entry; 1.0 = from T1.
      Trail ratchets ONLY on bar closes (live loop monitors LTP at poll
      granularity; closes are the conservative reading), never looser than SL.
      Fills at stop/target level (stop-limit semantics); EOD/max-hold at bar
      close; exit priority EOD > stops > target > max-hold as in the live loop.
      Costs: 2-lot statutory 0.12% x2 + 0.41% spread per lot. Theta NOT
      modelled (optimistic bias for the trailing leg, which holds past target)."""
    days = sorted({d.date() for d in df.index})
    trades = []

    for di, day in enumerate(days):
        if di < LOOKBACK_DAYS:
            continue
        window_start = days[di - LOOKBACK_DAYS]
        sl = df[(df.index.date >= window_start) & (df.index.date <= day)]
        today_bars = sl[sl.index.date == day]
        if len(today_bars) < 6:
            continue
        prev_day = df[df.index.date == days[di - 1]]
        prev_close = float(prev_day["close"].iloc[-1]) if len(prev_day) else None

        n_today = len(today_bars)
        base = len(sl) - n_today
        sig = None
        for k in range(n_today):
            s = m.compute_red_bar_signal(sl.iloc[: base + k + 1], day, None, prev_close)
            if s and s.get("signal"):
                sig, trig_idx = s, k - 1
                break
        if not sig:
            continue

        entry = sig["entry_spot"]
        slp = sig["sl_spot"]
        tgt = sig["target_spot"]
        risk = sig["risk"]
        side = 1 if sig["signal"] == "CE" else -1
        entry_time = today_bars.index[trig_idx] + pd.Timedelta(minutes=INTERVAL_MIN)
        prem = entry * prem_pct / 100.0

        peak = entry
        trail = None
        trail_active = False
        a_exit = b_exit = None
        a_reason = b_reason = None
        trail_bar_seen = False

        for ts, row in today_bars.iloc[trig_idx + 1:].iterrows():
            held = (ts - entry_time).total_seconds() / 60.0
            if ts.time() >= m.EXIT_TIME:
                if a_exit is None:
                    a_exit, a_reason = row["close"], "EOD"
                if b_exit is None:
                    b_exit, b_reason = row["close"], "EOD"
                break

            # stops (B: trail once active, else the SL; baseline B == A)
            if a_exit is None:
                if (row["low"] <= slp) if side == 1 else (row["high"] >= slp):
                    a_exit, a_reason = slp, "SL"
            if b_exit is None:
                stop = trail if trail_active else slp
                if (row["low"] <= stop) if side == 1 else (row["high"] >= stop):
                    b_exit, b_reason = stop, "trail" if trail_active else "SL"

            # target: A always; B too when mirroring (no trailing)
            if a_exit is None:
                if (row["high"] >= tgt) if side == 1 else (row["low"] <= tgt):
                    a_exit, a_reason = tgt, "target"
                    trail_bar_seen = True
            if trail_k is None and b_exit is None and trail_bar_seen:
                b_exit, b_reason = tgt, "target"

            if (a_exit is None or b_exit is None) and held >= m.MAX_HOLD_MINUTES:
                if a_exit is None:
                    a_exit, a_reason = row["close"], "max-hold"
                if b_exit is None:
                    b_exit, b_reason = row["close"], "max-hold"
                break
            if a_exit is not None and b_exit is not None:
                break

            # ratchet B's trail on the close
            if b_exit is None and trail_k is not None:
                peak = max(peak, row["close"]) if side == 1 else min(peak, row["close"])
                fav = (peak - entry) * side
                if not trail_active and fav >= trail_start * risk:
                    trail_active = True
                if trail_active:
                    cand = peak - trail_k * risk if side == 1 else peak + trail_k * risk
                    cand = max(cand, slp) if side == 1 else min(cand, slp)
                    trail = cand if trail is None else \
                        (max(trail, cand) if side == 1 else min(trail, cand))

        if a_exit is None or b_exit is None:
            if len(today_bars.iloc[trig_idx + 1:]) == 0:
                continue
            last_close = today_bars["close"].iloc[-1]
            if a_exit is None:
                a_exit, a_reason = last_close, "EOD"
            if b_exit is None:
                b_exit, b_reason = last_close, "EOD"

        pts_a = (a_exit - entry) * side
        pts_b = (b_exit - entry) * side
        tot = pts_a + pts_b
        cost = 2 * lot_cost(prem, lot)
        trades.append({
            "date": day, "dir": sig["signal"], "anchor": sig["anchor"],
            "entry": entry, "risk": risk,
            "a_exit": a_exit, "a_reason": a_reason, "ra": pts_a / risk if risk else 0.0,
            "b_exit": b_exit, "b_reason": b_reason, "rb": pts_b / risk if risk else 0.0,
            "pts": tot, "R": tot / risk if risk else 0.0,
            "rs": tot * DELTA * lot - cost, "prem2": 2 * prem * lot,
        })
    return pd.DataFrame(trades)


def pf(x):
    gw = x.loc[x["rs"] > 0, "rs"].sum()
    gl = abs(x.loc[x["rs"] <= 0, "rs"].sum())
    return gw / gl if gl else 99.0


def report(t, label):
    if t.empty:
        print(f"{label:44s} | no trades")
        return
    n = len(t)
    w = (t["rs"] > 0).mean() * 100
    print(f"{label:44s} | T:{n:4d} W:{w:5.1f}%  Rs {t['rs'].sum():+9,.0f} "
          f"({t['rs'].mean():+6.0f}/tr)  R {t['R'].mean():+5.2f}/tr  PF {pf(t):4.2f}  "
          f"max prem {t['prem2'].max():7,.0f} ({t['prem2'].max()/100000*100:.1f}% of 100k)")
    tg = t[t["a_reason"] == "target"]
    if len(tg) > 0:
        print(f"        target days: {len(tg)}  B captured {tg['rb'].mean():+.2f}R mean "
              f"(A booked {tg['ra'].mean():+.2f}R); B exits: " +
              " | ".join(f"{k} {v}" for k, v in
                         tg["b_reason"].value_counts().items()))
    vc = pd.Series([f"A:{r}" for r in t["a_reason"]] + [f"B:{r}" for r in t["b_reason"]]).value_counts()
    print("        all exits: " + " | ".join(f"{k} {v}" for k, v in vc.items()))


if __name__ == "__main__":
    m = rb.load_strategy()
    print(f"Params: FIB {m.FIB_LO}/{m.FIB_HI} | RR {m.RR} | MIN/MAX SL {m.MIN_SL_PCT}/{m.MAX_SL_PCT}% | "
          f"max-hold {m.MAX_HOLD_MINUTES}min | exit {m.EXIT_TIME} | delta {DELTA} | 2-lot cost ~Rs 114 RT")
    print()

    for sym, lot, pp in (("NIFTY", 65, 0.55), ("SENSEX", 20, 0.45)):
        d5 = rb.load_bars(sym)
        if d5.empty:
            continue
        d = resample(d5, 30)
        print("=" * 140)
        print(f" {sym} — {len({x.date() for x in d5.index})} sessions, 30m bars, Rs 100k frame, 2 lots")
        print("=" * 140)

        report(backtest_2lot(m, d, sym, lot, pp, None, 3.0), "baseline: BOTH lots at 3R target")
        report(backtest_2lot(m, d, sym, lot, pp, 2.0, 3.0),
               "B trails 2R from TARGET (A books at 3R)")
        report(backtest_2lot(m, d, sym, lot, pp, 1.5, 3.0),
               "B trails 1.5R from TARGET (A books at 3R)")
        report(backtest_2lot(m, d, sym, lot, pp, 1.0, 3.0),
               "B trails 1R from TARGET (A books at 3R)")
        report(backtest_2lot(m, d, sym, lot, pp, 0.5, 3.0),
               "B trails 0.5R from TARGET (A books at 3R)")
        report(backtest_2lot(m, d, sym, lot, pp, 0.25, 3.0),
               "B trails 0.25R from TARGET (A books at 3R)")
        report(backtest_2lot(m, d, sym, lot, pp, 1.0, 1.0),
               "B trails 1R from T1 (A books at 3R)")
        report(backtest_2lot(m, d, sym, lot, pp, 1.0, 0.0),
               "B trails 1R from ENTRY (A books at 3R)")
