#!/usr/bin/env python
"""
Dr Devendra Smart Renko Engine -- red-bar entry with SWEPT exits. SENSEX / MIDCPNIFTY.

WHY THIS EXISTS, and what the evidence actually says (wiki/research/renko_pro_backtest.md):

  The Pine indicator ships NO tester and its author calls the strategy layer
  "not demonstrated an edge". Testing it here went through four stages:

  1. Shipped defaults, 5 indices x 5 timeframes -> rejected (sections 2-4).
  2. Entry sweep, 1,728 configs -> "no edge". 99% of configs were profitable
     in-sample, which made in-sample ranking uninformative (section 5).
  3. That verdict was WRONG. The port had hardcoded the Pine's own worst exit
     (T2 at the Renko structure: 34 of 996 targets ever filled, yet carried 97%
     of net points). With entries frozen and the EXIT surface swept instead --
     12,096 configs, selected on net RUPEES after friction so trade count is a
     cost -- the entry beats a null that randomises day, direction AND timing by
     z = +2.53, only 1 of 200 random books beat it, and cross-symbol transfer
     went 2/4 -> 4/4 (section 6).
  4. Real option premiums on Volrix broadly CONFIRM the index-point model on
     matched windows (NIFTY model -Rs 75 vs real -Rs 5,902; SENSEX model
     +Rs 6,383 vs real +Rs 25,129 -- the model was pessimistic). Measured
     realised |dOpt| per favourable index point is 0.52, not the 0.358 assumed
     (sections 7-8).

  WHAT IS STILL WRONG WITH IT, stated plainly because it governs sizing:
    - SENSEX is profitable in only 2 of 7 months on real premiums, and March
      2026 alone carries the entire result (+Rs 67,965 of +Rs 18,483 total).
      Strip March and it loses Rs 49,482.
    - Jun-Aug 2026, the most recent data, is negative on every index tested.
    - MIDCPNIFTY has NO real-premium verification at all: it is unsupported on
      Volrix, has no weekly options, and its corrected model edge is 1.94x its
      friction hurdle (down from an incorrectly-computed 4.36x) on ~Rs 20,328 of
      premium per lot.

  Hence SHADOW BY DEFAULT. This forward-tests on live quotes and books nothing.

CONFIG (the swept winner, 15m, selected on net rupees after friction)
    stop            previous candle low/high
    T1              2.5R, books HALF
    T2              3.0R  (the sweep's nominal 2.0R resolves to 3.0R because T2
                           is held strictly beyond T1)
    trailing        none  (tested, rejected -- it capped the runners)
    max trades/day  2
    entry gates     red bar on a structural level (CPR / gap fib / institutional
                    zone / X candle / afternoon range, 8pt tolerance on the
                    BODY), long above the red bar high, short below its median,
                    filtered by X 44/56, the gap band and EMA30, blocked inside
                    the X band and inside CPR, and requiring >= 2R of room to
                    the Renko boundary (brick = 0.66% of spot).

PASS CONDITION, pre-registered so it cannot be softened later: profitable in a
MAJORITY of forward months. One month in seven is what the backtest already
gives and it is not enough. Every simulated round trip is appended to
log/strategies/renko_shadow_<UNDERLYING>.csv for exactly that count.
"""
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from openalgo import api

import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

api_key = os.getenv('OPENALGO_API_KEY')
host = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)
client = api(api_key=api_key, host=host)

STRATEGY_NAME = "Renko Engine"
UNDERLYING = os.getenv('UNDERLYING', 'SENSEX').upper()

# SENSEX trades on BFO off the BSE index; MIDCPNIFTY on NFO off the NSE index.
EXCH = {"SENSEX": ("BSE_INDEX", "BFO"), "MIDCPNIFTY": ("NSE_INDEX", "NFO"),
        "NIFTY": ("NSE_INDEX", "NFO"), "BANKNIFTY": ("NSE_INDEX", "NFO")}
if UNDERLYING not in EXCH:
    log.error("UNDERLYING %s not supported", UNDERLYING)
    sys.exit(1)
IDX_EXCHANGE, OPT_EXCHANGE = EXCH[UNDERLYING]

# ---- swept exit config; changing these invalidates the evidence above -------
TIMEFRAME_MIN = int(os.getenv('TIMEFRAME_MIN', '15'))
BRICK_PCT = float(os.getenv('BRICK_PCT', '0.66'))
LEVEL_TOL = float(os.getenv('LEVEL_TOL', '8.0'))
EMA_SLOW_LEN = int(os.getenv('EMA_SLOW_LEN', '30'))
T1_RR = float(os.getenv('T1_RR', '2.5'))
T2_RR = float(os.getenv('T2_RR', '3.0'))
MIN_ROOM_R = float(os.getenv('MIN_ROOM_R', '2.0'))
MAX_TRADES_DAY = int(os.getenv('MAX_TRADES_DAY', '2'))
INST_BARS = int(os.getenv('INST_BARS', '3'))
SL_FALLBACK_PTS = float(os.getenv('SL_FALLBACK_PTS', '30.0'))
QUANTITY = int(os.getenv('QUANTITY', '0'))          # 0 = detect the contract lot
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
OPT_COST_PCT = float(os.getenv('OPT_COST_PCT', '0.12'))   # statutory, each side
STRIKE_OFFSET = os.getenv('STRIKE_OFFSET', 'ATM')
ENTRY_END = os.getenv('ENTRY_END', '15:00')
EOD_EXIT = os.getenv('EOD_EXIT', '15:15')
POLL_SECS = float(os.getenv('POLL_SECS', '20'))

# SHADOW BY DEFAULT -- inverted from the other strategies on purpose. SENSEX
# carries its whole backtest result in one month and MIDCPNIFTY has no
# real-premium evidence at all, so live is opt-IN here, not opt-out.
STRATEGY_LABEL = os.getenv('STRATEGY_NAME', '')
DRY_RUN = (os.getenv('DRY_RUN', 'true').lower() == 'true'
           or 'shadow' in STRATEGY_LABEL.lower())

# INTRADAY ONLY -- not configurable, and deliberately not read from the
# environment. The whole evidence base is intraday: every backtest exits at the
# session close, so an overnight carry would be an untested strategy wearing a
# tested one's numbers. MIS also makes the broker enforce it independently of
# this code. There is no NRML path here on purpose.
PRODUCT = "MIS"
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"renko_engine_{UNDERLYING}{'_shadow' if DRY_RUN else ''}.json"
SHADOW_CSV = Path("log") / "strategies" / f"renko_shadow_{UNDERLYING}.csv"

_shutdown = False


def _sigterm(signum, _frame):
    """The platform stops strategies with SIGTERM at schedule_stop."""
    global _shutdown
    _shutdown = True
    log.info("SIGTERM received -- finishing the current cycle and exiting.")


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


# ------------------------------------------------------------------ helpers
def get_nearest_expiry():
    """Nearest expiry. For MIDCPNIFTY this is necessarily the MONTHLY -- the
    index has no weeklies, which is why its economics differ (premium measured
    at 1.469% of spot vs 0.45% weekly, so ~3x the friction hurdle)."""
    try:
        r = client.expiry(symbol=UNDERLYING, exchange=OPT_EXCHANGE, instrumenttype="options")
        if r.get("status") == "success" and r.get("data"):
            return r["data"][0].replace("-", "")
    except Exception as e:
        log.error("expiry lookup failed: %s", e)
    return None


def get_option_symbol(expiry, option_type):
    try:
        r = client.optionsymbol(underlying=UNDERLYING, exchange=OPT_EXCHANGE,
                               expiry_date=expiry, offset=STRIKE_OFFSET,
                               option_type=option_type)
        if r.get("status") == "success":
            return r.get("symbol")
    except Exception as e:
        log.error("optionsymbol failed: %s", e)
    return None


def fetch_lot_size():
    """Contract lot size from TWO independent sources, or None. Never a guess.

    On 2026-08-12 optionchain returned 404 all session on both indices while the
    master held the rows, detection fell through to a hardcoded 75, and every
    order was rejected for wrong multiples. optionsymbol() answered correctly
    throughout and is the second source.
    """
    exp = get_nearest_expiry()
    if exp:
        try:
            r = client.optionchain(underlying=UNDERLYING, exchange=IDX_EXCHANGE,
                                   expiry_date=exp, strike_count=1)
            if r.get("status") == "success":
                for item in r.get("chain", []):
                    for side in ("ce", "pe"):
                        d = item.get(side) or {}
                        if d.get("lotsize"):
                            return int(d["lotsize"])
        except Exception as e:
            log.warning("optionchain lot-size lookup raised: %s", e)
        try:
            r = client.optionsymbol(underlying=UNDERLYING, exchange=OPT_EXCHANGE,
                                    expiry_date=exp, offset="ATM", option_type="CE")
            if r.get("lotsize"):
                return int(r["lotsize"])
        except Exception as e:
            log.warning("optionsymbol lot-size lookup raised: %s", e)
    return None


def fetch_option_ltp(opt_symbol, spot=None, retries=3):
    """Option LTP, sanity-checked against spot. Brokers can leak the spot value
    into an option quote before the tick cache is warm."""
    for i in range(retries):
        try:
            q = client.quotes(symbol=opt_symbol, exchange=OPT_EXCHANGE)
            if q.get("status") == "success":
                ltp = float(q["data"]["ltp"])
                if ltp > 0 and (spot is None or ltp < spot * 0.2):
                    return ltp
        except Exception as e:
            log.debug("option ltp %s attempt %s: %s", opt_symbol, i + 1, e)
        time.sleep(0.8)
    log.warning("no usable LTP for %s", opt_symbol)
    return None


def statutory_cost(entry_px, exit_px, qty):
    """Round-trip statutory cost in rupees, premium-based, both sides."""
    if entry_px is None or exit_px is None:
        return 0.0
    return (float(entry_px) + float(exit_px)) * float(qty) * OPT_COST_PCT / 100.0


def fetch_15m():
    """15m OHLC built by resampling 1m.

    Deliberately NOT client.history(interval='15m'): the broker's range endpoint
    returned range-corrupted index candles during the Red Bar work, and 1m ->
    resample is what the validated offline port does (5m -> 15m, origin 09:15).
    """
    end = date.today()
    start = end - timedelta(days=6)
    try:
        df = client.history(symbol=UNDERLYING, exchange=IDX_EXCHANGE, interval="1m",
                            start_date=start.strftime("%Y-%m-%d"),
                            end_date=end.strftime("%Y-%m-%d"))
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")
        # Drop everything outside the cash session BEFORE resampling. The 1m
        # feed carries a pre-open artefact -- observed 2026-08-20 on SENSEX, a
        # flat 09:00 candle at 77468.45 (o=h=l=c) -- which resamples into its own
        # 15m bucket and becomes bar[0] of the day. That made the X candle
        # DEGENERATE: x_high == x_low, so x_44 == x_56, which silently disables
        # the X-band zone block and makes the `close > x_56` filter trivial.
        # The validated offline port never hit this because its 5m cache starts
        # at 09:15.
        m = df.index.hour * 60 + df.index.minute
        df = df[(m >= 555) & (m < 930)]        # 09:15 .. 15:30
        if df.empty:
            return pd.DataFrame()
        base = df.index[0].normalize() + pd.Timedelta("9h15m")
        out = (df.resample(f"{TIMEFRAME_MIN}min", origin=base, label="left", closed="left")
                 .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
                 .dropna())
        return out
    except Exception as e:
        log.error("history fetch failed: %s", e)
        return pd.DataFrame()


def prior_day_levels(df):
    """CPR from the prior session's H/L/C, and that session's closing bars as
    today's institutional zone."""
    if df.empty:
        return {}
    days = sorted({ts.date() for ts in df.index})
    if len(days) < 2:
        return {}
    today, prev = days[-1], days[-2]
    p = df[[ts.date() == prev for ts in df.index]]
    if p.empty:
        return {}
    pdh, pdl, pdc = float(p["high"].max()), float(p["low"].min()), float(p["close"].iloc[-1])
    cpp = (pdh + pdl + pdc) / 3.0
    bc = (pdh + pdl) / 2.0
    tc = 2.0 * cpp - bc
    tail = p.tail(INST_BARS)
    return {"today": today, "pdh": pdh, "pdl": pdl, "pdc": pdc, "cpp": cpp,
            "cpr_hi": max(tc, bc), "cpr_lo": min(tc, bc),
            "inst_hi": float(tail["high"].max()), "inst_lo": float(tail["low"].min())}


class Renko:
    """Sequential Renko: anchors to the last completed brick and steps in whole
    bricks. A floor() lattice would rewrite every level whenever the brick
    drifts with price."""

    def __init__(self):
        self.base = None

    def update(self, close):
        brick = max(close * BRICK_PCT / 100.0, 0.05)
        if self.base is None:
            self.base = close
        elif close >= self.base + brick:
            self.base += int((close - self.base) / brick) * brick
        elif close <= self.base - brick:
            self.base -= int((self.base - close) / brick) * brick
        return self.base - brick, self.base + brick, brick


def touches(level, lo, hi):
    return level is not None and (lo - LEVEL_TOL) <= level <= (hi + LEVEL_TOL)


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def append_shadow(row):
    """One CSV row per simulated round trip -- the record the pass condition
    (majority of forward months profitable) is counted from."""
    try:
        new = not SHADOW_CSV.exists()
        with SHADOW_CSV.open("a", encoding="utf-8") as f:
            if new:
                f.write("date,underlying,side,symbol,qty,entry_spot,exit_spot,"
                        "entry_prem,exit_prem,leg,reason,gross,cost,net\n")
            f.write(",".join(str(row.get(k, "")) for k in (
                "date", "underlying", "side", "symbol", "qty", "entry_spot",
                "exit_spot", "entry_prem", "exit_prem", "leg", "reason",
                "gross", "cost", "net")) + "\n")
    except Exception as e:
        log.debug("shadow csv append failed: %s", e)


def main():
    log.info("=" * 62)
    log.info("Renko Engine | %s | %s | %sm", UNDERLYING, OPT_EXCHANGE, TIMEFRAME_MIN)
    log.info("stop=prev candle | T1 %.1fR books half | T2 %.1fR | max %d/day",
             T1_RR, T2_RR, MAX_TRADES_DAY)
    if DRY_RUN:
        log.warning("SHADOW MODE -- no orders, no capital. P&L is simulated from "
                    "LIVE option quotes and appended to %s", SHADOW_CSV.name)
    else:
        log.warning("LIVE MODE -- real orders. SENSEX carries its whole backtest "
                    "result in ONE month and MIDCPNIFTY has no real-premium "
                    "verification. This was opt-in.")
    log.info("=" * 62)

    lot = QUANTITY or fetch_lot_size()
    if not lot or lot <= 0:
        log.error("lot size unavailable for %s -- standing down rather than "
                  "guessing a size (2026-08-12: a hardcoded guess got every "
                  "order rejected all session). Set QUANTITY to override.",
                  UNDERLYING)
        sys.exit(1)
    qty_total = lot * MAX_LOTS
    log.info("lot=%d lots=%d qty=%d", lot, MAX_LOTS, qty_total)

    entry_end, eod = hhmm(ENTRY_END), hhmm(EOD_EXIT)
    trade_day = None
    renko = Renko()
    day = {}
    pos = None
    trades_today = 0
    last_bar_ts = None
    prev_close = prev_red_hi = prev_red_med = None

    while not _shutdown:
        now = datetime.now()
        mins = now.hour * 60 + now.minute

        # Outside the session, idle instead of polling. A standalone shadow
        # instance runs continuously across days, and a 20s poll overnight would
        # burn ~2,000 needless history calls against a rate limit shared with
        # the LIVE strategies on this instance.
        #
        # `pos is None` is load-bearing: an open position must ALWAYS fall
        # through to the hard square-off below, or this guard would itself
        # become the overnight-carry bug it is unrelated to.
        if pos is None and (not (555 <= mins <= 935) or now.weekday() >= 5):
            time.sleep(300)
            continue
        if trade_day != date.today():
            trade_day = date.today()
            day = {}
            renko = Renko()
            pos = None
            trades_today = 0
            last_bar_ts = None
            prev_close = prev_red_hi = prev_red_med = None
            log.info("--- new session %s ---", trade_day)

        # ---- HARD intraday square-off, checked EVERY poll ------------------
        # Deliberately ABOVE the once-per-bar gate below. The first version had
        # the EOD check inside the per-bar block, so a stalled or late feed after
        # 15:15 meant no bar arrived, no check ran, and the position would have
        # been carried overnight. This strategy is intraday-only by design: MIS
        # product, no NRML path, and the exit does not depend on a candle
        # appearing.
        if pos is not None and mins >= eod:
            spot_now = None
            try:
                q = client.quotes(symbol=UNDERLYING, exchange=IDX_EXCHANGE)
                if q.get("status") == "success":
                    spot_now = float(q["data"]["ltp"])
            except Exception as e:
                log.debug("eod spot fetch: %s", e)
            exit_prem = fetch_option_ltp(pos["symbol"], spot=spot_now)
            q_out = int(pos["qty"])
            gross = ((exit_prem - pos["entry_prem"]) * q_out) if exit_prem else 0.0
            cost = statutory_cost(pos["entry_prem"], exit_prem, q_out)
            log.info("%s EOD | prem %.2f -> %.2f | qty %d | NET %+.0f",
                     "[SHADOW]" if DRY_RUN else "EXIT", pos["entry_prem"],
                     exit_prem or 0.0, q_out, gross - cost)
            append_shadow({"date": trade_day, "underlying": UNDERLYING,
                           "side": pos["side"], "symbol": pos["symbol"],
                           "qty": q_out,
                           "entry_spot": round(pos["entry_spot"], 2),
                           "exit_spot": round(spot_now, 2) if spot_now else "",
                           "entry_prem": pos["entry_prem"], "exit_prem": exit_prem,
                           "leg": "rest" if pos["t1_done"] else "full",
                           "reason": "EOD", "gross": round(gross, 2),
                           "cost": round(cost, 2), "net": round(gross - cost, 2)})
            if not DRY_RUN:
                client.placeorder(strategy=STRATEGY_NAME, symbol=pos["symbol"],
                                  action="SELL", exchange=OPT_EXCHANGE,
                                  price_type="MARKET", product=PRODUCT,
                                  quantity=q_out)
            pos = None

        df = fetch_15m()
        if df.empty or len(df) < 2:
            time.sleep(POLL_SECS)
            continue
        today_bars = df[[ts.date() == trade_day for ts in df.index]]
        if today_bars.empty:
            time.sleep(POLL_SECS)
            continue

        # act once per completed bar
        bar_ts = today_bars.index[-1]
        if bar_ts == last_bar_ts:
            time.sleep(POLL_SECS)
            continue
        last_bar_ts = bar_ts

        if not day:
            day = prior_day_levels(df)
            if not day:
                log.warning("no prior-day levels yet; waiting")
                time.sleep(POLL_SECS)
                continue
            first = today_bars.iloc[0]
            rng = float(first["high"]) - float(first["low"])
            day.update({"x_hi": float(first["high"]), "x_lo": float(first["low"]),
                        "x_44": float(first["low"]) + 0.44 * rng,
                        "x_56": float(first["low"]) + 0.56 * rng,
                        "day_open": float(first["open"]),
                        "aft_hi": None, "aft_lo": None, "aft_44": None,
                        "aft_56": None, "red_hi": None, "red_lo": None,
                        "red_med": None, "red_ok": False, "red_used": False})
            log.info("levels | X %.1f-%.1f (44 %.1f / 56 %.1f) | CPR %.1f-%.1f | inst %.1f-%.1f",
                     day["x_lo"], day["x_hi"], day["x_44"], day["x_56"],
                     day["cpr_lo"], day["cpr_hi"], day["inst_lo"], day["inst_hi"])

        bar = today_bars.iloc[-1]
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        bar_min = bar_ts.hour * 60 + bar_ts.minute
        r_floor, r_ceil, brick = renko.update(c)
        ema_slow = float(df["close"].ewm(span=EMA_SLOW_LEN, adjust=False).mean().iloc[-1])

        # afternoon range 12:45-13:15
        if 765 <= bar_min < 795:
            day["aft_hi"] = h if day["aft_hi"] is None else max(day["aft_hi"], h)
            day["aft_lo"] = l if day["aft_lo"] is None else min(day["aft_lo"], l)
        elif bar_min >= 795 and day["aft_44"] is None and day["aft_hi"] is not None:
            rng = day["aft_hi"] - day["aft_lo"]
            day["aft_44"] = day["aft_lo"] + 0.44 * rng
            day["aft_56"] = day["aft_lo"] + 0.56 * rng

        # ---------------- manage the open position on THIS bar ----------------
        if pos is not None:
            hit = None
            if pos["side"] == "long":
                if l <= pos["sl"]:
                    hit = "SL"
                elif not pos["t1_done"] and h >= pos["t1"]:
                    hit = "T1"
                elif pos["t1_done"] and h >= pos["t2"]:
                    hit = "T2"
            else:
                if h >= pos["sl"]:
                    hit = "SL"
                elif not pos["t1_done"] and l <= pos["t1"]:
                    hit = "T1"
                elif pos["t1_done"] and l <= pos["t2"]:
                    hit = "T2"
            if hit is None and mins >= eod:
                hit = "EOD"

            if hit:
                exit_prem = fetch_option_ltp(pos["symbol"], spot=c)
                part = 0.5 if hit == "T1" else (0.5 if pos["t1_done"] else 1.0)
                q = int(pos["qty"] * part)
                gross = ((exit_prem - pos["entry_prem"]) * q) if exit_prem else 0.0
                cost = statutory_cost(pos["entry_prem"], exit_prem, q)
                net = gross - cost
                log.info("%s %s | spot %.1f | prem %.2f -> %.2f | qty %d | "
                         "gross %+.0f cost %.0f NET %+.0f",
                         "[SHADOW]" if DRY_RUN else "EXIT", hit, c,
                         pos["entry_prem"], exit_prem or 0.0, q, gross, cost, net)
                append_shadow({"date": trade_day, "underlying": UNDERLYING,
                               "side": pos["side"], "symbol": pos["symbol"],
                               "qty": q, "entry_spot": round(pos["entry_spot"], 2),
                               "exit_spot": round(c, 2),
                               "entry_prem": pos["entry_prem"],
                               "exit_prem": exit_prem, "leg": "half" if hit == "T1" else "rest",
                               "reason": hit, "gross": round(gross, 2),
                               "cost": round(cost, 2), "net": round(net, 2)})
                if not DRY_RUN:
                    client.placeorder(strategy=STRATEGY_NAME, symbol=pos["symbol"],
                                      action="SELL", exchange=OPT_EXCHANGE,
                                      price_type="MARKET", product=PRODUCT, quantity=q)
                if hit == "T1":
                    pos["t1_done"] = True
                    pos["qty"] -= q
                else:
                    pos = None
                    continue

        # ---------------- red bar bookkeeping ----------------
        cur_hi, cur_med = day["red_hi"], day["red_med"]
        if c < o and len(today_bars) > 1:
            b_lo, b_hi = min(o, c), max(o, c)
            conf = any(touches(x, b_lo, b_hi) for x in (
                day["cpp"], day["cpr_hi"], day["cpr_lo"], day["inst_hi"],
                day["inst_lo"], day["x_hi"], day["x_lo"],
                (day["x_hi"] + day["x_lo"]) / 2.0, day["x_44"], day["x_56"],
                day["aft_44"], day["aft_56"]))
            gap = abs(day["day_open"] - day["pdc"])
            if gap > 0:
                sgn = 1.0 if day["day_open"] < day["pdc"] else -1.0
                conf = conf or any(touches(day["day_open"] + sgn * f * gap, b_lo, b_hi)
                                   for f in (0.44, 0.50, 0.56))
            day.update({"red_hi": h, "red_lo": l, "red_med": (h + l) / 2.0,
                        "red_ok": conf, "red_used": False})
            cur_hi, cur_med = h, (h + l) / 2.0

        long_trig = short_trig = False
        if prev_close is not None:
            if prev_red_hi is not None and cur_hi is not None:
                long_trig = prev_close <= prev_red_hi and c > cur_hi
            if prev_red_med is not None and cur_med is not None:
                short_trig = prev_close >= prev_red_med and c < cur_med
        prev_close, prev_red_hi, prev_red_med = c, cur_hi, cur_med

        if (pos is not None or trades_today >= MAX_TRADES_DAY or bar_min >= entry_end
                or day["red_used"] or not day["red_ok"] or not (long_trig or short_trig)):
            log.info("Regime | spot %.1f | renko %.1f-%.1f | red %s | trig %s/%s | trades %d/%d",
                     c, r_floor, r_ceil, "ok" if day["red_ok"] else "-",
                     int(long_trig), int(short_trig), trades_today, MAX_TRADES_DAY)
            time.sleep(POLL_SECS)
            continue

        # zone blocks: never inside the X buffer band, never inside CPR
        if day["x_44"] <= c <= day["x_56"] or day["cpr_lo"] <= c <= day["cpr_hi"]:
            time.sleep(POLL_SECS)
            continue

        prev_bar = today_bars.iloc[-2]
        side = sl = risk = None
        if long_trig and c > day["x_56"] and c > ema_slow:
            sl = float(prev_bar["low"]) if float(prev_bar["low"]) < c else c - SL_FALLBACK_PTS
            risk = c - sl
            tgt = r_ceil + brick if r_ceil < c + T1_RR * risk else r_ceil
            if risk > 0 and (tgt - c) >= MIN_ROOM_R * risk:
                side = "long"
        if side is None and short_trig and c < day["x_44"] and c < ema_slow:
            sl = float(prev_bar["high"]) if float(prev_bar["high"]) > c else c + SL_FALLBACK_PTS
            risk = sl - c
            tgt = r_floor - brick if r_floor > c - T1_RR * risk else r_floor
            if risk > 0 and (c - tgt) >= MIN_ROOM_R * risk:
                side = "short"
        if side is None:
            time.sleep(POLL_SECS)
            continue

        exp = get_nearest_expiry()
        opt_type = "CE" if side == "long" else "PE"
        symbol = get_option_symbol(exp, opt_type) if exp else None
        if not symbol:
            log.warning("could not resolve %s option symbol; skipping signal", opt_type)
            time.sleep(POLL_SECS)
            continue
        entry_prem = fetch_option_ltp(symbol, spot=c)
        if entry_prem is None:
            log.warning("no entry premium for %s; skipping signal", symbol)
            time.sleep(POLL_SECS)
            continue

        sgn = 1.0 if side == "long" else -1.0
        pos = {"side": side, "symbol": symbol, "qty": qty_total,
               "entry_spot": c, "entry_prem": entry_prem, "sl": sl,
               "t1": c + sgn * risk * T1_RR, "t2": c + sgn * risk * T2_RR,
               "t1_done": False}
        day["red_used"] = True
        trades_today += 1
        log.info("%s %s %s | spot %.1f | SL %.1f T1 %.1f T2 %.1f | prem %.2f qty %d",
                 "[SHADOW] would BUY" if DRY_RUN else "BUY", opt_type, symbol,
                 c, sl, pos["t1"], pos["t2"], entry_prem, qty_total)
        if not DRY_RUN:
            client.placeorder(strategy=STRATEGY_NAME, symbol=symbol, action="BUY",
                              exchange=OPT_EXCHANGE, price_type="MARKET",
                              product=PRODUCT, quantity=qty_total)
        time.sleep(POLL_SECS)

    # SIGTERM: a shadow instance holds nothing; a live one must not be left open.
    if pos is not None and not DRY_RUN:
        log.warning("shutdown with an open position on %s -- closing", pos["symbol"])
        try:
            client.placeorder(strategy=STRATEGY_NAME, symbol=pos["symbol"],
                              action="SELL", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT,
                              quantity=pos["qty"])
        except Exception as e:
            log.error("shutdown close failed: %s", e)
    log.info("Shutdown complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
