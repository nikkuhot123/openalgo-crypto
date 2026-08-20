"""
Dr Devendra Smart Renko Engine PRO -- faithful port of the Pine, honestly tested.
================================================================================
Ported construct-by-construct from Doctor_Diven_Smart_Renko_Engine_Pro_Combined.pine
so the result is a test OF THAT STRATEGY and not of something adjacent to it:

  Pine                                    here
  ----------------------------------      -----------------------------------
  sequential renko, brick = close*0.66%    RenkoState.update()
  X candle = first bar of session          day.x_high / x_low / 44 / 50 / 56
  CPR from prior D H/L/C                   cpp / tc / bc
  gap fibs 44/50/56 off day open           gap_near / gap_mid / gap_far
  institutional zone = prior session's     inst_high / inst_low
    last inst_bars_count bars
  EMA cloud 10 / 30                        ema_fast / ema_slow
  afternoon 12:45-13:15 -> 44% / 56%       aft_44 / aft_56
  red bar + confluence (body, 8pt tol)     qualified_red()
  long  = crossover(close, red_high)       trigger logic
  short = crossunder(close, red_median)
  filters X / gap / EMA, zone_ok           long_filter / short_filter
  room >= 2R to the renko target           room_ok()
  one-shot levels, 3/day, 6-bar cooldown   day counters
  SL prev candle low/high, T1 1.5R (50%),  position management
    T2 = renko structure, EOD flat

WHAT THE PINE ITSELF SAYS, and it is worth repeating before reading any number:
    "The strategy layer here is my mechanical reading of the lectures, not part
     of the product... has not demonstrated an edge."
`trade_backtest` ships OFF. So this run is a test of the interpretation layer.

FIDELITY NOTES that change results, all resolved conservatively:
  - process_orders_on_close: entry fills at the signal bar's close, and Pine
    only has position_size on the NEXT bar, so stops go live from the bar after
    entry. The entry bar is unprotected in the Pine too.
  - if a bar touches both stop and target, the STOP is taken first. Intrabar
    order is unknowable from OHLC and the optimistic choice is how backtests
    manufacture edges that do not exist.
  - the X candle is the first bar of the session, so its width depends on the
    timeframe. Run 5m/15m/30m -- if the edge only exists on one, it is noise.

Index points are the primary unit, matching the Pine. Because the live system
buys OPTIONS, results are also translated through the measured delta and real
friction from the earlier Red Bar work (DELTA 0.358, 0.12% x2 statutory,
0.41% spread) -- that translation is where prior index-point "edges" died.

Usage:
    ./venv/Scripts/python.exe backtesting/renko_engine/renko_engine_backtest.py
    ./venv/Scripts/python.exe backtesting/renko_engine/renko_engine_backtest.py --symbol BANKNIFTY --tf 15
"""
import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BACKTEST_CAPITAL  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "backtesting" / "data" / "market_cache.duckdb"

# ---- Pine inputs, same defaults -------------------------------------------
PCT_INDEX = 0.66
PCT_STOCK = 2.00
LEVEL_TOL = 8.0
CONFLUENCE_BODY = True
REQUIRE_CONFLUENCE = True
CONF_USE = {"cpr": True, "gap": True, "inst": True, "x": True, "aft": True,
            "renko": False, "ema": False}
REQUIRE_ROOM = True
MIN_TARGET_R = 2.0
ONE_SHOT = True
MAX_TRADES_DAY = 3
COOLDOWN_BARS = 6
SKIP_X_BAND = True
SKIP_CPR = True
EMA_FAST, EMA_SLOW = 10, 30
INST_BARS = 3
SL_POINTS = 30.0
T1_RR = 1.5
FILTER_X = True
FILTER_GAP = True
FILTER_EMA = True
TRADE_AFTERNOON = True
QTY = 2                      # Pine default_qty_value

# ---- exit engine, sweepable (the Pine's "Trade Management" group) ----------
# The original port hardcoded the two the Pine ships with -- SL at the previous
# candle and T2 at the Renko structure. The Pine's OWN ranking says that target
# is the worst of the six it offers (T2 fill 5.8%, expectancy -0.41R) and ATR
# the best (23.9%, -0.17R), and the 2026-08-19 sweep confirmed the symptom:
# only 34 of 996 T2s ever filled, yet they carried 97% of net points. So the
# exits are swept here rather than assumed.
#
# Defaults reproduce the previously published runs EXACTLY (regression-checked
# at 435/746/1407 trades on 30m/15m/5m); nothing below changes a shipped number.
SL_TYPE = "prev_candle"        # prev_candle | red_bar_opposite | fixed | atr
SL_ATR_MULT = 1.5
TARGET_MODE = "renko"          # renko | atr | fixed_rr | next_level | next_level_capped | eod
T2_ATR_MULT = 1.5
T2_RR = 4.0
T2_CAP_R = 4.0
T1_ENABLE = True               # Pine books 50% at T1
TRAIL_MODE = "none"            # none | breakeven_1r | atr | ema
TRAIL_ATR_MULT = 1.5
ATR_LEN = 14

# ---- measured option translation (backtesting/haema_signal/redbar_*) -------
DELTA = 0.358
OPT_COST_PCT = 0.12
SPREAD_PCT = 0.41
PREMIUM_PCT = 0.45        # ATM weekly premium as % of index, measured 2026-08-07
# Verified against the live symbol master 2026-08-19. FINNIFTY was wrong here
# (65); the exchange lot is 60, which overstated every FINNIFTY rupee figure by
# ~8%. It changes no verdict -- FINNIFTY sits below its friction hurdle either
# way -- but the numbers were wrong and are now right.
LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 60, "MIDCPNIFTY": 120}


def load_bars(symbol, tf_min):
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute(
        "SELECT to_timestamp(timestamp)::TIMESTAMP ts, open, high, low, close "
        "FROM market_data WHERE symbol=? AND interval='5m' ORDER BY timestamp",
        [symbol],
    ).fetchdf()
    con.close()
    df = df.set_index(pd.DatetimeIndex(df.pop("ts")))
    if tf_min != 5:
        df = (df.resample(f"{tf_min}min", origin=df.index[0].normalize() + pd.Timedelta("9h15m"),
                          label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
                .dropna())
    df["day"] = df.index.normalize()
    df["mins"] = df.index.hour * 60 + df.index.minute
    return df


class Renko:
    """Sequential renko. Anchors to the last completed brick and steps in whole
    bricks, so a drifting brick size never rewrites history (Pine section 3)."""

    def __init__(self):
        self.base = None
        self.dir = 0

    def update(self, close, brick):
        if self.base is None:
            self.base = close
        elif close >= self.base + brick:
            self.base += np.floor((close - self.base) / brick) * brick
            self.dir = 1
        elif close <= self.base - brick:
            self.base -= np.floor((self.base - close) / brick) * brick
            self.dir = -1
        return self.base - brick, self.base + brick


@dataclass
class Day:
    x_high: float = np.nan
    x_low: float = np.nan
    x_44: float = np.nan
    x_56: float = np.nan
    pdh: float = np.nan
    pdl: float = np.nan
    pdc: float = np.nan
    day_open: float = np.nan
    inst_high: float = np.nan
    inst_low: float = np.nan
    aft_high: float = np.nan
    aft_low: float = np.nan
    aft_44: float = np.nan
    aft_56: float = np.nan
    aft_set: bool = False
    aft_used: bool = False
    red_high: float = np.nan
    red_low: float = np.nan
    red_median: float = np.nan
    red_qualified: bool = False
    red_used: bool = False
    trades: int = 0

    @property
    def cpp(self):
        return (self.pdh + self.pdl + self.pdc) / 3.0

    @property
    def cpr(self):
        bc = (self.pdh + self.pdl) / 2.0
        tc = 2.0 * self.cpp - bc
        return min(tc, bc), max(tc, bc)

    def gaps(self):
        if np.isnan(self.pdc) or np.isnan(self.day_open):
            return (np.nan,) * 3
        size = abs(self.day_open - self.pdc)
        if size <= 0:
            return (np.nan,) * 3
        d = 1.0 if self.day_open < self.pdc else -1.0
        return (self.day_open + d * 0.44 * size,
                self.day_open + d * 0.50 * size,
                self.day_open + d * 0.56 * size)


@dataclass
class Trade:
    day: pd.Timestamp
    entry_ts: pd.Timestamp
    side: str
    entry: float
    sl: float
    t1: float
    t2: float
    # Frozen copy of the ENTRY stop. `sl` is mutated by TRAIL_MODE, so R has to
    # be measured against this or a trailed stop silently redefines 1R and the
    # breakeven ratchet arms against a moving target.
    sl_orig: float = np.nan
    exit_ts: pd.Timestamp = None
    exit: float = np.nan
    reason: str = ""
    pts: float = 0.0
    legs: list = field(default_factory=list)


def touches(level, lo, hi):
    return (not np.isnan(level)) and (lo - LEVEL_TOL) <= level <= (hi + LEVEL_TOL)


def run(df, symbol, entry_override=None):
    """entry_override: {bar_index: 'long'|'short'} replaces the SIGNAL only.

    Everything downstream -- SL from the prior candle, T1 at T1_RR, the Renko
    T2, stop-before-target resolution, the per-day trade cap, the cooldown and
    the EOD flat -- runs through the identical code path. That is the point: a
    random-entry null built by reimplementing the exits would be measuring the
    reimplementation. Used by renko_sweep.py to ask whether the red-bar trigger
    beats random timing on the same EMA side, which is the claim the Pine's own
    header reports failing (z(sharpe) = +0.14).
    """
    is_index = symbol.upper() in LOT
    pct = PCT_INDEX if is_index else PCT_STOCK
    ema_f = df["close"].ewm(span=EMA_FAST, adjust=False).mean().values
    ema_s = df["close"].ewm(span=EMA_SLOW, adjust=False).mean().values

    # Wilder ATR (RMA), matching Pine's ta.atr -- ewm(alpha=1/len) is RMA.
    _pc = df["close"].shift(1)
    _tr = pd.concat([df["high"] - df["low"],
                     (df["high"] - _pc).abs(),
                     (df["low"] - _pc).abs()], axis=1).max(axis=1)
    atr = _tr.ewm(alpha=1.0 / ATR_LEN, adjust=False).mean().values

    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    ts, days, mins = df.index, df["day"].values, df["mins"].values
    n = len(df)

    renko = Renko()
    day = Day()
    trades, pos = [], None
    prev_close = prev_red_high = prev_red_med = np.nan
    prev_day_bars = []
    cooldown_until = -1
    last_was_loss = False
    day_start_idx = 0

    for i in range(n):
        new_day = i == 0 or days[i] != days[i - 1]
        if new_day:
            # previous session's closing bars -> today's institutional zone
            prev = prev_day_bars[-INST_BARS:] if prev_day_bars else []
            pd_h = max((b[1] for b in prev_day_bars), default=np.nan) if prev_day_bars else np.nan
            pd_l = min((b[2] for b in prev_day_bars), default=np.nan) if prev_day_bars else np.nan
            pd_c = prev_day_bars[-1][3] if prev_day_bars else np.nan
            day = Day(
                x_high=h[i], x_low=l[i], pdh=pd_h, pdl=pd_l, pdc=pd_c, day_open=o[i],
                inst_high=max((b[1] for b in prev), default=np.nan) if prev else np.nan,
                inst_low=min((b[2] for b in prev), default=np.nan) if prev else np.nan,
            )
            rng = day.x_high - day.x_low
            day.x_44, day.x_56 = day.x_low + 0.44 * rng, day.x_low + 0.56 * rng
            prev_day_bars = []
            day_start_idx = i
        prev_day_bars.append((o[i], h[i], l[i], c[i]))

        brick = max(c[i] * pct / 100.0, 0.05)
        r_floor, r_ceil = renko.update(c[i], brick)

        # afternoon window 12:45-13:15
        in_aft = 765 <= mins[i] < 795
        if in_aft:
            day.aft_high = h[i] if np.isnan(day.aft_high) else max(day.aft_high, h[i])
            day.aft_low = l[i] if np.isnan(day.aft_low) else min(day.aft_low, l[i])
        elif not day.aft_set and not np.isnan(day.aft_high) and mins[i] >= 795:
            rng = day.aft_high - day.aft_low
            day.aft_44, day.aft_56 = day.aft_low + 0.44 * rng, day.aft_low + 0.56 * rng
            day.aft_set = True

        is_last_of_day = (i + 1 >= n) or days[i + 1] != days[i]

        # ---------------- manage an open position (from the bar AFTER entry) --
        if pos is not None:
            sgn_p = 1.0 if pos.side == "long" else -1.0

            # ---- trailing stop, applied BEFORE the hit tests so a trail can
            # only ever be checked on a later bar than the one that set it ----
            if TRAIL_MODE != "none":
                new_sl = pos.sl
                if TRAIL_MODE == "breakeven_1r":
                    # Measured on Judas (2026-08-06, 25 live round trips): moving
                    # to entry once +1R is reached lifted mean outcome from
                    # +0.189R to +0.332R. Same idea, tested here rather than assumed.
                    r = abs(pos.entry - pos.sl_orig)
                    if r > 0 and sgn_p * (c[i] - pos.entry) >= r:
                        new_sl = pos.entry
                elif TRAIL_MODE == "atr":
                    new_sl = c[i] - sgn_p * atr[i] * TRAIL_ATR_MULT
                elif TRAIL_MODE == "ema":
                    new_sl = ema_s[i]
                # a trail may only ever tighten
                if sgn_p * (new_sl - pos.sl) > 0:
                    pos.sl = new_sl

            hit = None
            if pos.side == "long":
                if l[i] <= pos.sl:
                    hit, px = "SL", pos.sl                       # stop first, always
                elif h[i] >= pos.t1 and T1_ENABLE and not pos.legs:
                    pos.legs.append(("T1", pos.t1))
                if hit is None and (pos.legs or not T1_ENABLE) and h[i] >= pos.t2:
                    hit, px = "T2", pos.t2
            else:
                if h[i] >= pos.sl:
                    hit, px = "SL", pos.sl
                elif l[i] <= pos.t1 and T1_ENABLE and not pos.legs:
                    pos.legs.append(("T1", pos.t1))
                if hit is None and (pos.legs or not T1_ENABLE) and l[i] <= pos.t2:
                    hit, px = "T2", pos.t2
            if hit is None and is_last_of_day:
                hit, px = "EOD", c[i]
            if hit:
                sign = 1.0 if pos.side == "long" else -1.0
                booked = sum(sign * (p - pos.entry) for _, p in pos.legs) * 0.5
                pos.pts = booked + sign * (px - pos.entry) * (0.5 if pos.legs else 1.0)
                pos.exit_ts, pos.exit, pos.reason = ts[i], px, hit
                trades.append(pos)
                last_was_loss = pos.pts < 0
                cooldown_until = i + COOLDOWN_BARS if last_was_loss else -1
                pos = None

        # ---------------- red bar bookkeeping --------------------------------
        body_lo, body_hi = (min(o[i], c[i]), max(o[i], c[i])) if CONFLUENCE_BODY else (l[i], h[i])
        cpr_lo, cpr_hi = day.cpr
        g_near, g_mid, g_far = day.gaps()
        conf = (
            (CONF_USE["cpr"] and any(touches(x, body_lo, body_hi) for x in (day.cpp, cpr_lo, cpr_hi)))
            or (CONF_USE["gap"] and any(touches(x, body_lo, body_hi) for x in (g_near, g_mid, g_far)))
            or (CONF_USE["inst"] and any(touches(x, body_lo, body_hi) for x in (day.inst_high, day.inst_low)))
            or (CONF_USE["x"] and any(touches(x, body_lo, body_hi)
                                      for x in (day.x_high, day.x_low, (day.x_high + day.x_low) / 2, day.x_44, day.x_56)))
            or (CONF_USE["aft"] and any(touches(x, body_lo, body_hi) for x in (day.aft_44, day.aft_56)))
            or (CONF_USE["renko"] and any(touches(x, body_lo, body_hi) for x in (r_floor, r_ceil)))
            or (CONF_USE["ema"] and touches(ema_s[i], body_lo, body_hi))
        )
        cur_red_high, cur_red_med = day.red_high, day.red_median
        if c[i] < o[i] and not new_day:
            day.red_high, day.red_low = h[i], l[i]
            day.red_median = (h[i] + l[i]) / 2.0
            day.red_qualified = (not REQUIRE_CONFLUENCE) or conf
            day.red_used = False
            cur_red_high, cur_red_med = day.red_high, day.red_median

        # ---------------- triggers (Pine crossover semantics) ----------------
        x_long = (not np.isnan(prev_red_high) and not np.isnan(cur_red_high)
                  and prev_close <= prev_red_high and c[i] > cur_red_high)
        x_short = (not np.isnan(prev_red_med) and not np.isnan(cur_red_med)
                   and prev_close >= prev_red_med and c[i] < cur_red_med)
        red_ready = day.red_qualified and (not ONE_SHOT or not day.red_used)
        aft_ready = TRADE_AFTERNOON and day.aft_set and (not ONE_SHOT or not day.aft_used)
        aft_long = aft_ready and not np.isnan(day.aft_56) and prev_close <= day.aft_56 and c[i] > day.aft_56
        aft_short = aft_ready and not np.isnan(day.aft_44) and prev_close >= day.aft_44 and c[i] < day.aft_44
        long_trig = (x_long and red_ready) or aft_long
        short_trig = (x_short and red_ready) or aft_short

        # ---------------- filters --------------------------------------------
        lf = sf = True
        if FILTER_X and not np.isnan(day.x_56):
            lf &= c[i] > day.x_56
        if FILTER_X and not np.isnan(day.x_44):
            sf &= c[i] < day.x_44
        if FILTER_GAP and not np.isnan(g_far):
            if day.day_open < day.pdc:
                lf &= c[i] > g_far
            if day.day_open > day.pdc:
                sf &= c[i] < g_far
        if FILTER_EMA:
            lf &= c[i] > ema_s[i]
            sf &= c[i] < ema_s[i]
        in_xband = (not np.isnan(day.x_44)) and day.x_44 <= c[i] <= day.x_56
        in_cpr = (not np.isnan(cpr_lo)) and cpr_lo <= c[i] <= cpr_hi
        zone_ok = (not SKIP_X_BAND or not in_xband) and (not SKIP_CPR or not in_cpr)

        # ---------------- room to the structural target ----------------------
        sl_l = l[i - 1] if i > 0 else np.nan
        sl_s = h[i - 1] if i > 0 else np.nan
        risk_l = c[i] - sl_l if (not np.isnan(sl_l) and sl_l < c[i]) else SL_POINTS
        risk_s = sl_s - c[i] if (not np.isnan(sl_s) and sl_s > c[i]) else SL_POINTS
        tgt_l = r_ceil + brick if r_ceil < c[i] + T1_RR * risk_l else r_ceil
        tgt_s = r_floor - brick if r_floor > c[i] - T1_RR * risk_s else r_floor
        room_l = (not REQUIRE_ROOM) or (tgt_l - c[i]) >= MIN_TARGET_R * risk_l
        room_s = (not REQUIRE_ROOM) or (c[i] - tgt_s) >= MIN_TARGET_R * risk_s

        long_sig = long_trig and lf and zone_ok and room_l
        short_sig = short_trig and sf and zone_ok and room_s
        if long_sig or short_sig:
            day.red_used = True
            if aft_long or aft_short:
                day.aft_used = True

        # Null mode: the trigger is replaced, the management is not.
        if entry_override is not None:
            _ov = entry_override.get(i)
            long_sig = _ov == "long"
            short_sig = _ov == "short"

        # ---------------- entry ----------------------------------------------
        blocked = is_last_of_day or day.trades >= MAX_TRADES_DAY or (last_was_loss and i < cooldown_until)
        if pos is None and not blocked and (long_sig or short_sig):
            side = "long" if long_sig else "short"
            entry = c[i]
            sgn = 1.0 if side == "long" else -1.0

            # ---- stop ------------------------------------------------------
            if SL_TYPE == "prev_candle":
                raw_sl = sl_l if side == "long" else sl_s
            elif SL_TYPE == "red_bar_opposite":
                raw_sl = day.red_low if side == "long" else day.red_high
            elif SL_TYPE == "atr":
                raw_sl = entry - sgn * atr[i] * SL_ATR_MULT
            else:  # fixed
                raw_sl = entry - sgn * SL_POINTS
            # a stop on the wrong side of entry is not a stop
            if np.isnan(raw_sl) or sgn * (entry - raw_sl) <= 0:
                raw_sl = entry - sgn * SL_POINTS
            risk = abs(entry - raw_sl)
            t1 = entry + sgn * risk * T1_RR

            # ---- target ----------------------------------------------------
            struct = r_ceil if side == "long" else r_floor
            if sgn * (struct - t1) < 0:          # never inside T1
                struct += sgn * brick
            if TARGET_MODE == "renko":
                t2 = struct
            elif TARGET_MODE == "atr":
                t2 = entry + sgn * atr[i] * T2_ATR_MULT
            elif TARGET_MODE == "fixed_rr":
                t2 = entry + sgn * risk * T2_RR
            elif TARGET_MODE in ("next_level", "next_level_capped"):
                cands = [x for x in (day.cpp, cpr_lo, cpr_hi, g_far, day.inst_high,
                                     day.inst_low, day.x_high, day.x_low, day.x_44,
                                     day.x_56, day.aft_44, day.aft_56, r_floor, r_ceil)
                         if not np.isnan(x) and sgn * (x - t1) > 0]
                nl = (min(cands) if side == "long" else max(cands)) if cands else entry + sgn * risk * 3.0
                if TARGET_MODE == "next_level_capped":
                    cap = entry + sgn * risk * T2_CAP_R
                    nl = min(nl, cap) if side == "long" else max(nl, cap)
                t2 = nl
            else:  # eod -- no target at all, ride the session
                t2 = entry + sgn * 1e9
            if sgn * (t2 - t1) <= 0:             # keep T2 strictly beyond T1
                t2 = t1 + sgn * risk * 0.5
            pos = Trade(day=days[i], entry_ts=ts[i], side=side, entry=entry,
                        sl=raw_sl, t1=t1, t2=t2, sl_orig=raw_sl)
            day.trades += 1

        prev_close, prev_red_high, prev_red_med = c[i], cur_red_high, cur_red_med

    return pd.DataFrame([t.__dict__ for t in trades])


def report(t, symbol, tf, label=""):
    if t.empty:
        print(f"  {label or 'ALL':12s} no trades")
        return None
    pts = t["pts"]
    gw, gl = pts[pts > 0].sum(), -pts[pts < 0].sum()
    pf = gw / gl if gl > 0 else np.inf
    lot = LOT.get(symbol.upper(), 65)

    # Option translation: point value to Rupees via delta and real friction
    prem = t["entry"].mean() * PREMIUM_PCT / 100.0
    cost_rt = (2 * prem * lot) * OPT_COST_PCT / 100.0 + prem * SPREAD_PCT / 100.0 * lot
    rs_per_point = DELTA * lot
    
    # 1 lot trade P&L series in Rupees
    rs = pts * rs_per_point - cost_rt
    
    # Max Drawdown on 1 lot
    eq = rs.cumsum()
    dd_Rs = (eq.cummax() - eq).max()
    
    # Sized on standard Rs 2,00,000 research notional
    lots = max(int((BACKTEST_CAPITAL * 0.5) // (prem * lot)), 1)
    scaled_net = rs.sum() * lots
    scaled_dd = dd_Rs * lots
    dd_pct = (scaled_dd / BACKTEST_CAPITAL) * 100.0
    
    # Sharpe ratio (Trade)
    sharpe = (rs.mean() / rs.std() * np.sqrt(len(rs))) if len(rs) > 1 and rs.std() > 0 else 0.0
    
    print(f"  {label or 'ALL':12s} n={len(t):4d}  win={100*(pts>0).mean():4.1f}%  "
          f"PF={pf:4.2f}  net={scaled_net:+8,.0f}  "
          f"maxDD={scaled_dd:5,.0f} ({dd_pct:4.1f}%)  "
          f"Sharpe={sharpe:5.2f}  | {lots}lot ({scaled_net / BACKTEST_CAPITAL * 100:+.1f}% of 2L)")
          
    return {"n": len(t), "pf": pf, "pts": pts.sum(), "rs": rs.sum(),
            "avg": pts.mean(), "lots": lots}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--tf", type=int, default=0, help="0 = sweep 5/15/30")
    args = ap.parse_args()

    tfs = [args.tf] if args.tf else [5, 15, 30]
    print(f"Dr Devendra Smart Renko Engine PRO -- faithful port | {args.symbol}")
    print("The Pine ships trade_backtest=OFF and calls the strategy layer "
          "'not demonstrated an edge'.\nMeasuring that claim.\n")

    for tf in tfs:
        df = load_bars(args.symbol, tf)
        t = run(df, args.symbol)
        span = f"{df.index[0]:%Y-%m-%d}..{df.index[-1]:%Y-%m-%d}"
        print(f"--- {tf}m | {len(df):,} bars | {span}")
        if t.empty:
            print("  no trades\n")
            continue
        report(t, args.symbol, tf)
        # in-sample / out-of-sample at the midpoint of the calendar
        cut = t["day"].iloc[len(t) // 2]
        report(t[t["day"] < cut], args.symbol, tf, "IS")
        report(t[t["day"] >= cut], args.symbol, tf, "OOS")
        by = t.groupby(t["reason"])["pts"].agg(["count", "sum"])
        print("  exits: " + ", ".join(
            f"{k} {int(v['count'])}/{v['sum']:+.0f}" for k, v in by.iterrows()))
        t.to_csv(Path(__file__).parent / f"trades_{args.symbol}_{tf}m.csv", index=False)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
