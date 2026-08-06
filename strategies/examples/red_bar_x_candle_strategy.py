#!/usr/bin/env python
"""
Red Bar / X-Candle Strategy for NIFTY and SENSEX index options.

Mechanised from the Upsurge_Transcripts digest (Upsurge_Transcripts/Strategy_Digest.md),
merging Dr. Devendra's "Advanced Red Bar Theory" (Course 1) with the Aggressive
profile of the "Intraday Options Trading Masterclass" (Course 2, section 3.1).
Only the rules that are unambiguous and computable from OHLC are encoded; the
proprietary Renko indicators (Smart Renko Pro-Ed, Renko Super-Trend 10/2.1) are
NOT reproducible from public data and are deliberately left out.

RULES ENCODED (digest section in brackets)
  [2.1] The first 30 minutes (09:15-09:45) is the X-candle. Never traded, only
        measured: x_high, x_low.
  [2.2] Fib levels on the X range: L44 = low + 0.44*R, L50 = low + 0.50*R,
        L56 = low + 0.56*R. The +/-0.06 margin around the mean is the fake-entry
        filter -- entries use L56/L44, never bare L50.
  [2.4] No-trade day: if price never breaks x_high or x_low after the X candle,
        the day is sideways -> stand down.
  [2.4/3.1] Trigger = a RED 5-minute candle (close < open) that CLOSES:
        - above L56 -> the "negative candle closing above the mean" reversal
          trap -> BUY CE
        - below L44 -> confirmed negativity -> BUY PE
        Stop = that bar's low (CE) / high (PE) on SPOT. Target = RR x risk.
  [2.5] Gap gate: on a gap-down open beyond GAP_PCT, CE entries are blocked
        until spot reclaims 50% of the gap. Mirrored for gap-up / PE.
  [2.6] CPR confluence: CPP = (pH+pL+pC)/3, the other band edge = (pH+pL)/2.
        CE requires spot above the CPR band, PE requires spot below it.
  [2.7] EMA trend filter on the 5-min chart: EMA10 (always) and EMA30
        (REQUIRE_EMA30) must sit on the trade's side.
  [2.9] The 12:45 rule: from 13:15 the levels are re-anchored to the 12:45-13:15
        30-minute candle, so afternoon trades stop carrying the morning bias.
  [2.11] Two-lot management: with >= 2 lots, half is booked at T1 (T1_RR x risk)
        and the stop moves to breakeven; the rest runs to the full target.

The CPR and gap gates FAIL CLOSED: when the previous session's daily bar cannot
be fetched, the trade is refused rather than taken with reduced filtering.

EXITS, in priority order: broker-side premium stop (SL stop-limit on the option),
spot stop / spot target, max-hold timer, premium-decay floor, EOD squareoff.
Entries stop at 14:30; the day is flat by EXIT_TIME (15:10, see the CAS note).

Both indices run from this one script -- set UNDERLYING=NIFTY or UNDERLYING=SENSEX
(exchange, option exchange, lot size and expiry are all resolved from it). Run two
instances of it to trade both.

BACKTEST VERDICT -- DO NOT DEPLOY WITH REAL MONEY.
Backtested on Volrix (real weekly option premiums, 30m, ATM, 1 lot, 0.5% option
slippage + Rs 20/order, Rs 1.5L capital). In-sample = 2026-02-02..2026-06-05,
out-of-sample = 2026-06-08..2026-08-04 (a free-tier account caps history at six
months, so this is ~85 + ~40 sessions, not years):

    window            trades   net Rs    PF    Sharpe
    NIFTY  in-sample      70   +14,556   1.2     1.07
    SENSEX in-sample      72   +19,329   1.3     1.34
    NIFTY  OUT-OF-SAMPLE  35   -15,301   0.5    -3.81
    SENSEX OUT-OF-SAMPLE  36   -19,107   0.4    -5.40

Both indices invert out of sample. That is the signature of a curve fit on a
small sample, not an edge: the in-sample numbers are what tuning bought, and
they did not survive two months forward. The constants below are the ones that
topped the in-sample sweep, kept only so the result is reproducible.

CONFIRMED 2026-08-06 by an independent harness (backtesting/haema_signal/,
see red_bar_config_spec.md). Two regime gates -- skip Tuesday and skip
strong-uptrend days (5-day momentum ending yesterday < 0.0137) -- were fitted
on 2023-24 and do real work: over 2026-05-28..08-06, bars no gate or grid had
seen, they lift the result from -Rs 10,919 (PF 0.61, 47 trades) to -Rs 896
(PF 0.94, 28 trades). They subtract losers; they do not add winners. The
ungated forward loss independently reproduces the Volrix figures above.

What was ruled OUT as the cause: option pricing. Re-pricing 31 trades against
real 1-minute premiums (harvest DBs) and live greeks gives
real = 1.185 x delta_model (95% CI [0.936, 1.438], r 0.81) -- the spot-delta
model is a fair, slightly CONSERVATIVE proxy. Theta over a 90-minute hold is
~Rs 55/lot against ~Rs 116/lot of total friction. The strategy does not fail
on costs; it fails on direction.

Treat this file as a faithful, working ENCODING of the course -- not as a
validated system. Run it in analyzer mode.
"""
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pandas as pd
from openalgo import api

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

api_key = os.getenv('OPENALGO_API_KEY')
host    = os.getenv('HOST_SERVER') or os.getenv('OPENALGO_HOST', 'http://127.0.0.1:5000')
ws_url  = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')

if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=api_key, host=host, ws_url=ws_url)

# ---------------------------------------------------------------- parameters
STRATEGY_NAME = "Red Bar X-Candle"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY').upper()
PRODUCT = os.getenv('PRODUCT', 'MIS')
QUANTITY = int(os.getenv('QUANTITY', '0'))   # 0 = auto-detect lot size
# The platform always injects MAX_LOTS from max_lots_nifty/max_lots_sensex
# (blueprints/python_strategy.py), so default 1 like every sibling. The digest's
# [2.11] half-book needs >= 2 lots; when it is 1 the T1 mechanic is logged as off.
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_MODE = os.getenv('LOT_MODE', 'manual').lower()          # 'manual' | 'auto'
RISK_PCT_PER_TRADE = float(os.getenv('RISK_PCT_PER_TRADE', '1.0'))
LOT_SIZE = QUANTITY

_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}
_IS_BSE = UNDERLYING in _BSE_UNDERLYINGS


def _index_exchange(underlying: str) -> str:
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"


def _option_exchange(underlying: str) -> str:
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"


# Strike: the transcript prefers OTM for the aggressive profile (L218-222), but the
# backtest put ATM ahead of OTM1 (+2,795 vs +3,972 net on the NIFTY IS window).
STRIKE_OFFSET = os.getenv('STRIKE_OFFSET', 'ATM')
# The operative frame is the 30-MINUTE chart -- "तीस मिन्ट के chart पे ... 5 EMA on
# 30 minutes chart, it is mandatory" (aggressive lesson L61-69). Running the same
# rules on 5m triggers turns the stop into a 5m bar's low, which sits inside the
# index's noise: that encoding backtested at PF 0.60 / -Rs 24,238 with a 16-minute
# median hold, versus PF 1.10 / +Rs 3,972 on 30m. Do not lower this without evidence.
INTERVAL = os.getenv('INTERVAL', '30m')
SESSION_OPEN = dtime(int(os.getenv('SESSION_OPEN_HOUR', '9')), int(os.getenv('SESSION_OPEN_MIN', '15')))
X_END = dtime(int(os.getenv('X_END_HOUR', '9')), int(os.getenv('X_END_MIN', '45')))
REANCHOR_START = dtime(int(os.getenv('REANCHOR_HOUR', '12')), int(os.getenv('REANCHOR_MIN', '45')))
ENTRY_END = dtime(int(os.getenv('ENTRY_END_HOUR', '14')), int(os.getenv('ENTRY_END_MIN', '30')))
# CAS (SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/2765/2026, live 2026-08-03):
# cash continuous trading in F&O-underlying stocks ends 15:15, and the index spot
# then teleports on the ~15:28 auction stamp (NIFTY +200.95 pts in one tick,
# 2026-08-03). Every stop/target here is evaluated against SPOT, so the squareoff
# must complete while spot is still a continuous price. Options trade to 15:40, so
# a 15:10 market exit fills normally.
EXIT_TIME = dtime(*(int(x) for x in os.getenv('EXIT_TIME', '15:10').split(':')))

# The aggressive lesson plots "0, 1, 0.44, 0.5" (L87) and activates the buyer on a
# close above the 50% (L90-96); 0.56 is the other course's wider margin band.
FIB_HI = float(os.getenv('FIB_HI', '0.50'))
FIB_LO = float(os.getenv('FIB_LO', '0.44'))
RR = float(os.getenv('RR', '3.0'))            # digest 2.4/3.1: book 1:3, ride to 1:12
T1_RR = float(os.getenv('T1_RR', '1.0'))      # digest 2.11: half off at the first target
# Minimum stop distance as % of spot. The raw stop is the trigger bar's extreme,
# which on a quiet 5m bar sits inside the index's own noise (siblings measured
# 0.022% stops dying in 12-42 seconds). T1 and the target are measured off the
# SAME floored risk -- scaling the stop but not the target silently collapses the
# realised reward:risk (a 2-pt bar would otherwise target 6 pts behind a 25-pt stop).
MIN_SL_PCT = float(os.getenv('MIN_SL_PCT', '0.10'))
MAX_SL_PCT = float(os.getenv('MAX_SL_PCT', '0.60'))   # skip signals whose bar is too tall
GAP_PCT = float(os.getenv('GAP_PCT', '0.30'))         # gap size that arms the gap gate
# One 30m bar covers the whole 09:15-09:45 anchor; raise this only if INTERVAL is 5m.
MIN_ANCHOR_BARS = int(os.getenv('MIN_ANCHOR_BARS', '1'))
# Siblings converged on one entry per day after measuring re-entry chop; raise this
# only with evidence. LOSS_STREAK_LIMIT only bites when it is below this number.
MAX_TRADES_PER_DAY = int(os.getenv('MAX_TRADES_PER_DAY', '1'))

# Gate defaults are backtest-driven, not taste. On the NIFTY in-sample window the
# EMA-distance precondition cost money (PF 1.1 -> 0.8 at 0.3x range, 0.9 at 0.6x)
# and the CPR/gap confluences are from the other course, unmeasured here. All off
# by default; turn one on only with a run that shows it paying.
REQUIRE_EMA10 = os.getenv('REQUIRE_EMA10', 'false').lower() == 'true'
REQUIRE_EMA30 = os.getenv('REQUIRE_EMA30', 'false').lower() == 'true'
REQUIRE_CPR = os.getenv('REQUIRE_CPR', 'false').lower() == 'true'
REQUIRE_GAP_GATE = os.getenv('REQUIRE_GAP_GATE', 'false').lower() == 'true'
REANCHOR_1245 = os.getenv('REANCHOR_1245', 'true').lower() == 'true'

LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '2'))
DAILY_LOSS_LIMIT_RS = float(os.getenv('DAILY_LOSS_LIMIT_RS', '10000'))
# Option-premium stop, placed broker-side on the option itself. The spot stop cannot
# see premium bleed (sibling bug 2026-07-16: a CE fell 169->82, -51%, while spot held
# above its spot-stop) and an in-process stop dies with the process.
# TENSION: the backtest hates it. At 35% it fired 11 times for -Rs 23,261 at a 0%
# win rate, and removing it took the NIFTY IS window from +Rs 3,972 to +Rs 14,556
# (PF 1.1 -> 1.2); at 60% it still cost ~Rs 3.5k. It is kept as a DISASTER backstop
# only -- far enough away to not cut live trades, close enough to bound a gap or a
# dead process. Set PREMIUM_SL_PCT=95 to reproduce the backtested configuration.
PREMIUM_SL_PCT = float(os.getenv('PREMIUM_SL_PCT', '70'))
# MUST be SL (stop-limit), never SL-M: measured 2026-07-28 across the siblings'
# order logs, SL-M was rejected 33/33 times on NFO+BFO options while MARKET on the
# same symbols succeeded 114/114, and the rejections came back as
# {"status":"success","orderid":null} -- hence the orderid-must-exist check below.
SL_LIMIT_BUFFER_PCT = float(os.getenv('SL_LIMIT_BUFFER_PCT', '5'))
# Safety nets for a position whose broker SL was cancelled/orphaned by a restart
# (sibling evidence 2026-07-02: legs held 3+ hours lost 75-80% unattended).
MAX_HOLD_MINUTES = int(os.getenv('MAX_HOLD_MINUTES', '90'))
DECAY_EXIT_PCT = float(os.getenv('DECAY_EXIT_PCT', '0.60'))
# Round-trip statutory cost as % of option premium turnover (STT/exchange/GST/
# SEBI/stamp; brokerage is zero on several discount brokers). Subtracted from every
# booked P&L so the circuit breakers count net money.
OPT_COST_PCT = float(os.getenv('OPT_COST_PCT', '0.12'))
# NIFTY DTE-0 ATM premium is ~1/3 of a normal day's, so gamma turns a 0.03% adverse
# move into a ~20% premium loss (same reasoning as the Judas/HA-EMA strategies here).
SKIP_EXPIRY_DAY_UNDERLYINGS = {
    s.strip().upper() for s in os.getenv('SKIP_EXPIRY_DAY_UNDERLYINGS', 'NIFTY').split(',') if s.strip()
}

# ---------------------------------------------------------------- regime gates
# Both were chosen on 2023-24 and re-validated under a full walk-forward that
# re-fits parameters AND this cutoff every quarter (349 OOS trades, PF 1.19,
# +Rs 32,142; see backtesting/haema_signal/red_bar_config_spec.md). WITHOUT
# them the same signal loses: the untouched 2026-05-28..08-06 window is
# -Rs 10,919 ungated (PF 0.61) versus -Rs 896 gated (PF 0.94). They subtract
# losers rather than add winners, so they are not optional decoration.
#
# SKIP_WEEKDAYS: Monday=0 .. Friday=4. Tuesday is the only day gate that held
# out of sample.
SKIP_WEEKDAYS = {
    int(x) for x in os.getenv('SKIP_WEEKDAYS', '1').split(',') if x.strip().lstrip('-').isdigit()
}
# MOM5_PREV_MAX: stand down after a strong 5-session run-up. mom5_prev is the
# 5-session return ENDING YESTERDAY -- close[-1]/close[-6]-1 over prior daily
# closes -- so it is fully known before the session opens. 0.0137 is the
# 2023-24 75th percentile, picked before any out-of-sample look.
MOM5_PREV_MAX = float(os.getenv('MOM5_PREV_MAX', '0.0137'))

# DRY_RUN: shadow mode. Signals, sizing, exits and P&L are computed and logged
# exactly as live, but no order is sent, no lock is taken and no broker
# position is consulted. Used to forward-test alongside a live instance
# without competing for capital or for the shared symbol/direction locks.
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'

LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"red_bar_x_candle_{UNDERLYING}.json"
# A leaked lock must never wedge the host forever (siblings found 9 orphaned .lock
# files, some on expired contracts, silently blocking valid entries).
LOCK_TTL_MIN = float(os.getenv('LOCK_TTL_MIN', '360'))


# ------------------------------------------------------------ locks
def _pid_alive(pid):
    """True if the process still exists. Unknown -> assume alive (never steal a live lock)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _lock_is_stale(ts_str, pid):
    """Stale if written on an earlier session, past its TTL, or its owner has died."""
    try:
        when = datetime.fromisoformat(str(ts_str))
    except (ValueError, TypeError):
        return True
    if when.date() != date.today():
        return True
    if (datetime.now() - when).total_seconds() / 60.0 > LOCK_TTL_MIN:
        return True
    if pid and not _pid_alive(pid):
        return True
    return False


def _strategy_slug(name):
    return re.sub(r'[^A-Za-z0-9]+', '_', str(name)).strip('_')


def acquire_symbol_lock(symbol, strategy_name):
    """Claim one CONTRACT. True if acquired, already ours, or the holder's lock is stale."""
    if DRY_RUN:
        return True          # shadow: never contend with the live instance
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    if lock_file.exists():
        try:
            parts = lock_file.read_text().split("|")
            owner = parts[0]
            ts = parts[1] if len(parts) > 1 else ""
            pid = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
            if owner == strategy_name:
                return True
            if not _lock_is_stale(ts, pid):
                return False
            log.warning(f"Reclaiming stale lock on {symbol} from '{owner}' (ts={ts} pid={pid})")
        except Exception:
            return False
    try:
        lock_file.write_text(f"{strategy_name}|{datetime.now().isoformat()}|{os.getpid()}")
        return True
    except Exception:
        return False


def release_symbol_lock(symbol, strategy_name):
    if DRY_RUN:
        return
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists() and lock_file.read_text().split("|", 1)[0] == strategy_name:
            lock_file.unlink()
    except Exception:
        pass


# Directional lock: one directional VIEW per underlying, ACROSS strategies. Siblings
# measured episodes where two strategies held CE and PE on the same underlying at
# once -- net delta ~0, double premium, double theta: a straddle nobody designed.
# The per-contract lock cannot see it because CE and PE are different symbols.
def acquire_direction_lock(underlying, side, strategy_name):
    """Claim a direction on an underlying. False if another strategy holds the opposite."""
    if DRY_RUN:
        return True          # shadow: never blocked by, and never blocks, the live book
    und = str(underlying).upper()
    me = _strategy_slug(strategy_name)
    want = str(side).upper()
    try:
        for f in LOCKS_DIR.glob(f"{und}.*.dir"):
            parts = f.name.split(".")
            if len(parts) < 4:
                continue
            owner, held = parts[1], parts[2].upper()
            if owner == me:
                continue
            try:
                body = f.read_text().split("|")
                ts = body[0] if body else ""
                pid = int(body[1]) if len(body) > 1 and body[1].strip().isdigit() else 0
            except Exception:
                ts, pid = "", 0
            if _lock_is_stale(ts, pid):
                try:
                    f.unlink()
                except Exception:
                    pass
                continue
            if held != want:
                log.info(f"DIRECTION CONFLICT on {und}: '{owner}' already holds {held}, "
                         f"we want {want} - standing aside (would be a delta-neutral "
                         f"straddle paying double premium)")
                return False
    except Exception as e:
        log.debug(f"direction lock scan failed: {e}")
    try:
        (LOCKS_DIR / f"{und}.{me}.{want}.dir").write_text(f"{datetime.now().isoformat()}|{os.getpid()}")
    except Exception:
        pass
    return True


def release_direction_lock(underlying, strategy_name, side=None):
    if DRY_RUN:
        return
    und = str(underlying).upper()
    me = _strategy_slug(strategy_name)
    pat = f"{und}.{me}.{str(side).upper()}.dir" if side else f"{und}.{me}.*.dir"
    try:
        for f in LOCKS_DIR.glob(pat):
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ------------------------------------------------------------ state persistence
# The trade snapshot lets a restart re-arm SL/target/partial context instead of
# degrading to "EOD-exit-only". The day counters are persisted with it so a restart
# loop cannot re-arm the circuit breakers and turn a daily loss cap into no cap.
def persist_state(trade, day):
    try:
        STATE_FILE.write_text(json.dumps({"trade": trade or {}, "day": day or {}}))
    except Exception as e:
        log.debug(f"persist_state failed: {e}")


def load_state():
    """Returns (trade_dict, day_dict); empty dicts when absent or corrupt."""
    try:
        if STATE_FILE.exists():
            blob = json.loads(STATE_FILE.read_text())
            if isinstance(blob, dict):
                return blob.get("trade") or {}, blob.get("day") or {}
    except Exception as e:
        log.warning(f"load_state failed: {e}")
    return {}, {}


def reconcile_orphan_position(underlying):
    """Find an open broker position on this underlying (restart adoption)."""
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return None
        for pos in pb.get("data", []):
            qty = int(pos.get("quantity", 0) or 0)
            sym = (pos.get("symbol", "") or "").upper()
            if qty != 0 and underlying.upper() in sym:
                return {
                    "symbol": pos.get("symbol"),
                    "direction": "CE" if "CE" in sym else "PE" if "PE" in sym else "UNKNOWN",
                    "qty": abs(qty),
                    "entry_price": float(pos.get("average_price", 0) or 0),
                    "adopted": True,
                }
    except Exception as e:
        log.debug(f"Reconcile failed: {e}")
    return None


def live_position_qty(underlying, symbol):
    """Broker's current qty on `symbol`: >0 held, 0 absent, None if unverifiable.

    In shadow mode there is no broker position, so report the qty this process
    believes it holds -- otherwise the empty book would be read as a filled
    premium SL and tear down the simulated trade.
    """
    if DRY_RUN:
        return int(_active_trade.get("qty_open", 0) or 0)
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return None
        for pos in pb.get("data", []):
            if (pos.get("symbol", "") or "").upper() == symbol.upper():
                return abs(int(pos.get("quantity", 0) or 0))
        return 0
    except Exception as e:
        log.debug(f"live_position_qty failed for {symbol}: {e}")
        return None


# ------------------------------------------------------------ broker helpers
def fetch_available_capital():
    try:
        resp = client.funds()
        if isinstance(resp, dict) and resp.get("status") == "success":
            cash = (resp.get("data") or {}).get("availablecash")
            if cash is not None:
                return float(cash)
    except Exception as e:
        log.warning(f"Failed to fetch capital: {e}")
    return None


def compute_auto_lots(capital, risk_pct, max_loss_per_unit, lot_size, hard_cap_lots):
    """Lot count from the risk budget. max_loss_per_unit is rupees per single contract."""
    if max_loss_per_unit <= 0 or lot_size <= 0:
        return 1
    max_loss_per_lot = max_loss_per_unit * lot_size
    if max_loss_per_lot <= 0:
        return 1
    auto_lots = int(capital * (risk_pct / 100.0) / max_loss_per_lot)
    return max(1, min(auto_lots, hard_cap_lots))


def fetch_option_ltp(opt_symbol, opt_exchange, underlying_ltp=None, max_retries=3, retry_delay=1.0):
    """Option LTP, guarded against brokers leaking the spot value on a cold cache."""
    for attempt in range(max_retries):
        try:
            q = client.quotes(symbol=opt_symbol, exchange=opt_exchange)
            if q.get("status") == "success":
                ltp = float(q["data"]["ltp"])
                if underlying_ltp is None or ltp < underlying_ltp * 0.2:
                    return ltp
                log.warning(f"Option LTP {ltp:.2f} too close to spot {underlying_ltp:.2f} for "
                            f"{opt_symbol}; retry {attempt+1}/{max_retries}")
        except Exception as e:
            log.warning(f"Option LTP fetch failed for {opt_symbol}: {e}; retry {attempt+1}/{max_retries}")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.error(f"No valid option LTP for {opt_symbol} after {max_retries} attempts")
    return None


def fetch_fill_price(order_id, symbol, max_retries=4, retry_delay=0.8):
    """Average TRADED price for an order. P&L must come from fills, never quotes.

    A MARKET exit decided off an observed price can fill points away; the circuit
    breakers then arm on fictional numbers. Returns None when unreadable (caller
    falls back to the quote and says so in the log).
    """
    if not order_id:
        return None
    for attempt in range(max_retries):
        try:
            r = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
            d = (r.get("data") or {}) if isinstance(r, dict) else {}
            for k in ("average_price", "averageprice", "avgprice", "avg_price", "price"):
                v = d.get(k)
                if v not in (None, "", 0, "0") and float(v) > 0:
                    return float(v)
        except Exception as e:
            log.debug(f"orderstatus {order_id} attempt {attempt+1}: {e}")
        try:
            tb = client.tradebook()
            for t in (tb.get("data") or []):
                if str(t.get("symbol")) == str(symbol):
                    for k in ("average_price", "averageprice", "fill_price", "price"):
                        v = t.get(k)
                        if v not in (None, "", 0, "0") and float(v) > 0:
                            return float(v)
        except Exception:
            pass
        time.sleep(retry_delay)
    log.warning(f"No fill price for order {order_id} ({symbol}); falling back to quote")
    return None


def safe_cancel_order(order_id, context=""):
    """Cancel an order, treating already-terminal states as success."""
    try:
        resp = client.cancelorder(order_id=order_id, strategy=STRATEGY_NAME)
    except Exception as e:
        return False, f"cancelorder threw: {e}"
    if not isinstance(resp, dict):
        return True, f"non-dict response (assumed ok): {resp}"
    if resp.get("status") == "success":
        return True, "cancelled"
    msg = str(resp.get("message", "")).lower()
    if any(t in msg for t in ("complete", "cancelled", "canceled", "rejected",
                              "trigger pending", "no such order")):
        return True, f"already terminal: {resp.get('message', '')}"
    return False, f"{resp.get('message', resp)}"


def statutory_cost(entry_px, exit_px, qty):
    """Round-trip statutory cost in rupees for an option BUY->SELL, premium-based."""
    if entry_px is None or exit_px is None or not qty:
        return 0.0
    return (float(entry_px) + float(exit_px)) * float(qty) * OPT_COST_PCT / 100.0


def _tick(px):
    return round(round(float(px) / 0.05) * 0.05, 2)


def place_premium_sl(opt_symbol, opt_exchange, qty, entry_px):
    """Broker-side stop-limit SELL at entry_px * (1 - PREMIUM_SL_PCT%).

    Returns (order_id, trigger_price); order_id is None when the stop is NOT live.
    """
    if entry_px is None or qty <= 0:
        return None, None
    if DRY_RUN:
        trig = _tick(entry_px * (1.0 - PREMIUM_SL_PCT / 100.0))
        log.info(f"[SHADOW] premium SL would sit @ {trig} ({PREMIUM_SL_PCT:.0f}% below "
                 f"entry {entry_px}) qty {qty}")
        return "shadow-sl", trig
    trigger = _tick(entry_px * (1.0 - PREMIUM_SL_PCT / 100.0))
    limit = max(0.05, _tick(trigger * (1.0 - SL_LIMIT_BUFFER_PCT / 100.0)))
    try:
        resp = client.placeorder(
            strategy=STRATEGY_NAME, symbol=opt_symbol, action="SELL",
            exchange=opt_exchange, price_type="SL", trigger_price=trigger,
            price=limit, product=PRODUCT, quantity=qty,
        )
        oid = resp.get("orderid") if isinstance(resp, dict) else None
        # An order without an id does NOT exist: the API answers status=success with
        # orderid=null when the broker rejected it. Trusting that left 33 sibling
        # positions unprotected while the log claimed a stop was armed.
        if isinstance(resp, dict) and resp.get("status") == "success" and oid:
            log.info(f"Premium SL placed @ trigger {trigger} limit {limit} "
                     f"({PREMIUM_SL_PCT:.0f}% below entry {entry_px}) qty {qty} -- order {oid}")
            return oid, trigger
        log.error(f"PREMIUM SL NOT PLACED -- position is UNPROTECTED at the broker; "
                  f"relying on in-process monitoring only. Response: {resp}")
    except Exception as e:
        log.error(f"Failed to place premium SL: {e} -- position is UNPROTECTED at the broker")
    return None, trigger


def verified_exit_sell(underlying, symbol, opt_exchange, qty, sl_oid, reason):
    """Cancel the protective SL and SELL only what the broker ACTUALLY holds.

    Returns (outcome, sold_qty, fill_price) where outcome is one of:
      'sold'     -- SELL accepted; sold_qty went out
      'flat'     -- broker holds nothing; nothing to do
      'unknown'  -- positionbook unverifiable; caller MUST keep tracking and retry
      'rejected' -- SELL was refused; caller MUST keep tracking and retry
    Never collapse 'rejected' into 'flat': that abandons a live position.
    """
    if DRY_RUN:
        px = fetch_option_ltp(symbol, opt_exchange)
        log.info(f"[SHADOW] {reason}: would SELL {qty} {symbol} @ {px}")
        return "sold", qty, px
    bq = live_position_qty(underlying, symbol)
    if bq is None:
        log.warning(f"{reason}: cannot verify broker position for {symbol} -- deferring exit")
        return "unknown", 0, None
    if sl_oid:
        ok, msg = safe_cancel_order(sl_oid, context=f"{reason}-{symbol}")
        (log.info if ok else log.warning)(f"{reason}: cancel SL {sl_oid} -> {msg}")
    if bq <= 0:
        log.warning(f"{reason}: broker flat on {symbol} -- no long to close; "
                    f"skipping SELL to avoid a naked short")
        return "flat", 0, None
    close_qty = min(bq, qty)
    try:
        resp = client.placeorder(
            strategy=STRATEGY_NAME, symbol=symbol, action="SELL", exchange=opt_exchange,
            price_type="MARKET", product=PRODUCT, quantity=close_qty)
    except Exception as e:
        log.error(f"{reason}: SELL threw for {symbol}: {e} -- position still open, will retry")
        return "rejected", 0, None
    log.info(f"{reason} exit response for {symbol}: {resp}")
    if not (isinstance(resp, dict) and resp.get("status") == "success"):
        log.error(f"{reason}: SELL REJECTED for {symbol} -- position still open, will retry")
        return "rejected", 0, None
    return "sold", close_qty, fetch_fill_price(resp.get("orderid"), symbol)


def get_nearest_expiry(underlying, exchange):
    try:
        resp = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="options")
        if resp.get("status") == "success" and resp.get("data"):
            return resp["data"][0].replace("-", "")
    except Exception as e:
        log.error(f"Error fetching expiry: {e}")
    return None


def get_option_symbol(underlying, exchange, expiry, offset, option_type):
    try:
        resp = client.optionsymbol(
            underlying=underlying, exchange=exchange, expiry_date=expiry,
            offset=offset, option_type=option_type,
        )
        if resp.get("status") == "success":
            return resp.get("symbol")
    except Exception as e:
        log.error(f"Error fetching optionsymbol: {e}")
    return None


def is_expiry_today(expiry_str):
    if not expiry_str:
        return False
    try:
        return datetime.strptime(expiry_str.upper(), "%d%b%y").date() == date.today()
    except (ValueError, TypeError):
        return False


def fetch_lot_size(underlying, idx_exchange, opt_exchange):
    try:
        expiry = get_nearest_expiry(underlying, opt_exchange)
        if not expiry:
            return None
        resp = client.optionchain(underlying=underlying, exchange=idx_exchange,
                                  expiry_date=expiry, strike_count=1)
        if resp.get("status") == "success":
            for item in resp.get("chain", []):
                for leg in (item.get("ce") or {}, item.get("pe") or {}):
                    if leg.get("lotsize"):
                        return int(leg["lotsize"])
    except Exception as e:
        log.error(f"Error fetching lot size: {e}")
    return None


def fetch_daily_context(underlying, idx_exchange, today):
    """Previous session's CPR band + close. Returns (cpr, prev_close) or (None, None).

    CPR [digest 2.6]: CPP = (H+L+C)/3; the other edge = (H+L)/2; the band is the
    span between them (which edge is top/bottom depends on the day).
    """
    try:
        start = (today - timedelta(days=15)).strftime("%Y-%m-%d")
        df = client.history(symbol=underlying, exchange=idx_exchange, interval="D",
                            start_date=start, end_date=today.strftime("%Y-%m-%d"))
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None, None
        df = df.sort_index()
        prior = df[[t.date() < today for t in pd.to_datetime(df.index)]]
        if prior.empty:
            return None, None
        row = prior.iloc[-1]
        ph, pl, pc = float(row["high"]), float(row["low"]), float(row["close"])
        cpp = (ph + pl + pc) / 3.0
        edge = (ph + pl) / 2.0
        other = 2.0 * cpp - edge
        return {"cpp": cpp, "top": max(edge, other), "bottom": min(edge, other)}, pc
    except Exception as e:
        log.warning(f"Daily context fetch failed: {e}")
        return None, None


def regime_gate(underlying, idx_exchange, today):
    """(ok, detail) for the two IS-chosen regime gates, decided before entry.

    Gate A -- weekday: Tuesday (1) stands down.
    Gate B -- mom5_prev = close[-1]/close[-6] - 1 over PRIOR daily closes must
              be < MOM5_PREV_MAX. Both closes predate today's open, so there
              is no lookahead.

    FAILS CLOSED, like the CPR and gap gates: if the daily series cannot be
    fetched or is too short, the day is refused rather than traded unfiltered.
    Ungated, this signal loses money -- see the note at SKIP_WEEKDAYS.
    """
    if today.weekday() in SKIP_WEEKDAYS:
        return False, f"weekday gate: {today:%A} is in SKIP_WEEKDAYS"
    try:
        start = (today - timedelta(days=25)).strftime("%Y-%m-%d")
        df = client.history(symbol=underlying, exchange=idx_exchange, interval="D",
                            start_date=start, end_date=today.strftime("%Y-%m-%d"))
        if not isinstance(df, pd.DataFrame) or df.empty:
            return False, "mom5 gate: no daily history (failing closed)"
        df = df.sort_index()
        prior = df[[t.date() < today for t in pd.to_datetime(df.index)]]
        if len(prior) < 6:
            return False, f"mom5 gate: only {len(prior)} prior sessions (failing closed)"
        closes = [float(x) for x in prior["close"].tail(6)]
        mom5_prev = closes[-1] / closes[0] - 1.0
        if mom5_prev >= MOM5_PREV_MAX:
            return False, (f"mom5 gate: mom5_prev {mom5_prev:+.4f} >= {MOM5_PREV_MAX} "
                           f"(5-session run-up, standing down)")
        return True, f"gates clear (mom5_prev {mom5_prev:+.4f} < {MOM5_PREV_MAX})"
    except Exception as e:
        return False, f"mom5 gate: fetch failed ({e}) -- failing closed"


# ---------------------------------------------------------------- the signal
def _anchor_from(times, highs, lows, start, end):
    """High/low of the 30-min block whose 5m opens fall in [start, end)."""
    hi = lo = None
    last_idx = -1
    bars = 0
    for i, t in enumerate(times):
        if start <= t < end:
            hi = highs[i] if hi is None else max(hi, highs[i])
            lo = lows[i] if lo is None else min(lo, lows[i])
            last_idx = i
            bars += 1
    if hi is None or lo is None or hi <= lo or bars < MIN_ANCHOR_BARS:
        return None
    return {"high": hi, "low": lo, "end_idx": last_idx, "bars": bars,
            "l44": lo + FIB_LO * (hi - lo),
            "l50": lo + 0.50 * (hi - lo),
            "l56": lo + FIB_HI * (hi - lo)}


def _plus30(t):
    return (datetime.combine(date.today(), t) + timedelta(minutes=30)).time()


def compute_red_bar_signal(df_5m, today, cpr, prev_close):
    """Evaluate the latest COMPLETED 5m candle against the X-candle framework.

    Live history is OPEN-timestamped (the 09:15 row is the 09:15-09:20 bar), so the
    X candle is the set of 5m opens in [SESSION_OPEN, X_END) and comparisons are
    strict '<'. Pre-open rows, if the feed emits any, are excluded by that window.

    Always returns a status dict (so the monitor panel has values) with 'signal'
    set to 'CE'/'PE' only when every gate passes. None when data is insufficient.
    """
    if not isinstance(df_5m, pd.DataFrame) or df_5m.empty:
        return None
    df = df_5m.sort_index().iloc[:-1]        # drop the forming candle
    if df.empty:
        return None

    # EMAs computed on the full multi-day series for continuity, then sliced to today
    ema10_s = df["close"].ewm(span=10, adjust=False).mean()
    ema30_s = df["close"].ewm(span=30, adjust=False).mean()

    mask = [t.date() == today for t in pd.to_datetime(df.index)]
    day = df[mask]
    if len(day) < MIN_ANCHOR_BARS + 1:
        return None
    ema10 = ema10_s[mask].tolist()
    ema30 = ema30_s[mask].tolist()
    times = [t.time() for t in pd.to_datetime(day.index)]
    opens, highs = day["open"].tolist(), day["high"].tolist()
    lows, closes = day["low"].tolist(), day["close"].tolist()

    x = _anchor_from(times, highs, lows, SESSION_OPEN, X_END)
    if x is None:
        return None
    # Session open for the gap measurement: the first bar at/after SESSION_OPEN
    day_open = next((opens[i] for i, t in enumerate(times) if t >= SESSION_OPEN), opens[0])

    last = len(day) - 1
    lt, c_open, c_high, c_low, c_close = times[last], opens[last], highs[last], lows[last], closes[last]

    # 12:45 re-anchor [2.9]: active only once that 30-min block has completed
    anchor_name = "X"
    if REANCHOR_1245 and lt >= _plus30(REANCHOR_START):
        alt = _anchor_from(times, highs, lows, REANCHOR_START, _plus30(REANCHOR_START))
        if alt is not None:
            x, anchor_name = alt, "12:45"

    status = {
        "signal": None, "reason": "", "anchor": anchor_name,
        "x_high": x["high"], "x_low": x["low"], "range": x["high"] - x["low"],
        "l44": x["l44"], "l50": x["l50"], "l56": x["l56"],
        "spot": c_close, "candle_fp": (float(c_open), float(c_high), float(c_low), float(c_close)),
    }

    if last <= x["end_idx"]:
        status["reason"] = "inside anchor window"
        return status
    if lt < X_END or lt >= ENTRY_END:
        status["reason"] = "outside entry window"
        return status

    # [2.4] sideways day: the anchor range must have been broken at least once
    # "पहले 30 मिनट की candle का high और low नहीं तूटा और उसके उपर/नीचे candle ने
    # close नहीं किया" (L83-84): the break must be a CLOSE beyond the anchor, not a
    # wick through it. A touch-based test admits the sideways days the rule excludes.
    post = range(x["end_idx"] + 1, len(day))
    if not any(closes[i] > x["high"] or closes[i] < x["low"] for i in post):
        status["reason"] = f"inside {anchor_name} body (sideways, no trade)"
        return status

    # [2.4/3.1] the trigger bar must be red
    if c_close >= c_open:
        status["reason"] = "trigger candle not red"
        return status

    if c_close > x["l56"]:
        direction = "CE"
    elif c_close < x["l44"]:
        direction = "PE"
    else:
        status["reason"] = f"close inside the {FIB_LO}-{FIB_HI} margin"
        return status

    # [2.7] EMA trend filter
    e10, e30 = ema10[last], ema30[last]
    if REQUIRE_EMA10 and not ((direction == "CE" and c_close > e10) or (direction == "PE" and c_close < e10)):
        status["reason"] = f"{direction} blocked by EMA10 {e10:.2f}"
        return status
    if REQUIRE_EMA30 and not ((direction == "CE" and c_close > e30) or (direction == "PE" and c_close < e30)):
        status["reason"] = f"{direction} blocked by EMA30 {e30:.2f}"
        return status

    # [2.6] CPR confluence. FAIL CLOSED: no daily bar means the gate cannot be
    # evaluated, so the trade is refused rather than taken with the gate silently off.
    if REQUIRE_CPR:
        if not cpr:
            status["reason"] = "CPR unavailable -- gate cannot be evaluated, standing down"
            return status
        if direction == "CE" and c_close <= cpr["top"]:
            status["reason"] = f"CE blocked below CPR top {cpr['top']:.2f}"
            return status
        if direction == "PE" and c_close >= cpr["bottom"]:
            status["reason"] = f"PE blocked above CPR bottom {cpr['bottom']:.2f}"
            return status

    # [2.5] gap gate: a gap must be rebuilt through its own 50% before trading with it
    if REQUIRE_GAP_GATE:
        if not prev_close:
            status["reason"] = "prev close unavailable -- gap gate cannot be evaluated"
            return status
        gap = day_open - prev_close
        if abs(gap) >= prev_close * (GAP_PCT / 100.0):
            gap_mid = prev_close + gap / 2.0
            if gap < 0 and direction == "CE" and c_close < gap_mid:
                status["reason"] = f"gap-down not rebuilt to 50% ({gap_mid:.2f})"
                return status
            if gap > 0 and direction == "PE" and c_close > gap_mid:
                status["reason"] = f"gap-up not rebuilt to 50% ({gap_mid:.2f})"
                return status

    # [2.4] stop is glued to the trigger bar's extreme; T1 and target are measured
    # off the SAME (floored) risk so the configured reward:risk is what gets traded.
    raw_risk = (c_close - c_low) if direction == "CE" else (c_high - c_close)
    if raw_risk <= 0:
        status["reason"] = "degenerate trigger bar"
        return status
    if raw_risk > c_close * (MAX_SL_PCT / 100.0):
        status["reason"] = f"trigger bar too tall ({raw_risk:.2f} pts)"
        return status
    risk = max(raw_risk, c_close * (MIN_SL_PCT / 100.0))
    sign = 1.0 if direction == "CE" else -1.0
    status.update({
        "signal": direction,
        "reason": f"red bar {'above L56' if direction == 'CE' else 'below L44'} on {anchor_name}",
        "entry_spot": c_close,
        "sl_spot": c_close - sign * risk,
        "t1_spot": c_close + sign * T1_RR * risk,
        "target_spot": c_close + sign * RR * risk,
        "risk": risk,
    })
    return status


# ------------------------------------------------------------------ shutdown
_active_trade = {}
_day_state = {}
_opt_exchange = None


def _graceful_shutdown(signum, frame):
    log.info(f"SHUTDOWN SIGNAL ({signal.Signals(signum).name}) -- cleaning up...")
    symbol = (_active_trade or {}).get("symbol")
    if symbol and _opt_exchange:
        try:
            outcome, _sold, _px = verified_exit_sell(
                UNDERLYING, symbol, _opt_exchange,
                _active_trade.get("qty_open", LOT_SIZE * MAX_LOTS),
                _active_trade.get("opt_sl_orderid"), "Shutdown: Closing position")
            if outcome in ("unknown", "rejected"):
                log.error(f"Shutdown could not flatten {symbol} ({outcome}) -- leaving state "
                          f"and lock intact for restart adoption")
                sys.exit(0)
            release_symbol_lock(symbol, STRATEGY_NAME)
            release_direction_lock(UNDERLYING, STRATEGY_NAME)
            persist_state({}, _day_state)
        except Exception as e:
            log.error(f"Failed to close position on shutdown: {e}")
    else:
        log.info("No active position -- nothing to close.")
    log.info("Shutdown complete. Exiting.")
    sys.exit(0)


signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)


# ---------------------------------------------------------------- run loop
def run_strategy():
    global _active_trade, _day_state, _opt_exchange, QUANTITY, LOT_SIZE
    idx_exchange = _index_exchange(UNDERLYING)
    opt_exchange = _option_exchange(UNDERLYING)
    _opt_exchange = opt_exchange

    log.info(f"Starting {STRATEGY_NAME} on {UNDERLYING} ({idx_exchange}/{opt_exchange})")
    log.info(f"X <= {X_END} | re-anchor {REANCHOR_START} | entry <= {ENTRY_END} | flat {EXIT_TIME}")
    log.info(f"Strike {STRIKE_OFFSET} | RR {RR} (T1 {T1_RR}) | fib {FIB_LO}/{FIB_HI} | "
             f"EMA10 {REQUIRE_EMA10} EMA30 {REQUIRE_EMA30} CPR {REQUIRE_CPR} GAP {REQUIRE_GAP_GATE}")
    log.info(f"Premium SL {PREMIUM_SL_PCT}% | max hold {MAX_HOLD_MINUTES}min | "
             f"decay floor {DECAY_EXIT_PCT:.0%} | trades/day {MAX_TRADES_PER_DAY}")
    log.info(f"Gates: skip weekdays {sorted(SKIP_WEEKDAYS)} | mom5_prev < {MOM5_PREV_MAX} "
             f"| maxSL {MAX_SL_PCT}% (both gates FAIL CLOSED)")
    if DRY_RUN:
        log.warning("=== SHADOW MODE (DRY_RUN=true) -- no orders, no locks, no capital. "
                    "Signals and P&L are logged as if live. ===")
    else:
        log.warning("=== LIVE MODE -- orders are REAL. ===")

    if QUANTITY == 0:
        detected = fetch_lot_size(UNDERLYING, idx_exchange, opt_exchange)
        QUANTITY = detected or (20 if _IS_BSE else 75)
        if detected:
            log.info(f"Auto-detected lot size: {QUANTITY}")
        else:
            log.warning(f"Could not detect lot size, using default: {QUANTITY}")
    LOT_SIZE = QUANTITY
    if MAX_LOTS < 2 and LOT_MODE != "auto":
        log.warning(f"MAX_LOTS={MAX_LOTS}: the digest [2.11] half-book at T1 is DISABLED "
                    f"(needs >= 2 lots). The whole position runs to target or stop.")

    state = "IDLE"
    active_trade = {}
    trade_date = None
    last_entry_candle_fp = None
    trades_today = 0
    consecutive_losses = 0
    daily_loss_rs = 0.0
    cpr, prev_close, ctx_date = None, None, None
    expiry_cache = (None, None)
    gate_cache = (None, False, "")
    force_flatten = False

    def save():
        _day = {"date": str(trade_date), "trades_today": trades_today,
                "consecutive_losses": consecutive_losses, "daily_loss_rs": daily_loss_rs}
        persist_state(active_trade, _day)
        return _day

    # ---- boot: restore counters, then adopt any open broker position
    saved_trade, saved_day = load_state()
    if saved_day.get("date") == str(date.today()):
        trades_today = int(saved_day.get("trades_today", 0))
        consecutive_losses = int(saved_day.get("consecutive_losses", 0))
        daily_loss_rs = float(saved_day.get("daily_loss_rs", 0.0))
        log.info(f"Restored today's counters: trades {trades_today} | "
                 f"loss streak {consecutive_losses} | daily losses Rs {daily_loss_rs:.0f}")

    orphan = reconcile_orphan_position(UNDERLYING)
    if orphan:
        usable = (saved_trade and saved_trade.get("symbol") == orphan["symbol"]
                  and saved_trade.get("sl_spot") is not None
                  and saved_trade.get("t1_spot") is not None
                  and saved_trade.get("target_spot") is not None
                  and saved_trade.get("entry_spot") is not None)
        if usable:
            active_trade = dict(saved_trade)
            # A crash between the T1 SELL and its persist leaves the file claiming
            # the full position on the original wide stop. The broker's qty is the
            # truth: if it shrank, the partial already happened -- adopt that.
            if orphan["qty"] < int(saved_trade.get("qty_open", 0) or 0):
                active_trade["partial_done"] = True
                active_trade["sl_spot"] = active_trade["entry_spot"]
                log.warning(f"Broker qty {orphan['qty']} < persisted "
                            f"{saved_trade.get('qty_open')}: T1 partial already filled; "
                            f"adopting stop-at-cost {active_trade['sl_spot']}")
            active_trade["qty_open"] = orphan["qty"]     # broker is authoritative
            active_trade.pop("adopted", None)
            log.warning(f"Adopting {orphan['symbol']} qty={orphan['qty']} with restored context "
                        f"| SL {active_trade['sl_spot']} | Target {active_trade['target_spot']}")
        else:
            log.warning(f"Adopting unknown orphan: {orphan['symbol']} qty={orphan['qty']} "
                        f"-- no spot context; protected only by the max-hold timer, "
                        f"the decay floor and the EOD squareoff")
            active_trade = {"symbol": orphan["symbol"], "direction": orphan["direction"],
                            "entry_spot": None, "sl_spot": None, "t1_spot": None,
                            "target_spot": None, "qty_open": orphan["qty"],
                            "entry_opt_price": orphan["entry_price"] or None,
                            "entry_time": datetime.now().isoformat(), "adopted": True}
        _active_trade = active_trade
        state = "IN_TRADE"
        trades_today = max(trades_today, 1)
        acquire_symbol_lock(orphan["symbol"], STRATEGY_NAME)
        if active_trade.get("direction") in ("CE", "PE"):
            acquire_direction_lock(UNDERLYING, active_trade["direction"], STRATEGY_NAME)
        trade_date = date.today()   # seed so the new-day reset does not wipe the adoption
        _day_state = save()
    else:
        trade_date = date.today()
        _day_state = save()

    while True:
        try:
            today = date.today()
            if trade_date != today:
                if state == "IN_TRADE" and active_trade:
                    # Never wipe an open position on the date roll: that leaks the
                    # lock, destroys the snapshot and abandons a live long. Flatten it.
                    log.error(f"DATE ROLLOVER with {active_trade.get('symbol')} still open "
                              f"-- forcing a flatten before resetting the day")
                    force_flatten = True
                else:
                    trade_date = today
                    state, active_trade, _active_trade = "IDLE", {}, {}
                    last_entry_candle_fp = None
                    trades_today = 0
                    consecutive_losses = 0
                    daily_loss_rs = 0.0
                    force_flatten = False
                    _day_state = save()
                    log.info(f"--- New trading day initialized: {trade_date} ---")

            now = datetime.now()
            current_time = now.time()

            if ctx_date != today:
                cpr, prev_close = fetch_daily_context(UNDERLYING, idx_exchange, today)
                if cpr:
                    ctx_date = today
                    log.info(f"CPR {cpr['bottom']:.2f}-{cpr['top']:.2f} (CPP {cpr['cpp']:.2f}) "
                             f"| prev close {prev_close:.2f}")
                else:
                    log.warning("Daily context unavailable -- CPR/gap gates will refuse "
                                "entries until it loads")

            quotes_resp = client.quotes(symbol=UNDERLYING, exchange=idx_exchange)
            if not quotes_resp or quotes_resp.get("status") != "success" or "data" not in quotes_resp:
                log.warning(f"Failed to fetch quotes for {UNDERLYING}. Retrying...")
                time.sleep(15)
                continue
            underlying_ltp = float(quotes_resp["data"]["ltp"])

            # ------------------------------------------------ IN_TRADE
            if state == "IN_TRADE":
                symbol = active_trade["symbol"]
                direction = active_trade["direction"]
                sl_spot = active_trade.get("sl_spot")
                t1_spot = active_trade.get("t1_spot")
                target_spot = active_trade.get("target_spot")
                qty_open = active_trade.get("qty_open", 0)
                entry_opt_price = active_trade.get("entry_price_effective") or \
                    active_trade.get("entry_fill_price") or active_trade.get("entry_opt_price")
                sign = 1.0 if direction == "CE" else -1.0
                has_spot_levels = None not in (sl_spot, t1_spot, target_spot)
                held_min = None
                if active_trade.get("entry_time"):
                    try:
                        held_min = (now - datetime.fromisoformat(
                            active_trade["entry_time"])).total_seconds() / 60.0
                    except (ValueError, TypeError):
                        held_min = None

                # The broker SL may have filled on its own: the position then simply
                # vanishes from the book. Only trust an empty book once the entry has
                # had time to settle -- right after a BUY the position can legitimately
                # be missing from the book for a few seconds, and treating that as a
                # fill would tear down the state of a position that is about to exist.
                bq = live_position_qty(UNDERLYING, symbol)
                if bq == 0 and not force_flatten and (held_min is None or held_min > 0.5):
                    fill = fetch_fill_price(active_trade.get("opt_sl_orderid"), symbol)
                    log.info(f"Broker reports {symbol} flat -- premium SL filled "
                             f"(Closing position bookkeeping only)")
                    realised = active_trade.get("realised_pnl", 0.0)
                    if fill is not None and entry_opt_price:
                        realised += (fill - entry_opt_price) * qty_open \
                            - statutory_cost(entry_opt_price, fill, qty_open)
                    if realised < 0:
                        consecutive_losses += 1
                        daily_loss_rs += abs(realised)
                        log.info(f"Trade P&L: Rs {realised:+.2f} (premium SL) | "
                                 f"loss streak {consecutive_losses} | daily Rs {daily_loss_rs:.0f}")
                    else:
                        consecutive_losses = 0
                        log.info(f"Trade P&L: Rs {realised:+.2f} (premium SL) | loss streak reset")
                    release_symbol_lock(symbol, STRATEGY_NAME)
                    release_direction_lock(UNDERLYING, STRATEGY_NAME)
                    active_trade, _active_trade = {}, {}
                    state = "DONE" if trades_today >= MAX_TRADES_PER_DAY else "IDLE"
                    _day_state = save()
                    continue

                opt_ltp = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)
                if has_spot_levels:
                    log.info(f"Monitoring Trade: {symbol} | Spot: {underlying_ltp:.2f} | "
                             f"SL: {sl_spot:.2f} | T1: {t1_spot:.2f} | Target: {target_spot:.2f} | "
                             f"Qty: {qty_open}")
                else:
                    log.info(f"Monitoring Trade: {symbol} | Spot: {underlying_ltp:.2f} | "
                             f"Qty: {qty_open} | adopted, no spot levels")

                # [2.11] book half at T1, then run the rest with the stop at cost
                if (has_spot_levels and not active_trade.get("partial_done")
                        and not force_flatten and current_time < EXIT_TIME
                        and qty_open >= 2 * LOT_SIZE
                        and active_trade.get("partial_attempts", 0) < 3
                        and sign * (underlying_ltp - t1_spot) >= 0):
                    half = (qty_open // (2 * LOT_SIZE)) * LOT_SIZE
                    outcome, sold, fill = verified_exit_sell(
                        UNDERLYING, symbol, opt_exchange, half,
                        active_trade.get("opt_sl_orderid"), "T1 partial book")
                    if outcome == "sold":
                        booked = 0.0
                        px = fill if fill is not None else opt_ltp
                        if px is not None and entry_opt_price:
                            booked = (px - entry_opt_price) * sold \
                                - statutory_cost(entry_opt_price, px, sold)
                        active_trade["realised_pnl"] = active_trade.get("realised_pnl", 0.0) + booked
                        active_trade["qty_open"] = qty_open - sold
                        active_trade["sl_spot"] = active_trade["entry_spot"]
                        active_trade["partial_done"] = True
                        # The old SL was cancelled with the whole quantity on it; re-arm
                        # one sized to what is actually left, or the stop would try to
                        # sell more than we hold.
                        oid, trig = place_premium_sl(symbol, opt_exchange,
                                                     active_trade["qty_open"], entry_opt_price)
                        active_trade["opt_sl_orderid"], active_trade["opt_sl_price"] = oid, trig
                        _active_trade = active_trade
                        _day_state = save()
                        log.info(f"Booked {sold} at T1 for Rs {booked:+.2f}; stop moved to cost "
                                 f"{active_trade['entry_spot']:.2f}, {active_trade['qty_open']} riding")
                        time.sleep(5)
                        continue
                    if outcome in ("rejected", "unknown"):
                        active_trade["partial_attempts"] = active_trade.get("partial_attempts", 0) + 1
                        _day_state = save()
                        if active_trade["partial_attempts"] >= 3:
                            log.error("T1 partial failed 3x -- giving up on the half-book; "
                                      "the full position stays on its original stop")

                exit_reason = ""
                if force_flatten:
                    exit_reason = "Stale overnight position: Closing position"
                elif current_time >= EXIT_TIME:
                    exit_reason = f"EOD Squareoff ({EXIT_TIME}): Closing position"
                elif has_spot_levels and sign * (underlying_ltp - active_trade["sl_spot"]) <= 0:
                    exit_reason = ("Trail-to-cost Hit: Closing position"
                                   if active_trade.get("partial_done") else
                                   "Stop-Loss Hit: Closing position")
                elif has_spot_levels and sign * (underlying_ltp - target_spot) >= 0:
                    exit_reason = "Target Hit: Closing position"
                elif held_min is not None and held_min >= MAX_HOLD_MINUTES:
                    exit_reason = f"Max-hold exit ({held_min:.0f}min): Closing position"
                elif (opt_ltp is not None and entry_opt_price
                        and opt_ltp < entry_opt_price * DECAY_EXIT_PCT):
                    exit_reason = (f"Decay exit (LTP {opt_ltp:.2f} < {DECAY_EXIT_PCT:.0%} of "
                                   f"entry {entry_opt_price:.2f}): Closing position")

                if not exit_reason:
                    time.sleep(5)
                    continue

                log.info(f"!!! {exit_reason} on {symbol}...")
                outcome, sold, fill = verified_exit_sell(
                    UNDERLYING, symbol, opt_exchange, qty_open,
                    active_trade.get("opt_sl_orderid"), exit_reason)
                if outcome in ("unknown", "rejected"):
                    # Keep the position, the lock and the snapshot; retry next cycle.
                    time.sleep(5)
                    continue

                realised = active_trade.get("realised_pnl", 0.0)
                if outcome == "sold":
                    px = fill if fill is not None else opt_ltp
                    if px is not None and entry_opt_price:
                        realised += (px - entry_opt_price) * sold \
                            - statutory_cost(entry_opt_price, px, sold)
                        if fill is None:
                            log.warning("Exit P&L booked from a QUOTE (no fill price) -- "
                                        "excludes slippage")
                if realised < 0:
                    consecutive_losses += 1
                    daily_loss_rs += abs(realised)
                    log.info(f"Trade P&L: Rs {realised:+.2f} net | loss streak {consecutive_losses} "
                             f"| daily losses Rs {daily_loss_rs:.0f}")
                else:
                    consecutive_losses = 0
                    log.info(f"Trade P&L: Rs {realised:+.2f} net | loss streak reset")

                release_symbol_lock(symbol, STRATEGY_NAME)
                release_direction_lock(UNDERLYING, STRATEGY_NAME)
                active_trade, _active_trade = {}, {}
                if force_flatten:
                    # The flatten that the date roll was waiting on is done; reset now.
                    trade_date, force_flatten = today, False
                    state, last_entry_candle_fp = "IDLE", None
                    trades_today, consecutive_losses, daily_loss_rs = 0, 0, 0.0
                    log.info(f"--- New trading day initialized: {trade_date} ---")
                else:
                    state = ("DONE" if (exit_reason.startswith("EOD")
                                        or trades_today >= MAX_TRADES_PER_DAY) else "IDLE")
                _day_state = save()

            # --------------------------------------------------- IDLE
            elif state == "IDLE":
                if current_time <= X_END:
                    wait = (datetime.combine(today, X_END) - now).total_seconds()
                    log.info(f"Building the X candle (<= {X_END}). Waiting {int(max(wait, 0))}s...")
                    time.sleep(min(max(wait, 0) + 1, 60))
                    continue
                if current_time > ENTRY_END:
                    log.info(f"Past the entry window ({ENTRY_END}). Done for today.")
                    state = "DONE"
                    continue
                if trades_today >= MAX_TRADES_PER_DAY:
                    log.info(f"Trade cap reached ({trades_today}/{MAX_TRADES_PER_DAY}). Done for today.")
                    state = "DONE"
                    continue
                if consecutive_losses >= LOSS_STREAK_LIMIT:
                    log.warning(f"CIRCUIT BREAKER: {consecutive_losses} consecutive losses. Halting.")
                    state = "DONE"
                    continue
                if daily_loss_rs >= DAILY_LOSS_LIMIT_RS:
                    log.warning(f"CIRCUIT BREAKER: Rs {daily_loss_rs:.0f} daily losses. Halting.")
                    state = "DONE"
                    continue

                # Expiry-day stand-down, resolved once per session
                if expiry_cache[0] != today:
                    expiry_cache = (today, get_nearest_expiry(UNDERLYING, opt_exchange))
                expiry = expiry_cache[1]
                if not expiry:
                    time.sleep(15)
                    continue
                if UNDERLYING in SKIP_EXPIRY_DAY_UNDERLYINGS and is_expiry_today(expiry):
                    log.info(f"{UNDERLYING} weekly expires today ({expiry}) -- standing down (DTE-0 gamma)")
                    state = "DONE"
                    continue

                # Regime gates, resolved once per session (both are decided
                # from data that predates today's open, so one check is enough)
                if gate_cache[0] != today:
                    gate_cache = (today, *regime_gate(UNDERLYING, idx_exchange, today))
                if not gate_cache[1]:
                    log.info(f"Regime gate: standing down -- {gate_cache[2]}")
                    state = "DONE"
                    continue

                intra_start = (today - timedelta(days=5)).strftime("%Y-%m-%d")
                df_5m = client.history(symbol=UNDERLYING, exchange=idx_exchange,
                                       interval=INTERVAL, start_date=intra_start,
                                       end_date=today.strftime("%Y-%m-%d"))
                sig = compute_red_bar_signal(df_5m, today, cpr, prev_close)
                if not sig:
                    time.sleep(15)
                    continue

                log.info(f"Regime: {sig['anchor']} {sig['x_low']:.2f}-{sig['x_high']:.2f} | "
                         f"Phase: {'ARMED-' + sig['signal'] if sig['signal'] else 'SCANNING'} | "
                         f"Velocity: {sig['spot']:.2f} | ATR: {sig['range']:.2f} | {sig['reason']}")

                if not sig["signal"]:
                    time.sleep(15)
                    continue
                if last_entry_candle_fp is not None and sig["candle_fp"] == last_entry_candle_fp:
                    time.sleep(15)
                    continue

                opt_symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry,
                                               STRIKE_OFFSET, sig["signal"])
                if not opt_symbol:
                    time.sleep(15)
                    continue
                if not acquire_symbol_lock(opt_symbol, STRATEGY_NAME):
                    log.info(f"{opt_symbol} locked by another strategy. Skipping this signal.")
                    last_entry_candle_fp = sig["candle_fp"]
                    time.sleep(15)
                    continue
                if not acquire_direction_lock(UNDERLYING, sig["signal"], STRATEGY_NAME):
                    release_symbol_lock(opt_symbol, STRATEGY_NAME)
                    last_entry_candle_fp = sig["candle_fp"]
                    time.sleep(15)
                    continue

                entry_opt_price = fetch_option_ltp(opt_symbol, opt_exchange,
                                                   underlying_ltp=underlying_ltp)
                if entry_opt_price is None:
                    # No premium means no premium stop, no auto-lot and no P&L: both
                    # circuit breakers would be dead for this trade. Refuse it.
                    log.error(f"No option premium for {opt_symbol} -- skipping the entry "
                              f"(cannot size, stop or account for it)")
                    release_symbol_lock(opt_symbol, STRATEGY_NAME)
                    release_direction_lock(UNDERLYING, STRATEGY_NAME, sig["signal"])
                    last_entry_candle_fp = sig["candle_fp"]
                    time.sleep(15)
                    continue

                if LOT_MODE == "auto":
                    capital = fetch_available_capital()
                    if capital and capital > 0:
                        # Worst case per contract is the premium stop, not the premium.
                        per_unit = entry_opt_price * (PREMIUM_SL_PCT / 100.0)
                        lots = compute_auto_lots(capital, RISK_PCT_PER_TRADE, per_unit,
                                                 LOT_SIZE, MAX_LOTS)
                        log.info(f"AUTO-LOT: capital Rs {capital:.0f} | risk {RISK_PCT_PER_TRADE}% "
                                 f"-> {lots} lots x {LOT_SIZE} (cap {MAX_LOTS})")
                    else:
                        lots = 1
                        log.warning("AUTO-LOT: capital unavailable, falling back to 1 lot")
                else:
                    lots = MAX_LOTS
                entry_qty = LOT_SIZE * lots

                log.info(f"Red bar trigger ({sig['signal']}, {sig['reason']}). "
                         f"{'[SHADOW] would place' if DRY_RUN else 'Placing'} BUY order "
                         f"for {opt_symbol} (qty={entry_qty})...")
                if DRY_RUN:
                    entry_fill_price = entry_opt_price
                else:
                    order_resp = client.placeorder(strategy=STRATEGY_NAME, symbol=opt_symbol,
                                                   action="BUY", exchange=opt_exchange,
                                                   price_type="MARKET", product=PRODUCT,
                                                   quantity=entry_qty)
                    log.info(f"Entry order response: {order_resp}")

                    if not (isinstance(order_resp, dict) and order_resp.get("status") == "success"):
                        release_symbol_lock(opt_symbol, STRATEGY_NAME)
                        release_direction_lock(UNDERLYING, STRATEGY_NAME, sig["signal"])
                        last_entry_candle_fp = sig["candle_fp"]
                        time.sleep(15)
                        continue

                    entry_fill_price = fetch_fill_price(order_resp.get("orderid"), opt_symbol)
                if entry_fill_price and entry_opt_price:
                    log.info(f"Entry fill {entry_fill_price} vs quote {entry_opt_price} -> "
                             f"slippage {(entry_fill_price - entry_opt_price) / entry_opt_price * 1e4:+.0f} bps")
                # Stop maths uses the fill when we have it, the pre-trade quote otherwise.
                effective_entry = entry_fill_price or entry_opt_price
                sl_oid, sl_trigger = place_premium_sl(opt_symbol, opt_exchange,
                                                      entry_qty, effective_entry)

                state = "IN_TRADE"
                trades_today += 1
                active_trade = {
                    "symbol": opt_symbol, "direction": sig["signal"],
                    "entry_spot": sig["entry_spot"], "sl_spot": sig["sl_spot"],
                    "t1_spot": sig["t1_spot"], "target_spot": sig["target_spot"],
                    "qty_open": entry_qty, "qty_initial": entry_qty,
                    "entry_opt_price": entry_opt_price, "entry_fill_price": entry_fill_price,
                    "entry_price_effective": effective_entry,
                    "opt_sl_orderid": sl_oid, "opt_sl_price": sl_trigger,
                    "entry_time": now.isoformat(), "partial_done": False,
                    "partial_attempts": 0, "realised_pnl": 0.0,
                }
                _active_trade = active_trade
                _day_state = save()
                last_entry_candle_fp = sig["candle_fp"]
                log.info(f"Entered Trade! Spot Entry: {sig['entry_spot']:.2f} | "
                         f"SL: {sig['sl_spot']:.2f} | T1: {sig['t1_spot']:.2f} | "
                         f"Target: {sig['target_spot']:.2f} | Opt entry: {effective_entry} | "
                         f"Prem SL: {sl_trigger}")
                time.sleep(5)

            elif state == "DONE":
                time.sleep(300)

        except Exception as e:
            log.error(f"Error in strategy loop: {e}")
            time.sleep(15)


if __name__ == "__main__":
    run_strategy()
