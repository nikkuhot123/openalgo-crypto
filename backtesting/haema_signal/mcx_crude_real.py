"""
HA-EMA Channel on REAL MCX CRUDEOILM data — honest cost model, session-aware.
=============================================================================
No WTI proxy. No hand-waved slippage. Real contract, real Indian statutory costs.

MCX CRUDEOILM contract spec:
  Lot size      : 10 barrels
  Tick size     : Rs 1 per barrel  ->  Rs 10 per lot per tick
  Session       : 09:00 - 23:30 IST (23:55 on US DST shift)
  Notional      : ~Rs 74,000 per lot at Rs 7,400/bbl
  SPAN margin   : ~Rs 18,000 - 22,000 per lot

Cost model (round trip, per lot, at Rs 74,000 notional):
  CTT           : 0.01% on SELL side notional        = Rs 7.40
  Exchange txn  : Rs 2.10 per lakh per side          = Rs 3.11
  SEBI turnover : Rs 10 per crore                    = Rs 0.15
  Stamp duty    : 0.002% on BUY side                 = Rs 1.48
  GST           : 18% on (brokerage + exch + SEBI)
  Brokerage     : modelled both at Rs 0 and Rs 20/order
  Slippage      : 1 tick per side = Rs 10 x 2        = Rs 20.00
"""
import numpy as np
import pandas as pd
from openalgo import api

API_KEY = "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a"
HOST = "https://openalgo.inikhilesh.com"

LOT_BARRELS = 10
TICK_RS = 1.0            # per barrel
SLIP_TICKS_PER_SIDE = 1  # conservative: cross one tick each way


# ── Cost model ───────────────────────────────────────────────────────────
def round_trip_cost(entry_px, exit_px, brokerage_per_order=0.0):
    """Real MCX round-trip cost in rupees for ONE lot of CRUDEOILM."""
    buy_notional = entry_px * LOT_BARRELS
    sell_notional = exit_px * LOT_BARRELS

    ctt = 0.0001 * sell_notional              # 0.01% sell side
    exch = 0.000021 * (buy_notional + sell_notional)   # Rs 2.10 / lakh / side
    sebi = 0.000001 * (buy_notional + sell_notional)   # Rs 10 / crore
    stamp = 0.00002 * buy_notional            # 0.002% buy side
    brok = brokerage_per_order * 2
    gst = 0.18 * (brok + exch + sebi)
    slippage = SLIP_TICKS_PER_SIDE * TICK_RS * LOT_BARRELS * 2

    return ctt + exch + sebi + stamp + brok + gst + slippage


# ── Indicators ───────────────────────────────────────────────────────────
def ha_bias_from_intraday(df):
    """Build daily OHLC from intraday, then Heikin-Ashi bias (causal)."""
    daily = df.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    n = len(daily)
    ho = np.empty(n)
    hc = np.empty(n)
    ho[0] = (daily["open"].iloc[0] + daily["close"].iloc[0]) / 2
    hc[0] = daily.iloc[0][["open", "high", "low", "close"]].mean()
    for i in range(1, n):
        hc[i] = daily.iloc[i][["open", "high", "low", "close"]].mean()
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    bias = pd.Series(np.where(hc > ho, "GREEN", "RED"), index=daily.index).shift(1)
    return {d.date(): v for d, v in bias.items()}


def ema_arr(values, period):
    a = np.asarray(values, dtype=float)
    out = np.empty_like(a)
    k = 2.0 / (period + 1)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


# ── Backtest ─────────────────────────────────────────────────────────────
def backtest(
    df,
    ema_period=55,
    trail_trigger_r=1.0,
    trail_lock_r=1.0,
    trail_step_r=0.5,
    max_trades_per_day=1,
    session_start=9,
    session_end=22,
    eod_hour=23,
    brokerage_per_order=0.0,
):
    """Returns trade DataFrame with rupee P&L per lot, net of real costs."""
    bias_map = ha_bias_from_intraday(df)
    eh = ema_arr(df["high"].values, ema_period)
    el = ema_arr(df["low"].values, ema_period)

    hours = df.index.hour.values
    dates = np.array([d.date() for d in df.index])
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values

    trades = []
    pos = 0
    entry_px = sl_px = risk = best_px = 0.0
    entry_time = None
    day_count = 0
    cur_date = None
    trailing = False

    for i in range(ema_period + 1, len(df)):
        d, h = dates[i], hours[i]
        if d != cur_date:
            cur_date = d
            day_count = 0

        bias = bias_map.get(d)
        if bias is None or (isinstance(bias, float) and np.isnan(bias)):
            continue

        # ── manage open position ──
        if pos != 0:
            exit_px = None
            exit_type = None

            if pos == 1:
                best_px = max(best_px, highs[i])
                if not trailing and best_px >= entry_px + trail_trigger_r * risk:
                    trailing = True
                    sl_px = entry_px + trail_lock_r * risk
                if trailing:
                    sl_px = max(sl_px, best_px - trail_step_r * risk)
                if lows[i] <= sl_px:
                    exit_px, exit_type = sl_px, ("TRAIL" if trailing else "SL")
            else:
                best_px = min(best_px, lows[i])
                if not trailing and best_px <= entry_px - trail_trigger_r * risk:
                    trailing = True
                    sl_px = entry_px - trail_lock_r * risk
                if trailing:
                    sl_px = min(sl_px, best_px + trail_step_r * risk)
                if highs[i] >= sl_px:
                    exit_px, exit_type = sl_px, ("TRAIL" if trailing else "SL")

            if exit_px is None and h >= eod_hour:
                exit_px, exit_type = closes[i], "EOD"

            if exit_px is not None:
                gross = (exit_px - entry_px) if pos == 1 else (entry_px - exit_px)
                gross_rs = gross * LOT_BARRELS
                cost_rs = round_trip_cost(entry_px, exit_px, brokerage_per_order)
                trades.append({
                    "entry_time": entry_time, "exit_time": df.index[i],
                    "dir": "LONG" if pos == 1 else "SHORT",
                    "entry": entry_px, "exit": exit_px,
                    "gross_rs": gross_rs, "cost_rs": cost_rs,
                    "net_rs": gross_rs - cost_rs,
                    "exit_type": exit_type,
                    "entry_hour": entry_time.hour,
                })
                pos = 0
                trailing = False
                continue

        # ── entry ──
        if pos == 0 and day_count < max_trades_per_day and session_start <= h < session_end:
            c, hi, lo = closes[i], highs[i], lows[i]
            if bias == "GREEN" and c > eh[i]:
                r = c - lo
                if r >= TICK_RS:
                    pos, entry_px, sl_px, best_px = 1, c, lo, c
                    risk, entry_time, trailing = r, df.index[i], False
                    day_count += 1
            elif bias == "RED" and c < el[i]:
                r = hi - c
                if r >= TICK_RS:
                    pos, entry_px, sl_px, best_px = -1, c, hi, c
                    risk, entry_time, trailing = r, df.index[i], False
                    day_count += 1

    return pd.DataFrame(trades)


def stats(tdf, label, show_costs=False):
    if tdf.empty:
        print(f"{label:52s} | no trades")
        return None
    n = len(tdf)
    w = (tdf["net_rs"] > 0).sum()
    wr = w / n * 100
    net = tdf["net_rs"].sum()
    gross = tdf["gross_rs"].sum()
    cost = tdf["cost_rs"].sum()
    avg = tdf["net_rs"].mean()
    gw = tdf.loc[tdf["net_rs"] > 0, "net_rs"].sum()
    gl = abs(tdf.loc[tdf["net_rs"] <= 0, "net_rs"].sum())
    pf = gw / gl if gl > 0 else float("inf")

    # max drawdown on the equity curve
    eq = tdf["net_rs"].cumsum()
    dd = (eq - eq.cummax()).min()

    extra = f" | gross Rs{gross:+,.0f} cost Rs{cost:,.0f}" if show_costs else ""
    print(
        f"{label:52s} | T:{n:4d} W:{wr:5.1f}% net Rs{net:+8,.0f} "
        f"avg Rs{avg:+7.1f} PF:{pf:5.2f} maxDD Rs{dd:+8,.0f}{extra}"
    )
    return {"label": label, "n": n, "wr": wr, "net": net, "avg": avg, "pf": pf, "dd": dd}


if __name__ == "__main__":
    c = api(api_key=API_KEY, host=HOST)
    print("Fetching real MCX CRUDEOILM 5m data...")
    df = c.history(
        symbol="CRUDEOILM19AUG26FUT", exchange="MCX", interval="5m",
        start_date="2026-01-01", end_date="2026-08-05",
    )
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
    sessions = len(set(d.date() for d in df.index))
    print(f"  {len(df):,} bars | {df.index[0]} -> {df.index[-1]} | {sessions} sessions\n")

    # cost reality check
    demo_cost = round_trip_cost(7400, 7400)
    print(f"Real MCX round-trip cost @ Rs7,400: Rs {demo_cost:.2f}/lot "
          f"({demo_cost / (7400 * LOT_BARRELS) * 10000:.2f} bps)")
    print(f"  -> strategy must clear {demo_cost / LOT_BARRELS:.2f} points/barrel just to break even\n")

    print("=" * 130)
    print(" BASELINE: config tuned on WTI proxy, applied to real MCX")
    print("=" * 130)
    t = backtest(df, ema_period=55, trail_trigger_r=1.0)
    stats(t, "EMA=55 trigger=1.0 lock=1.0 step=0.5 (WTI-tuned)", show_costs=True)

    print()
    print("=" * 130)
    print(" SWEEP on real MCX data")
    print("=" * 130)
    results = []
    for ep in [21, 34, 44, 55, 89]:
        for trig in [0.8, 1.0, 1.5]:
            for step in [0.5, 1.0]:
                t = backtest(df, ema_period=ep, trail_trigger_r=trig, trail_step_r=step)
                r = stats(t, f"EMA={ep:2d} trig={trig} step={step}")
                if r:
                    r.update(ep=ep, trig=trig, step=step)
                    results.append(r)

    results.sort(key=lambda x: x["net"], reverse=True)

    print()
    print("=" * 130)
    print(" SESSION SPLIT — MCX morning (09-17, thin) vs evening (17-23, NYMEX live)")
    print("=" * 130)
    best = results[0]
    for lo, hi, name in [(9, 17, "morning 09-17"), (17, 23, "evening 17-23"), (9, 23, "full day")]:
        t = backtest(df, ema_period=best["ep"], trail_trigger_r=best["trig"],
                     trail_step_r=best["step"], session_start=lo, session_end=hi)
        stats(t, f"EMA={best['ep']} {name}")

    print()
    print("=" * 130)
    print(" BROKERAGE SENSITIVITY on best config")
    print("=" * 130)
    for brok in [0, 20, 40]:
        t = backtest(df, ema_period=best["ep"], trail_trigger_r=best["trig"],
                     trail_step_r=best["step"], brokerage_per_order=brok)
        stats(t, f"brokerage Rs{brok}/order")

    print()
    print("=" * 130)
    print(" TOP 5 on real MCX")
    print("=" * 130)
    for r in results[:5]:
        ann = r["net"] * (250 / sessions)
        print(f"  {r['label']:48s} net Rs{r['net']:+8,.0f} over {sessions} sessions "
              f"-> Rs{ann:+9,.0f}/yr per lot | PF {r['pf']:.2f} | maxDD Rs{r['dd']:+,.0f}")
