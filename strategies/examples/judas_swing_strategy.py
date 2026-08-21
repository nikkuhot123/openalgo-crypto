#!/usr/bin/env python
"""
Autonomous Judas Swing (Opening-Range Sweep-Reversal) Strategy
ICT "Judas Swing" adapted for Indian index options: the session opens, price makes
a false break of the opening range (sweeps liquidity/stops), then reverses. This
strategy builds the opening range (09:15-09:45), detects a sweep beyond it, and on
the reversal candle (close back inside the range) buys the OPPOSITE option:
  - sweep above OR-high  -> false bullish break -> buy PE (real move down)
  - sweep below OR-low   -> false bearish break -> buy CE (real move up)
SL = the sweep extreme (spot), target = RR x risk. One trade per day. EOD exit 15:15.
Monitors spot index for exits (broker-agnostic).
"""
import os
import sys
import signal
import json
import time
import logging
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path
import pandas as pd
from openalgo import api

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Read credentials and endpoints from environment
api_key = os.getenv('OPENALGO_API_KEY')
host    = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
ws_url  = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')

if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=api_key, host=host, ws_url=ws_url)

# Strategy Parameters
STRATEGY_NAME = "Judas Swing"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = os.getenv('PRODUCT', 'MIS')
QUANTITY = int(os.getenv('QUANTITY', '0'))  # 0 = auto-detect from exchange
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_SIZE = QUANTITY  # Will be updated at startup if auto-detected
LOT_MODE = os.getenv('LOT_MODE', 'manual').lower()  # 'manual' or 'auto'
RISK_PCT_PER_TRADE = float(os.getenv('RISK_PCT_PER_TRADE', '1.0'))

# Exchange mapping
_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

def _index_exchange(underlying: str) -> str:
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"

def _option_exchange(underlying: str) -> str:
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"

# Strategy Constants — per-underlying backtested winners, env vars override.
#   NIFTY : ATM  strikes, full sweep window (≤12:00), entry ≤14:00, RR 2.0
#   SENSEX: ITM1 strikes, early sweep window (≤10:30), entry ≤13:00, RR 2.0
_IS_BSE = UNDERLYING.upper() in _BSE_UNDERLYINGS
_DEF_STRIKE = 'ITM1' if _IS_BSE else 'ATM'
_DEF_SWEEP_H, _DEF_SWEEP_M = (10, 30) if _IS_BSE else (12, 0)
_DEF_ENTRY_H, _DEF_ENTRY_M = (13, 0) if _IS_BSE else (14, 0)
STRIKE_OFFSET = os.getenv('STRIKE_OFFSET', _DEF_STRIKE)  # 'ATM', 'ITM1', 'ITM2', 'OTM1', 'OTM2'
OR_END = dtime(int(os.getenv('OR_END_HOUR', '9')), int(os.getenv('OR_END_MIN', '45')))
SWEEP_END = dtime(int(os.getenv('SWEEP_END_HOUR', str(_DEF_SWEEP_H))), int(os.getenv('SWEEP_END_MIN', str(_DEF_SWEEP_M))))
ENTRY_END = dtime(int(os.getenv('ENTRY_END_HOUR', str(_DEF_ENTRY_H))), int(os.getenv('ENTRY_END_MIN', str(_DEF_ENTRY_M))))
# CAS (SEBI circular 2026-01-16, live 2026-08-03): the cash index spot stops
# updating at 15:15 and then teleports on the ~15:28 auction stamp (NIFTY moved
# +200.95 pts in one tick on Aug 3). SL/target here are evaluated against spot,
# so the squareoff must complete while spot is still live. Options trade to
# 15:40, so a 15:10 market exit fills normally.
EXIT_TIME = dtime(*(int(x) for x in os.getenv('EXIT_TIME', '15:10').split(':')))
RR = float(os.getenv('RR', '2.0'))  # reward:risk target multiple (both indices: 2.0)
# Break-even ratchet: once the trade shows this many R of open profit, the stop
# moves to entry and stays there. 0 disables it.
# Evidence (backtesting/haema_signal/judas_trail.py, 25 live round trips
# 2026-07-14..08-06 replayed on 1-minute index bars):
#     current        mean +0.189R   median -0.14R   worst -1.80R
#     BE at 1.0R     mean +0.332R   median  0.00R   worst -1.00R
#     trail 0.5R     mean +0.009R   <- trailing CAPS the 2.6-3.75R runners
#     half at 1.0R   mean +0.241R   <- needs 2x premium capital
# Paired improvement of BE-1.0R over current: +0.143R, 95% CI [+0.020, +0.294],
# better on 16/25 trades. Only 4/25 trades ever reach the 2R target, which is
# why protecting the median 0.60R excursion matters more than chasing it.
BE_ARM_R = float(os.getenv('BE_ARM_R', '1.0'))

# ---- resting DISASTER stop at the broker -----------------------------------
# Judas's real stop is a SPOT level held in-process, checked every ~5s. A crash
# or SIGKILL between polls leaves the position unprotected -- POV's 2026-07-02
# incident is the realised cost of that (3 SENSEX PE legs, 3+ hours, -75-80%).
#
# A resting broker order cannot watch spot, only premium, and the measured
# spot->premium mapping is far too unstable to translate the real stop into one:
# realised |dPrem/dSpot| ranges 0.019-0.856 across 8 live contracts (a 46x
# spread, one with R^2 = 0.00), mis-placing a translated stop by a median 14.3%
# of premium. See wiki/research/judas_broker_stop.md.
#
# So this is NOT the strategy's stop. It is a wide backstop that only fires when
# nothing else can. Measured over 1,338 live (spot, premium) samples, the worst
# adverse premium excursion on any normally-managed trade was -48.6% and the
# median -14.7%; a level at -40% would have pre-empted the real stop on 2 of 8
# contracts, -50% clears the worst by only 1.4pp, and -60% clears it by 11.4pp.
# Hence -60%: on this evidence it never fires in normal operation, so its
# expected cost is ~zero, and it converts an 80-100% tail into a 60% one.
DISASTER_STOP_PCT = float(os.getenv('DISASTER_STOP_PCT', '60'))
# Stop-LIMIT, never SL-M: SL-M is rejected outright for options (measured 33/33
# on POV) and the API used to report that as success with orderid=null, silently
# leaving positions with no stop at all.
SL_LIMIT_BUFFER_PCT = float(os.getenv('SL_LIMIT_BUFFER_PCT', '5'))
# Circuit breaker config
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
DAILY_LOSS_LIMIT_RS = float(os.getenv('DAILY_LOSS_LIMIT_RS', '10000'))
# Minimum stop distance as % of spot. Judas' raw stop is the sweep extreme,
# which on a tight opening range sits inside the index's noise: live stops of
# 5.4-5.9 pts (0.022-0.025%) were taken out in 12-42 seconds on 2026-07-21 and
# 2026-07-28. Floors the STOP only — the target keeps using the RAW risk, since
# scaling the target with the floored risk is strictly worse (validated on the
# HA-EMA analog: -6.16% scaled vs -2.03% unscaled, same window).
MIN_SL_PCT = float(os.getenv('MIN_SL_PCT', '0.10'))
# Round-trip statutory cost as % of option premium turnover. Brokerage is zero
# on Flattrade, STT/exchange/GST/SEBI/stamp are not. 0.12% matches Flattrade's
# own calculator (Rs 103.01 on Rs 84,000 turnover = 12.3 bps).
OPT_COST_PCT = float(os.getenv('OPT_COST_PCT', '0.12'))

# ── Entry geometry gate (added 2026-08-05 from the live record) ──
# Six live trades: 7 stop-losses, 1 target, -Rs 1,990 GROSS. Two defects made
# that arithmetically unavoidable rather than unlucky:
#
# 1. The target could sit BELOW the break-even distance, so a winning trade
#    still lost money. 2026-07-29 SENSEX 10:17 targeted 15.3 pts when
#    break-even needed 100.7.
# 2. MIN_SL_PCT floors the STOP but the target keeps using raw_risk, so when
#    raw_risk << floor the reward:risk silently inverts. Same trade: stop
#    floored to 77.6 pts against a 15.3 pt target = 0.20:1, while RR reads 2.0.
#
# Both are geometry known BEFORE the order is sent, so refuse the trade rather
# than pay the spread to discover it. These are structural guards, not fitted
# parameters - 6 trades cannot calibrate anything.
MIN_TARGET_VS_BE = float(os.getenv('MIN_TARGET_VS_BE', '1.5'))   # a win must clear friction
MIN_EFFECTIVE_RR = float(os.getenv('MIN_EFFECTIVE_RR', '1.2'))   # after stop flooring
ASSUMED_DELTA = float(os.getenv('ASSUMED_DELTA', '0.5'))         # ATM; conservative for ITM1
# Option bid/ask as % of premium. Measured 2026-08-05 NIFTY11AUG2624600CE:
# 0.55 spread on 132.95 premium = 0.41%. This DOMINATES statutory charges.
SPREAD_PCT_OF_PREMIUM = float(os.getenv('SPREAD_PCT_OF_PREMIUM', '0.5'))
# Skip entries on the traded weekly's own expiry day, for these underlyings.
# NIFTY (Tue) DTE-0 ATM premium is ~1/3 of a normal day's, so gamma turns a
# 0.025% adverse move into ~19% premium loss. Not applied to SENSEX (its Thu
# expiry is its best day in live logs). Validated on the HA-EMA analog only.
SKIP_EXPIRY_DAY_UNDERLYINGS = {
    s.strip().upper() for s in os.getenv('SKIP_EXPIRY_DAY_UNDERLYINGS', 'NIFTY').split(',') if s.strip()
}

# Symbol lock dir (shared across all strategies on this host)
LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)

# Locks must never wedge. Judas had NO staleness check at all: a leaked lock --
# and release only runs on the normal exit paths, so a crash or an unusual branch
# leaks one -- silently blocked every future entry on that contract, forever.
# POV found 9 orphaned .lock files sitting in this directory, some on
# already-expired contracts, which is what prompted its own TTL.
LOCK_TTL_MIN = float(os.getenv('LOCK_TTL_MIN', '360'))   # one session


def _pid_alive(pid):
    """True if the process still exists. Unknown -> assume alive, never steal."""
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
    """Stale if written on an earlier session, past its TTL, or its owner died."""
    if pid and _pid_alive(pid):
        try:
            age = (datetime.now() - datetime.fromisoformat(str(ts_str))).total_seconds()
        except (ValueError, TypeError):
            age = None
        if age is not None and age < 86400:
            return False        # live owner, under a day old -- a real claim
    try:
        when = datetime.fromisoformat(str(ts_str))
    except (ValueError, TypeError):
        return True                                  # unparseable -> reclaim
    if when.date() != date.today():
        return True                                  # previous session
    if (datetime.now() - when).total_seconds() / 60.0 > LOCK_TTL_MIN:
        return True
    if pid and not _pid_alive(pid):
        return True                                  # owner process gone
    return False


def _read_lock(path):
    """(owner, iso_ts, pid) from a lock file, in EITHER convention.

    Two formats coexist in this directory:
      pipe  "owner|iso|pid"                      -- POV, Judas, Renko
      JSON  {"strategy":..,"ts":..,"pid":..}     -- PDH-PDL EMA

    Judas's old reader took `split("|", 1)[0]` and compared it to its own name,
    so it never stole a foreign lock -- but it also never expired one.
    """
    try:
        raw = path.read_text().strip()
    except Exception:
        return None, "", 0
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return (str(d.get("strategy") or ""), str(d.get("ts") or ""),
                    int(d.get("pid") or 0))
        except Exception:
            return None, "", 0
    parts = raw.split("|")
    ts = parts[1] if len(parts) > 1 else ""
    pid = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
    return (parts[0] if parts else ""), ts, pid


def acquire_symbol_lock(symbol, strategy_name):
    """Claim one CONTRACT. True if acquired, already ours, or the holder's is stale."""
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    if lock_file.exists():
        owner, ts, pid = _read_lock(lock_file)
        if owner is None:
            log.warning(f"Unreadable contract lock on {symbol} — standing aside")
            return False
        if owner == strategy_name:
            return True
        if not _lock_is_stale(ts, pid):
            log.info(f"CONTRACT LOCKED: {symbol} held by '{owner}' — standing aside")
            return False
        log.warning(f"Stale contract lock on {symbol} (owner '{owner}') — reclaiming")
    try:
        # pid included so siblings can tell a live holder from a dead one
        lock_file.write_text(
            f"{strategy_name}|{datetime.now().isoformat()}|{os.getpid()}")
        return True
    except Exception:
        return False

def release_symbol_lock(symbol, strategy_name):
    """Release the lock if we own it."""
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists():
            owner = lock_file.read_text().split("|", 1)[0]
            if owner == strategy_name:
                lock_file.unlink()
    except Exception:
        pass

# ── Position state persistence (survive restarts with full SL/target context) ──
# Bug 2026-07-13: a service restart orphaned an open position; old adoption only
# knew entry price -> "EOD-exit-only". The snapshot lets a restart re-arm SL/target.
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"judas_swing_{UNDERLYING.upper()}.json"

def persist_trade(trade):
    """Snapshot the active trade to disk ({} when flat)."""
    try:
        STATE_FILE.write_text(json.dumps(trade or {}))
    except Exception as e:
        log.debug(f"persist_trade failed: {e}")

def load_persisted_trade():
    """Load the trade snapshot from a previous run. {} when absent/corrupt."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.warning(f"load_persisted_trade failed: {e}")
    return {}

def persist_done(day):
    """Record that the one permitted trade for `day` is finished.

    `state = "DONE"` lives only in memory. Before this, a mid-session restart
    reloaded an empty snapshot, came up IDLE, and could open a SECOND position
    on a day Judas had already traded -- breaking the one-trade-per-day rule
    that the entry logic and every backtest of it rest on. The 2026-08-19
    external-close fix makes that reachable: standing down now clears the trade
    and leaves nothing behind to say why.
    """
    try:
        STATE_FILE.write_text(json.dumps({"done_date": day.isoformat()}))
    except Exception as e:
        log.debug(f"persist_done failed: {e}")

def load_done_date():
    """The date Judas last finished trading for, or None."""
    try:
        raw = (load_persisted_trade() or {}).get("done_date")
        if raw:
            return date.fromisoformat(str(raw))
    except Exception as e:
        log.debug(f"load_done_date failed: {e}")
    return None

def reconcile_orphan_position(underlying):
    """Check positionbook for an open position matching this underlying. Returns adopted trade dict or None."""
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return None
        for pos in pb.get("data", []):
            qty = int(pos.get("quantity", 0) or 0)
            sym = pos.get("symbol", "") or ""
            if qty != 0 and underlying.upper() in sym.upper():
                direction = "CE" if "CE" in sym.upper() else "PE" if "PE" in sym.upper() else "UNKNOWN"
                return {
                    "symbol": sym,
                    "direction": direction,
                    "qty": abs(qty),
                    "entry_price": float(pos.get("average_price", 0) or 0),
                    "adopted": True,
                }
    except Exception as e:
        log.debug(f"Reconcile failed: {e}")
    return None

def live_position_qty(underlying, symbol):
    """Broker's current qty on `symbol`: >0 held, 0 absent, None if unverifiable."""
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

def fetch_available_capital():
    """Query funds API for current available cash. Returns float or None."""
    try:
        resp = client.funds()
        if isinstance(resp, dict) and resp.get("status") == "success":
            data = resp.get("data", {})
            cash = data.get("availablecash")
            if cash is not None:
                return float(cash)
    except Exception as e:
        log.warning(f"Failed to fetch capital: {e}")
    return None

def compute_auto_lots(capital, risk_pct, max_loss_per_unit, lot_size, hard_cap_lots):
    """Compute lot count from risk budget. max_loss_per_unit is in rupees per single contract."""
    if max_loss_per_unit <= 0 or lot_size <= 0:
        return 1
    risk_budget = capital * (risk_pct / 100.0)
    max_loss_per_lot = max_loss_per_unit * lot_size
    if max_loss_per_lot <= 0:
        return 1
    auto_lots = int(risk_budget / max_loss_per_lot)
    return max(1, min(auto_lots, hard_cap_lots))

def fetch_option_ltp(opt_symbol, opt_exchange, underlying_ltp=None, max_retries=3, retry_delay=1.0):
    """Fetch option LTP with sanity check against underlying spot.

    Brokers (notably Shoonya) can return the underlying spot value when the
    option symbol's tick cache isn't populated yet (first quote after subscription).
    Validates that the returned LTP isn't suspiciously close to the spot price.

    Returns: float LTP on success, None on persistent failure.
    """
    for attempt in range(max_retries):
        try:
            q = client.quotes(symbol=opt_symbol, exchange=opt_exchange)
            if q.get("status") == "success":
                ltp = float(q["data"]["ltp"])
                # Option premium for indices is virtually never > 20% of spot.
                # If we got back a value close to spot, it's a stale leak — retry.
                if underlying_ltp is None or ltp < underlying_ltp * 0.2:
                    return ltp
                log.warning(f"Option LTP {ltp:.2f} suspiciously close to spot {underlying_ltp:.2f} for {opt_symbol}; retry {attempt+1}/{max_retries}")
        except Exception as e:
            log.warning(f"Option LTP fetch failed for {opt_symbol}: {e}; retry {attempt+1}/{max_retries}")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.error(f"Failed to get valid option LTP for {opt_symbol} after {max_retries} attempts")
    return None

def get_nearest_expiry(underlying, exchange):
    try:
        resp = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="options")
        if resp.get("status") == "success":
            expiries = resp.get("data", [])
            if expiries:
                return expiries[0].replace("-", "")
    except Exception as e:
        log.error(f"Error fetching expiry: {e}")
    return None

def get_option_symbol(underlying, exchange, expiry, offset, option_type):
    try:
        resp = client.optionsymbol(
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry,
            offset=offset,
            option_type=option_type
        )
        if resp.get("status") == "success":
            return resp.get("symbol")
    except Exception as e:
        log.error(f"Error fetching optionsymbol: {e}")
    return None

def is_expiry_today(expiry_str):
    """True when `expiry_str` ('28JUL26' from get_nearest_expiry) is today."""
    if not expiry_str:
        return False
    try:
        return datetime.strptime(expiry_str.upper(), "%d%b%y").date() == date.today()
    except (ValueError, TypeError):
        return False

def fetch_lot_size(underlying, idx_exchange, opt_exchange):
    """Actual contract lot size, or None if it genuinely cannot be determined.

    TWO independent sources, because relying on one produced invalid orders on
    2026-08-12: optionchain returned
        404 "No strikes found for NIFTY expiring 18-AUG-26 ... update master
        contract"
    all session, on BOTH indices, even though the master held 462 CE rows for
    that very expiry. Detection fell through to a hardcoded guess and every
    order that day was rejected with "Quantity must be in multiples of lot
    size". symbol() kept answering correctly (lotsize 65) throughout, so it is
    now the second source rather than a guess.
    """
    expiry = None
    try:
        expiry = get_nearest_expiry(underlying, opt_exchange)
        if expiry:
            resp = client.optionchain(
                underlying=underlying, exchange=idx_exchange,
                expiry_date=expiry, strike_count=1
            )
            if resp.get("status") == "success":
                for item in resp.get("chain", []):
                    ce = item.get("ce") or {}
                    if ce.get("lotsize"):
                        return int(ce["lotsize"])
                    pe = item.get("pe") or {}
                    if pe.get("lotsize"):
                        return int(pe["lotsize"])
            else:
                log.warning("optionchain lot-size lookup failed: %s",
                            str(resp.get("message"))[:120])
    except Exception as e:
        log.warning(f"optionchain lot-size lookup raised: {e}")

    # Source 2: optionsymbol returns lotsize at the TOP LEVEL of its response,
    # alongside the symbol. This is the same endpoint the strategy already
    # calls successfully on every cycle to resolve its leg, so if the strategy
    # can trade at all, this can size it. One call, no extra dependency.
    try:
        if expiry:
            resp = client.optionsymbol(
                underlying=underlying, exchange=opt_exchange,
                expiry_date=expiry, offset="ATM", option_type="CE",
            )
            if resp.get("status") == "success" and resp.get("lotsize"):
                log.info("lot size via optionsymbol(%s) = %s",
                         resp.get("symbol"), resp["lotsize"])
                return int(resp["lotsize"])
    except Exception as e:
        log.warning(f"optionsymbol lot-size lookup raised: {e}")
    return None


# Tape-reading context, ported from openmtops narrative.py `_action_label`.
QUAD_WINDOW = int(os.getenv("QUAD_WINDOW", "15"))   # 1m candles to look back
QUAD_OI_MIN_PCT = 1.0                               # upstream OI_MILD_PCT
QUAD_PRICE_MIN_PCT = 1.0                            # upstream PRICE_SIG_PCT / 3


def oi_price_quadrant(df, window=None):
    """Positioning read: OI change x price change over `window` candles.

        OI up   + price up   -> long buildup    (new longs paying up)
        OI up   + price down -> fresh writing   (new shorts, sellers in control)
        OI down + price up   -> short covering  (shorts buying back = squeeze)
        OI down + price down -> long unwinding  (longs giving up)

    DIAGNOSTIC ONLY -- never gates a trade. Recorded at entry so the give-back
    study has a covariate to explain outcomes against. Judas reads SPOT
    structure and otherwise never inspects the option's own book, so this is
    the only place that positioning context is captured.

    Returns (label_or_None, d_oi_pct, d_price_pct). Never raises.
    """
    w = QUAD_WINDOW if window is None else window
    try:
        if df is None or len(df) < w + 1 or "oi" not in df.columns:
            return None, 0.0, 0.0
        oi_now, oi_then = float(df["oi"].iloc[-1]), float(df["oi"].iloc[-1 - w])
        px_now, px_then = float(df["close"].iloc[-1]), float(df["close"].iloc[-1 - w])
        if oi_then <= 0 or px_then <= 0:
            return None, 0.0, 0.0
        d_oi = (oi_now - oi_then) / oi_then * 100.0
        d_px = (px_now - px_then) / px_then * 100.0
        if abs(d_oi) < QUAD_OI_MIN_PCT or abs(d_px) < QUAD_PRICE_MIN_PCT:
            return None, d_oi, d_px
        if d_oi > 0:
            return ("long_buildup" if d_px > 0 else "fresh_writing"), d_oi, d_px
        return ("short_covering" if d_px > 0 else "long_unwinding"), d_oi, d_px
    except Exception:
        return None, 0.0, 0.0


# Consecutive positionbook misses before an open trade is declared externally
# closed. One miss is not proof -- assuming it was cancelled the stops on two
# live POV legs on 2026-08-14.
EXT_CLOSE_MISS_LIMIT = int(os.getenv("EXT_CLOSE_MISS_LIMIT", "3"))
_ext_close_miss = [0]

# How often the open position is reconciled against the broker, in seconds.
# With MISS_LIMIT 3 an undetermined entry is resolved in ~90s worst case.
RECON_SECS = float(os.getenv("RECON_SECS", "30"))
_last_recon = [0.0]


def detect_external_close(active_trade, underlying, symbol):
    """Was this position closed by someone other than this strategy?

    Returns (True, reason) only on positive evidence. Judas never places a
    resting stop, so the only broker-side records are the entry it placed and
    whatever closed the position. The tests:

      entry order rejected/cancelled -> it never became a position
      broker flat, entry complete    -> something else closed it (manual
                                        square-off, broker action)
      broker flat, entry unreadable  -> require EXT_CLOSE_MISS_LIMIT misses
      broker holds qty > 0           -> still open, reset the counter
      broker unverifiable            -> no decision at all
    """
    if not active_trade or not symbol:
        return False, ""
    qty = live_position_qty(underlying, symbol)
    if qty is None:
        return False, ""            # cannot verify -- decide nothing
    if qty > 0:
        _ext_close_miss[0] = 0
        return False, ""

    entry_state = order_state(active_trade.get("entry_orderid"), f"entry {symbol}")
    if entry_state in ("rejected", "cancelled"):
        _ext_close_miss[0] = 0
        return True, f"entry {entry_state}"
    if entry_state == "complete":
        _ext_close_miss[0] = 0
        return True, "broker flat, entry was filled"

    _ext_close_miss[0] += 1
    if _ext_close_miss[0] >= EXT_CLOSE_MISS_LIMIT:
        n = _ext_close_miss[0]
        _ext_close_miss[0] = 0
        return True, f"broker flat {n}x, entry state undetermined"
    log.warning("%s absent from positionbook (%s/%s) and entry state undetermined — holding.",
                symbol, _ext_close_miss[0], EXT_CLOSE_MISS_LIMIT)
    return False, ""


def order_state(order_id, context=""):
    """'complete' | 'rejected' | 'cancelled' | 'pending', or None if unreadable."""
    if not order_id:
        return None
    try:
        r = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
        d = (r.get("data") or {}) if isinstance(r, dict) else {}
        st = str(d.get("order_status", "")).strip().lower()
        if st in ("complete", "completed", "filled"):
            return "complete"
        if st == "rejected":
            return "rejected"
        if st in ("cancelled", "canceled"):
            return "cancelled"
        if st:
            return "pending"
    except Exception as e:
        log.debug(f"order_state({order_id}) {context} failed: {e}")
    return None


def find_external_exit_price(symbol, active_trade):
    """Average price of the SELL that closed us, per the broker's own books.

    An externally-closed trade has no exit order in this strategy's records, so
    the price has to come from the broker. Tradebook first (actual fills), then
    the orderbook. Returns None when neither can supply it -- the caller still
    closes out tracking, because a permanent ghost is worse than an unpriced
    trade.
    """
    want = str(symbol).upper()
    for source, fn in (("tradebook", client.tradebook), ("orderbook", client.orderbook)):
        try:
            r = fn()
            if not isinstance(r, dict) or r.get("status") != "success":
                continue
            data = r.get("data") or {}
            rows = data.get("orders", data) if isinstance(data, dict) else data
            for row in rows or []:
                if str(row.get("symbol", "")).upper() != want:
                    continue
                if str(row.get("action", "")).upper() != "SELL":
                    continue
                if str(row.get("order_status", "complete")).lower() not in ("complete", "completed", "filled"):
                    continue
                for key in ("average_price", "averageprice", "price", "tradeprice"):
                    try:
                        px = float(row.get(key) or 0)
                    except (TypeError, ValueError):
                        px = 0.0
                    if px > 0:
                        log.info("External exit price for %s from %s: %.2f", symbol, source, px)
                        return px
        except Exception as e:
            log.debug(f"find_external_exit_price {source} failed: {e}")
    return None

def statutory_cost(entry_px, exit_px, qty):
    """Round-trip statutory cost in rupees for an option BUY->SELL.

    Judas reported GROSS P&L until 2026-08-05: trade_pnl was simply
    (exit_ltp - entry_px) * qty with no costs at all. Across the first six
    live trades that understated the loss by roughly Rs 1,050 per trade, and
    it fed the circuit breakers a flattering number - DAILY_LOSS_LIMIT_RS was
    metering against a loss ~4x smaller than the real one.
    """
    if entry_px is None or exit_px is None or not qty:
        return 0.0
    turnover = (float(entry_px) + float(exit_px)) * float(qty)
    return turnover * OPT_COST_PCT / 100.0


def safe_cancel_order(order_id, context=""):
    """Cancel an order, treating 'already terminal' as success.

    Brokers error when cancelling an order that already filled or was rejected,
    but that IS the desired end state -- it is no longer active. Mapping those to
    a clean no-op is what lets callers audit-log honestly.
    """
    if not order_id:
        return True, "no order id"
    try:
        resp = client.cancelorder(order_id=order_id, strategy=STRATEGY_NAME)
    except Exception as e:
        return False, f"cancelorder threw: {e}"
    if not isinstance(resp, dict):
        return True, f"non-dict response (assumed ok): {resp}"
    if resp.get("status") == "success":
        return True, "cancelled"
    msg = str(resp.get("message", "")).lower()
    if any(w in msg for w in ("complete", "cancel", "reject", "not found", "traded")):
        return True, f"already terminal: {resp.get('message')}"
    return False, str(resp.get("message") or resp)


def place_disaster_stop(opt_symbol, opt_exchange, entry_premium, qty):
    """Rest a WIDE stop-LIMIT at the broker as a crash backstop.

    This is deliberately NOT the strategy's stop -- see DISASTER_STOP_PCT. It
    exists only for the case where this process is gone and the in-process spot
    stop cannot run. Returns the order id, or None.
    """
    if DISASTER_STOP_PCT <= 0 or not entry_premium or entry_premium <= 0:
        return None
    # Guard on the RAW value, before tick rounding. A 0.10 entry premium gives a
    # raw trigger of 0.04, which rounds UP to exactly 0.05 and would slip past a
    # post-rounding check -- arming a meaningless "sell at almost zero" stop that
    # can never protect anything.
    raw = float(entry_premium) * (1.0 - DISASTER_STOP_PCT / 100.0)
    if raw < 0.10:
        log.info("disaster stop skipped: entry premium %.2f too small for a "
                 "%.0f%% trigger (raw %.3f)", entry_premium, DISASTER_STOP_PCT, raw)
        return None
    trg = round(round(raw / 0.05) * 0.05, 2)
    lim = max(0.05, round(round(trg * (1.0 - SL_LIMIT_BUFFER_PCT / 100.0)
                                / 0.05) * 0.05, 2))
    try:
        resp = client.placeorder(
            strategy=STRATEGY_NAME, symbol=opt_symbol, action="SELL",
            exchange=opt_exchange, price_type="SL", trigger_price=trg,
            price=lim, product=PRODUCT, quantity=qty)
    except Exception as e:
        log.error("disaster stop placement threw: %s", e)
        return None
    oid = resp.get("orderid") if isinstance(resp, dict) else None
    if isinstance(resp, dict) and resp.get("status") == "success" and oid:
        log.info("DISASTER STOP armed on %s: trigger %.2f limit %.2f (-%.0f%% of "
                 "entry %.2f) order %s -- backstop only, the real stop is spot %s",
                 opt_symbol, trg, lim, DISASTER_STOP_PCT, entry_premium, oid, "in-process")
        return oid
    # An unarmed backstop must be loud: silence here is how a position ends up
    # with no protection at all while the log looks healthy.
    log.warning("DISASTER STOP NOT ARMED on %s: %s", opt_symbol, resp)
    return None

def fetch_fill_price(order_id, max_retries=4, retry_delay=1.0):
    """Average TRADED price for an order, so P&L rests on real fills.

    Quote-derived P&L excludes slippage: it prices the exit at whatever LTP we
    happened to observe rather than what the broker actually filled.
    Returns float, or None so the caller can fall back to the quote.
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
            log.debug(f"orderstatus {order_id} attempt {attempt + 1}: {e}")
        try:
            tb = client.tradebook()
            if isinstance(tb, dict) and tb.get("status") == "success":
                for t in tb.get("data", []) or []:
                    if str(t.get("orderid", "")) == str(order_id):
                        for k in ("average_price", "averageprice", "price", "tradeprice"):
                            v = t.get(k)
                            if v not in (None, "", 0, "0") and float(v) > 0:
                                return float(v)
        except Exception:
            pass
        time.sleep(retry_delay)
    log.warning(f"Could not resolve fill price for order {order_id} — falling back to quote")
    return None


def breakeven_points(opt_premium, qty):
    """Index points the spot must travel for the trade to reach exactly zero.

    Two components, both premium-denominated:
      statutory  OPT_COST_PCT of premium turnover (Flattrade-validated)
      spread     crossing the option bid/ask, which DOMINATES - measured
                 2026-08-05 on NIFTY11AUG2624600CE: 0.55 on a 132.95 premium
                 = 0.41%, i.e. Rs 35.75 of spread vs Rs 20.74 of charges.

    Cost is charged per round trip and does not scale with distance travelled,
    so it converts into a fixed number of index points to cover before any
    profit exists. For NIFTY that is ~1.7 pts, NOT the ~34 pts a notional-based
    model wrongly produced: options are charged on premium turnover, not on
    spot x lot, and those differ by ~37x.
    """
    if not opt_premium or not qty:
        return None
    statutory = statutory_cost(opt_premium, opt_premium, qty)
    spread = float(opt_premium) * (SPREAD_PCT_OF_PREMIUM / 100.0) * float(qty)
    denom = ASSUMED_DELTA * float(qty)
    return (statutory + spread) / denom if denom else None


def check_entry_geometry(entry_spot, sl_spot, target_spot, opt_premium, qty):
    """Reject trades whose geometry is broken. Returns (ok, reason, detail).

    The binding guard here is the effective reward:risk, not the cost floor.
    Friction is ~1.7 index pts on NIFTY while stops run 25-98 pts, so cost is
    NOT what breaks these trades. What does break them is MIN_SL_PCT flooring
    the stop while the target keeps using raw_risk, which silently inverts the
    reward:risk the strategy believes it has taken.
    """
    stop_d = abs(entry_spot - sl_spot)
    tgt_d = abs(target_spot - entry_spot)
    be = breakeven_points(opt_premium, qty)
    eff_rr = (tgt_d / stop_d) if stop_d > 0 else 0.0
    detail = (f"stop {stop_d:.1f}pt | target {tgt_d:.1f}pt | "
              f"break-even {be:.1f}pt | effective RR {eff_rr:.2f}"
              if be else f"stop {stop_d:.1f}pt | target {tgt_d:.1f}pt | effective RR {eff_rr:.2f}")

    if be and tgt_d < MIN_TARGET_VS_BE * be:
        return False, (f"target {tgt_d:.1f}pt < {MIN_TARGET_VS_BE}x break-even "
                       f"({be:.1f}pt) — a WIN would still lose money"), detail
    if eff_rr < MIN_EFFECTIVE_RR:
        return False, (f"effective RR {eff_rr:.2f} < {MIN_EFFECTIVE_RR} — stop floor "
                       f"(MIN_SL_PCT={MIN_SL_PCT}%) inverted the reward:risk"), detail
    return True, None, detail

def compute_judas_signal(df_5m, today):
    """Reconstruct the opening-range sweep-reversal signal from intraday 5m candles.

    Uses only COMPLETED candles (drops the currently-forming last candle, mirroring
    the backtest which evaluates on candle close). Live history is OPEN-timestamped
    (the 09:15 row is the 09:15-09:20 bar), whereas the Volrix backtest used close-time
    candleTime. To reproduce the backtest's 30-min opening range (09:15-09:45) we bucket
    by OPEN time with strict '<' comparisons: OR = opens in [09:15, OR_END).

    Returns a status dict ALWAYS (for the live monitor panel):
      {or_high, or_low, swept_high, swept_low, spot, signal, ...}
    where 'signal' is 'CE'/'PE' when a reversal entry triggers, else None.
    Returns None only when there isn't enough data to compute the opening range.
    """
    if not isinstance(df_5m, pd.DataFrame) or len(df_5m) < 3:
        return None
    df = df_5m.sort_index()
    # Drop the currently-forming (partial) candle — evaluate on completed candles only
    df = df.iloc[:-1]
    if df.empty:
        return None
    ts = pd.to_datetime(df.index)
    # Restrict to today's session
    mask = [t.date() == today for t in ts]
    df = df[mask]
    if len(df) < 1:
        return None
    times = [t.time() for t in pd.to_datetime(df.index)]
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()
    opens = df["open"].tolist()

    # 1. Build opening range — OPEN times strictly before OR_END (30-min window)
    or_high = None
    or_low = None
    or_end_idx = -1
    for i, t in enumerate(times):
        if t < OR_END:
            or_high = highs[i] if or_high is None else max(or_high, highs[i])
            or_low = lows[i] if or_low is None else min(or_low, lows[i])
            or_end_idx = i
    if or_high is None or or_low is None:
        return None

    # 2. Detect sweep beyond the OR during the sweep window (candles after OR .. < SWEEP_END)
    swept_high = False
    swept_low = False
    sweep_extreme_high = None
    sweep_extreme_low = None
    for i in range(or_end_idx + 1, len(df)):
        if times[i] < SWEEP_END:
            if highs[i] > or_high:
                swept_high = True
                sweep_extreme_high = highs[i] if sweep_extreme_high is None else max(sweep_extreme_high, highs[i])
            if lows[i] < or_low:
                swept_low = True
                sweep_extreme_low = lows[i] if sweep_extreme_low is None else min(sweep_extreme_low, lows[i])

    # Base status snapshot (always returned so the monitor panel has values)
    last = len(df) - 1
    c_close = closes[last]
    status = {
        "signal": None,
        "or_high": or_high,
        "or_low": or_low,
        "swept_high": swept_high,
        "swept_low": swept_low,
        "spot": c_close,
        "candle_fp": (float(opens[last]), float(highs[last]), float(lows[last]), float(c_close)),
    }

    # 3. Evaluate the latest completed candle for the reversal (close back inside OR)
    if last <= or_end_idx:
        return status
    lt = times[last]
    if lt < OR_END or lt >= ENTRY_END:
        return status

    risk_floor = c_close * (MIN_SL_PCT / 100.0)

    # sweep above -> false bullish break -> reversal down -> buy PE
    if swept_high and c_close < or_high:
        raw_risk = sweep_extreme_high - c_close
        if raw_risk > 0:
            status.update({"signal": "PE", "entry_spot": c_close,
                           "sl_spot": c_close + max(raw_risk, risk_floor),
                           "target_spot": c_close - RR * raw_risk})
            return status
    # sweep below -> false bearish break -> reversal up -> buy CE
    if swept_low and c_close > or_low:
        raw_risk = c_close - sweep_extreme_low
        if raw_risk > 0:
            status.update({"signal": "CE", "entry_spot": c_close,
                           "sl_spot": c_close - max(raw_risk, risk_floor),
                           "target_spot": c_close + RR * raw_risk})
            return status
    return status

# Shutdown state shared between signal handler and run loop
_shutdown_requested = False
_active_trade = {}
_opt_exchange = None
# Premium-path instrumentation. Judas exits on SPOT only and never looked at
# what the position was worth, so once the weekly contract expired the path
# was gone: on 2026-08-07 only 4 of 27 round trips could still be replayed.
PREMIUM_LOG_SECS = int(os.getenv("PREMIUM_LOG_SECS", "30"))
_last_premium_log = 0.0

def _graceful_shutdown(signum, frame):
    """Handle Ctrl+C / SIGTERM: close active position, then exit."""
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    log.info(f"\n{'='*60}")
    log.info(f"SHUTDOWN SIGNAL RECEIVED ({sig_name}) — cleaning up...")
    log.info(f"{'='*60}")

    if _active_trade and _opt_exchange:
        symbol = _active_trade.get("symbol")
        if symbol:
            # CRITICAL: verify broker still has this position OPEN with non-zero qty.
            # Without this check, a stale active_trade dict + repeated restarts can fire
            # repeated SELLs that push us net-short on options we don't own.
            broker_qty = None
            try:
                pb = client.positionbook()
                if isinstance(pb, dict) and pb.get("status") == "success":
                    for pos in pb.get("data", []):
                        if (pos.get("symbol", "") or "").upper() == symbol.upper():
                            broker_qty = int(pos.get("quantity", 0) or 0)
                            break
                    if broker_qty is None:
                        broker_qty = 0  # not in book → flat
            except Exception as e:
                log.error(f"Shutdown: positionbook check failed for {symbol}: {e} — aborting close to avoid naked short")
                release_symbol_lock(symbol, STRATEGY_NAME)
                log.info("Shutdown complete. Exiting.")
                sys.exit(0)

            _d_oid = _active_trade.get("disaster_oid")
            if broker_qty is None:
                # UNKNOWN (positionbook non-success, e.g. app restarting -> 502).
                # Do NOT assume flat; keep lock + state so restart adoption re-arms it.
                # Deliberately LEAVE the resting backstop in place: this is exactly
                # the case it exists for -- we are exiting without knowing whether a
                # position survives us.
                log.error(f"Shutdown: cannot verify {symbol} position — leaving untouched for restart adoption")
                if _d_oid:
                    log.warning("Shutdown: LEAVING disaster stop %s armed — position "
                                "state unknown and this is its whole purpose", _d_oid)
                log.info("Shutdown complete. Exiting.")
                sys.exit(0)
            if broker_qty <= 0:
                log.info(f"Shutdown: broker reports {symbol} qty={broker_qty} — already flat, no SELL")
                # Flat, so a live resting SELL would be a naked short.
                if _d_oid:
                    _ok, _m = safe_cancel_order(_d_oid, f"shutdown flat {symbol}")
                    (log.info if _ok else log.error)("disaster stop cancel: %s", _m)
                release_symbol_lock(symbol, STRATEGY_NAME)
            else:
                close_qty = min(broker_qty, _active_trade.get("qty", QUANTITY))
                log.info(f"Closing active position: {symbol} (broker qty={broker_qty}, closing {close_qty})")
                try:
                    resp = client.placeorder(
                        strategy=STRATEGY_NAME,
                        symbol=symbol,
                        action="SELL",
                        exchange=_opt_exchange,
                        price_type="MARKET",
                        product=PRODUCT,
                        quantity=close_qty
                    )
                    log.info(f"Shutdown exit response: {resp}")
                    # Position closed -> the backstop must go, or it fires naked.
                    if _d_oid:
                        _ok, _m = safe_cancel_order(_d_oid, f"shutdown close {symbol}")
                        (log.info if _ok else log.error)("disaster stop cancel: %s", _m)
                    release_symbol_lock(symbol, STRATEGY_NAME)
                except Exception as e:
                    log.error(f"Failed to close position on shutdown: {e}")
                    if _d_oid:
                        log.warning("close failed — LEAVING disaster stop %s armed", _d_oid)
    else:
        log.info("No active position — nothing to close.")

    log.info("Shutdown complete. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)


def log_premium_path(symbol, opt_exchange, active_trade, underlying_ltp, qty, now=None):
    """Emit one throttled PATH line recording what the position is worth.

    2026-08-07 is the case for it: the 24600PE bought at 127.50 peaked at
    148.50 (+16.5%) at 14:15 and was flattened at 109.70 (-14.0%) at the 15:10
    EOD -- a Rs 2,522 give-back -- while SPOT finished 21 points IN FAVOUR.
    The break-even ratchet armed at 14:12, two minutes before the premium peak,
    and never fired, because it guards spot and the spot stop was never
    touched. Nothing watched what the position was actually worth, and once the
    weekly contract expired the path was unrecoverable: only 4 of 27 round
    trips could still be replayed.

    INSTRUMENTATION ONLY. It must never be able to disturb an exit, so every
    failure is swallowed. Returns True only when a line was actually written,
    which is what makes it testable -- a silent no-op collector would waste the
    whole collection window before anyone noticed.
    """
    global _last_premium_log
    t = time.time() if now is None else now
    if t - _last_premium_log < PREMIUM_LOG_SECS:
        return False
    _last_premium_log = t
    try:
        prem = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)
        entry = (active_trade or {}).get("entry_opt_price")
        if prem is None or not entry:
            return False
        e = float(entry)
        log.info(
            f"PATH {symbol} prem={prem:.2f} entry={e:.2f} "
            f"pct={(prem - e) / e * 100:+.1f}% rs={(prem - e) * qty:+.0f}"
        )
        return True
    except Exception as err:
        log.debug(f"premium path log failed: {err}")
        return False

def run_strategy():
    global _active_trade, _opt_exchange, QUANTITY, LOT_SIZE, _last_premium_log
    log.info(f"Starting Autonomous Judas Swing Strategy for {UNDERLYING}...")
    log.info(f"OR window ≤ {OR_END} | Sweep ≤ {SWEEP_END} | Entry ≤ {ENTRY_END} | Strike {STRIKE_OFFSET} | RR {RR}")
    idx_exchange = _index_exchange(UNDERLYING)
    opt_exchange = _option_exchange(UNDERLYING)
    _opt_exchange = opt_exchange

    # Auto-detect lot size if QUANTITY not explicitly set
    if QUANTITY == 0:
        detected = fetch_lot_size(UNDERLYING, idx_exchange, opt_exchange)
        if detected:
            QUANTITY = detected
            LOT_SIZE = detected
            log.info(f"Auto-detected lot size: {QUANTITY}")
        else:
            # NEVER guess. The old fallback was a hardcoded 75 -- the NIFTY lot
            # size from before the 2025-12-31 change to 65, and never correct
            # for SENSEX (20). On 2026-08-12 detection failed and that guess
            # produced 51 rejected orders across both books:
            #   "Quantity must be in multiples of lot size 65" / "... 20"
            # Analyzer rejects a wrong size outright, which is the benign case.
            # LIVE would risk a wrong-sized REAL position, so stand down instead.
            log.error(
                "Lot size undetectable for %s (both optionchain and symbol() "
                "failed) -- standing down. Set QUANTITY explicitly to override.",
                UNDERLYING,
            )
            sys.exit(1)
    else:
        LOT_SIZE = QUANTITY
        log.info(f"Using configured lot size: {QUANTITY}")

    # Active trade state
    state = "IDLE"
    active_trade = {}
    trade_date = None
    last_entry_candle_fp = None  # (o,h,l,c) of the candle that triggered last entry
    consecutive_losses = 0
    daily_loss_rs = 0.0

    # Adopt orphan position on boot (e.g. after restart while position was open)
    orphan = reconcile_orphan_position(UNDERLYING)
    if orphan:
        saved = load_persisted_trade()
        if saved and saved.get("symbol") == orphan["symbol"] and saved.get("sl_spot") is not None:
            active_trade = dict(saved)
            active_trade["qty"] = orphan["qty"]  # broker is authoritative on qty
            active_trade.pop("adopted", None)
            log.warning(
                f"Adopting position with RESTORED context: {orphan['symbol']} qty={orphan['qty']} "
                f"| SL: {active_trade.get('sl_spot')} | Target: {active_trade.get('target_spot')}")
        else:
            log.warning(f"Adopting unknown orphan (EOD-exit-only): {orphan['symbol']} qty={orphan['qty']} @ {orphan['entry_price']}")
            active_trade = {
                "symbol": orphan["symbol"],
                "direction": orphan["direction"],
                "entry_spot": None,        # unknown — orphan from prior session
                "sl_spot": None,
                "target_spot": None,
                "qty": orphan["qty"],
                "adopted": True,
            }
        _active_trade = active_trade
        state = "IN_TRADE"
        acquire_symbol_lock(orphan["symbol"], STRATEGY_NAME)
        # Re-establish the resting backstop on the ADOPTED position.
        #
        # Without this the protection exists only on the happy path: the
        # restored-context branch carries a `disaster_oid` that may long since
        # have been cancelled or filled, and the unknown-orphan branch builds a
        # fresh dict with no oid at all -- so an adopted position would carry no
        # backstop while the code believed otherwise. POV already does exactly
        # this check for its own SL, and this is the same requirement.
        _d_oid = active_trade.get("disaster_oid")
        _d_live = False
        if _d_oid:
            try:
                _st = client.orderstatus(order_id=_d_oid, strategy=STRATEGY_NAME)
                _os = str(((_st.get("data") or {}).get("order_status") or "")).lower()
                _d_live = (_st.get("status") == "success"
                           and _os in ("open", "trigger pending", "pending"))
            except Exception as _derr:
                log.debug("disaster stop status check failed: %s", _derr)
            log.info("adopted disaster stop %s is %s", _d_oid,
                     "still resting" if _d_live else "NOT live -- re-arming")
        if not _d_live:
            # Price the backstop off whatever entry reference we have. An adopted
            # unknown orphan has only the broker's average, which is the right
            # reference anyway.
            _ref = (active_trade.get("entry_fill_price")
                    or active_trade.get("entry_opt_price")
                    or orphan.get("entry_price"))
            active_trade["disaster_oid"] = place_disaster_stop(
                orphan["symbol"], opt_exchange, _ref, orphan["qty"])
            if not active_trade["disaster_oid"]:
                log.warning("adopted %s has NO resting backstop -- the in-process "
                            "spot stop is its only protection", orphan["symbol"])
        trade_date = date.today()  # seed so the new-day reset does NOT wipe the adopted IN_TRADE state
        persist_trade(active_trade)
    else:
        # Read the day-marker BEFORE touching the snapshot -- persist_trade({})
        # would erase the very record we need. An open position always wins:
        # if the broker holds something we adopt and manage it (above), marker
        # or not.
        _done_on = load_done_date()
        if _done_on == date.today():
            state = "DONE"
            trade_date = date.today()
            log.warning("Already traded today (%s) — standing down for the session. "
                        "Judas is one-trade-per-day; a restart must not open a second.", _done_on)
            persist_done(_done_on)      # keep the marker; do NOT clear it
        else:
            persist_trade({})  # broker holds nothing — clear any stale snapshot

    while True:
        try:
            today = date.today()
            if trade_date != today:
                trade_date = today
                state = "IDLE"
                active_trade = {}
                _active_trade = {}
                persist_trade({})
                last_entry_candle_fp = None
                consecutive_losses = 0
                daily_loss_rs = 0.0
                log.info(f"--- New trading day initialized: {trade_date} ---")

            now = datetime.now()
            current_time = now.time()

            # Fetch Spot Price (LTP)
            quotes_resp = client.quotes(symbol=UNDERLYING, exchange=idx_exchange)
            if not quotes_resp or quotes_resp.get("status") != "success" or "data" not in quotes_resp:
                log.warning(f"Failed to fetch quotes for underlying {UNDERLYING}. Retrying...")
                time.sleep(15)
                continue
            underlying_ltp = float(quotes_resp["data"]["ltp"])

            # State Machine: IN_TRADE (Active Exit Monitoring)
            if state == "IN_TRADE":
                symbol = active_trade["symbol"]
                direction = active_trade["direction"]
                sl_spot = active_trade["sl_spot"]
                target_spot = active_trade["target_spot"]
                qty = active_trade["qty"]

                # Adopted orphans from prior sessions have no SL/target — EOD exit only
                is_adopted = active_trade.get("adopted") and (sl_spot is None or target_spot is None)
                if is_adopted:
                    log.info(f"Monitoring (adopted) Trade: {symbol} | Spot: {underlying_ltp:.2f} | EOD-exit-only")
                else:
                    log.info(f"Monitoring Trade: {symbol} | Spot: {underlying_ltp:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f}")

                log_premium_path(symbol, opt_exchange, active_trade, underlying_ltp, qty)

                # ---- reconcile against the broker --------------------------
                # Judas is otherwise a pure SPOT watcher: live_position_qty()
                # appears nowhere except inside `if exit_triggered`. So a
                # position closed by anything OTHER than this strategy -- a
                # manual square-off, a broker action -- is invisible to it.
                # 2026-08-19: a manual square-off at 11:58 left Judas still
                # logging "Monitoring Trade" at 12:16, holding the symbol lock
                # and blocking the session, and the +Rs 520 never reached the
                # books or the circuit breakers.
                #
                # Positive evidence only, same rule as POV's RECONCILE: a bare
                # positionbook miss is NOT proof (that assumption cancelled the
                # stops on two live legs on 2026-08-14).
                # Throttled: the in-trade loop polls every 5s and this costs a
                # positionbook call, so at 5s it would add ~12 calls/min for the
                # whole session. RECON_SECS keeps it cheap; a ghost surviving
                # 30s longer costs nothing, since no exit decision depends on it.
                _closed, _detail = (False, "")
                if time.time() - _last_recon[0] >= RECON_SECS:
                    _last_recon[0] = time.time()
                    _closed, _detail = detect_external_close(active_trade, UNDERLYING, symbol)
                if _closed:
                    _ext_px = find_external_exit_price(symbol, active_trade)
                    _entry_fill = active_trade.get("entry_fill_price") or active_trade.get("entry_opt_price")
                    if _ext_px is not None and _entry_fill is not None:
                        _gross = (_ext_px - _entry_fill) * qty
                        _cost = statutory_cost(_entry_fill, _ext_px, qty)
                        _pnl = _gross - _cost
                        if _pnl < 0:
                            consecutive_losses += 1
                            daily_loss_rs += abs(_pnl)
                            log.info(f"Trade P&L: ₹{_pnl:+.2f} (gross ₹{_gross:+.2f} − cost ₹{_cost:.2f}, "
                                     f"externally closed) | Loss streak: {consecutive_losses} "
                                     f"| Daily losses: ₹{daily_loss_rs:.0f}")
                        else:
                            consecutive_losses = 0
                            log.info(f"Trade P&L: ₹{_pnl:+.2f} (gross ₹{_gross:+.2f} − cost ₹{_cost:.2f}, "
                                     f"externally closed) | Loss streak reset")
                    else:
                        # Close the tracking regardless. An unpriced closed trade
                        # is recoverable from the broker later; a permanent ghost
                        # blocks the strategy and lies on the dashboard.
                        log.warning(
                            "EXTERNAL CLOSE %s (%s) — exit price unavailable, P&L NOT booked. "
                            "Reconcile manually from the broker orderbook.", symbol, _detail)
                    log.info("EXTERNAL CLOSE: %s no longer held (%s) — releasing and standing down.",
                             symbol, _detail)
                    # An orphaned resting SELL is a NAKED SHORT once the position
                    # is gone. This is the same class of failure that produced
                    # POV's RECONCILE work, so it is cancelled on every exit path.
                    _ok, _m = safe_cancel_order(active_trade.get("disaster_oid"),
                                                f"external close {symbol}")
                    if active_trade.get("disaster_oid"):
                        (log.info if _ok else log.error)(
                            "disaster stop cancel on external close: %s", _m)
                    release_symbol_lock(symbol, STRATEGY_NAME)
                    state = "DONE"
                    active_trade = {}
                    _active_trade = {}
                    persist_done(today)   # a restart must not open a second trade
                    continue

                # ---- break-even ratchet -------------------------------------
                # Measured 2026-08-06 over the 25 live round trips since
                # 2026-07-14, replayed on 1-minute index bars (see
                # backtesting/haema_signal/judas_trail.py): MFE is median
                # 0.60R and only 4/25 trades ever touch the 2R target, so most
                # winners round-trip all the way back into the stop. Moving the
                # stop to entry once the trade has shown BE_ARM_R lifts the mean
                # result from +0.189R to +0.332R (paired improvement +0.143R,
                # 95% CI [+0.020, +0.294], better on 16/25 trades).
                #
                # A TRAILING stop was tested and is WORSE than doing nothing
                # (trail 0.5R off peak: +0.009R) because it caps the handful of
                # 2.6-3.75R runners that carry the book. Break-even protects the
                # downside without touching the upside, so the ratchet only ever
                # moves the stop to entry -- never further, never backwards.
                if (BE_ARM_R > 0 and not is_adopted
                        and not active_trade.get("be_moved")
                        and active_trade.get("entry_spot")
                        and active_trade.get("orig_sl_spot")):
                    entry_spot = float(active_trade["entry_spot"])
                    risk = abs(entry_spot - float(active_trade["orig_sl_spot"]))
                    if risk > 0:
                        sign = 1.0 if direction == "CE" else -1.0
                        fav_r = sign * (underlying_ltp - entry_spot) / risk
                        if fav_r >= BE_ARM_R:
                            active_trade["sl_spot"] = entry_spot
                            active_trade["be_moved"] = True
                            sl_spot = entry_spot
                            persist_trade(active_trade)
                            log.info(f"BREAK-EVEN ARMED: {fav_r:.2f}R reached "
                                     f"({underlying_ltp:.2f}) -- stop moved to entry "
                                     f"{entry_spot:.2f} (was {active_trade['orig_sl_spot']:.2f})")

                exit_triggered = False
                exit_reason = ""

                if current_time >= EXIT_TIME:
                    exit_triggered = True
                    exit_reason = f"EOD Squareoff ({EXIT_TIME.strftime('%H:%M')}, pre-CAS freeze)"
                elif is_adopted:
                    pass  # adopted orphan — defer to EOD; no SL/target known
                elif direction == "CE":
                    if underlying_ltp <= sl_spot:
                        exit_triggered = True
                        exit_reason = "Stop-Loss Hit"
                    elif underlying_ltp >= target_spot:
                        exit_triggered = True
                        exit_reason = "Target Hit"
                elif direction == "PE":
                    if underlying_ltp >= sl_spot:
                        exit_triggered = True
                        exit_reason = "Stop-Loss Hit"
                    elif underlying_ltp <= target_spot:
                        exit_triggered = True
                        exit_reason = "Target Hit"

                if exit_triggered:
                    log.info(f"!!! {exit_reason} !!! Closing position on {symbol}...")
                    # Sell only what the broker ACTUALLY holds — prevents naked-short
                    # exit SELLs (paper entry + live exit after mode toggle, rejected
                    # entry, already-closed position — bug 2026-07-14).
                    bq = live_position_qty(UNDERLYING, symbol)
                    if bq is None:
                        log.warning(f"{exit_reason}: cannot verify broker position for {symbol} — deferring exit")
                        time.sleep(5)
                        continue
                    pre_exit_opt_ltp = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)
                    if bq > 0:
                        order_resp = client.placeorder(
                            strategy=STRATEGY_NAME,
                            symbol=symbol,
                            action="SELL",
                            exchange=opt_exchange,
                            price_type="MARKET",
                            product=PRODUCT,
                            quantity=min(bq, qty)
                        )
                        log.info(f"Exit Order Response: {order_resp}")
                        # ── P&L on REAL fills, NET of statutory cost ──
                        # Until 2026-08-05 this was (pre_exit_opt_ltp - entry) * qty:
                        # a quote-derived GROSS number. It hid ~Rs 1,050/trade of
                        # friction and fed the circuit breakers a loss ~4x too small.
                        entry_opt_price = active_trade.get("entry_opt_price")
                        exit_oid = order_resp.get("orderid") if isinstance(order_resp, dict) else None
                        exit_px = fetch_fill_price(exit_oid)
                        src = "fill"
                        if exit_px is None:
                            exit_px = pre_exit_opt_ltp
                            src = "est-ltp"
                        entry_fill = active_trade.get("entry_fill_price") or entry_opt_price
                        if entry_fill is not None and exit_px is not None:
                            gross = (exit_px - entry_fill) * qty
                            cost = statutory_cost(entry_fill, exit_px, qty)
                            trade_pnl = gross - cost
                            if trade_pnl < 0:
                                consecutive_losses += 1
                                daily_loss_rs += abs(trade_pnl)
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} (gross ₹{gross:+.2f} − cost ₹{cost:.2f}, {src}) "
                                         f"| Loss streak: {consecutive_losses} | Daily losses: ₹{daily_loss_rs:.0f}")
                            else:
                                consecutive_losses = 0
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} (gross ₹{gross:+.2f} − cost ₹{cost:.2f}, {src}) "
                                         f"| Loss streak reset")
                    else:
                        log.warning(f"{exit_reason}: broker flat on {symbol} — no long to close; skipping SELL to avoid naked short")

                    # Cancel the resting backstop BEFORE releasing the symbol.
                    # Left alive after the position closes it is a naked short.
                    if active_trade.get("disaster_oid"):
                        _ok, _m = safe_cancel_order(active_trade["disaster_oid"],
                                                    f"exit {symbol}")
                        (log.info if _ok else log.error)(
                            "disaster stop cancel on %s: %s", exit_reason, _m)
                    release_symbol_lock(symbol, STRATEGY_NAME)

                    # Judas is one-trade-per-day — after any exit, done for the day
                    state = "DONE"
                    active_trade = {}
                    _active_trade = {}
                    persist_done(today)   # survives a restart, unlike `state`
                else:
                    time.sleep(5)  # Fast poll when in trade
                    continue

            # State Machine: IDLE (Opening-Range Sweep-Reversal Entry Monitoring)
            elif state == "IDLE":
                # Before OR completes, wait
                if current_time <= OR_END:
                    wait_secs = (datetime.combine(today, OR_END) - now).total_seconds()
                    log.info(f"Building opening range (≤ {OR_END}). Waiting {int(max(wait_secs, 0))}s...")
                    time.sleep(min(max(wait_secs, 0) + 1, 60))
                    continue

                if current_time > ENTRY_END:
                    log.info(f"Past entry window ({ENTRY_END}). Done for today.")
                    state = "DONE"
                    continue

                # Circuit breaker: consecutive losses
                if consecutive_losses >= LOSS_STREAK_LIMIT:
                    log.warning(f"CIRCUIT BREAKER: {consecutive_losses} consecutive losses. Halting for today.")
                    state = "DONE"
                    continue

                # Circuit breaker: daily loss cap
                if daily_loss_rs >= DAILY_LOSS_LIMIT_RS:
                    log.warning(f"CIRCUIT BREAKER: ₹{daily_loss_rs:.0f} daily losses exceed ₹{DAILY_LOSS_LIMIT_RS:.0f}. Halting.")
                    state = "DONE"
                    continue

                # Fetch 5m intraday history
                intra_start = (today - timedelta(days=3)).strftime("%Y-%m-%d")
                df_5m = client.history(
                    symbol=UNDERLYING,
                    exchange=idx_exchange,
                    interval="5m",
                    start_date=intra_start,
                    end_date=today.strftime("%Y-%m-%d")
                )
                if not isinstance(df_5m, pd.DataFrame) or len(df_5m) < 3:
                    time.sleep(15)
                    continue

                sig = compute_judas_signal(df_5m, today)
                if not sig:
                    time.sleep(15)
                    continue

                # Emit a parseable status line so the live monitor panel shows values each scan
                _swp = "SWEPT-HIGH" if sig["swept_high"] else "SWEPT-LOW" if sig["swept_low"] else "WAIT-SWEEP"
                _arm = ("ARMED-" + sig["signal"]) if sig.get("signal") else "SCANNING"
                log.info(
                    f"Regime: {_swp} | Phase: {_arm} | Velocity: {sig['spot']:.2f} | "
                    f"ATR: {sig['or_low']:.2f}-{sig['or_high']:.2f}"
                )

                # No reversal entry yet -> keep scanning
                if not sig.get("signal"):
                    time.sleep(15)
                    continue

                # Signal-aware cooldown: skip if this is the same candle we already acted on
                if last_entry_candle_fp is not None and sig["candle_fp"] == last_entry_candle_fp:
                    time.sleep(15)
                    continue

                signal_type = sig["signal"]
                entry_spot = sig["entry_spot"]
                sl_spot = sig["sl_spot"]
                target_spot = sig["target_spot"]

                expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
                if not expiry:
                    time.sleep(15)
                    continue
                # Expiry-day skip: nearest weekly expires today -> stand down.
                if UNDERLYING.upper() in SKIP_EXPIRY_DAY_UNDERLYINGS and is_expiry_today(expiry):
                    log.info(f"{UNDERLYING} weekly expires today ({expiry}) — skipping entries for the day "
                             f"(DTE-0 gamma; validated on the HA-EMA analog)")
                    state = "DONE"
                    continue
                opt_symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry, STRIKE_OFFSET, signal_type)
                if not opt_symbol:
                    time.sleep(15)
                    continue

                # Symbol lock: skip if another strategy holds this symbol
                if not acquire_symbol_lock(opt_symbol, STRATEGY_NAME):
                    log.info(f"Symbol {opt_symbol} locked by another strategy. Skipping this signal.")
                    last_entry_candle_fp = sig["candle_fp"]
                    time.sleep(15)
                    continue

                # Capture option entry price (validated; needed for P&L + auto-lot)
                entry_opt_price = fetch_option_ltp(opt_symbol, opt_exchange, underlying_ltp=underlying_ltp)

                # Compute entry quantity based on LOT_MODE
                if LOT_MODE == "auto" and entry_opt_price is not None:
                    capital = fetch_available_capital()
                    if capital is not None and capital > 0:
                        # Judas uses spot-based SL → worst case is full premium loss per unit
                        lots = compute_auto_lots(capital, RISK_PCT_PER_TRADE, entry_opt_price, LOT_SIZE, MAX_LOTS)
                        entry_qty = lots * LOT_SIZE
                        log.info(f"AUTO-LOT: capital ₹{capital:.0f} | risk {RISK_PCT_PER_TRADE}% → {lots} lots × {LOT_SIZE} = {entry_qty} qty (cap: {MAX_LOTS} lots)")
                    else:
                        entry_qty = LOT_SIZE  # fallback to 1 lot
                        log.warning("AUTO-LOT: capital unavailable, falling back to 1 lot")
                else:
                    entry_qty = LOT_SIZE * MAX_LOTS

                # ── Entry geometry gate ──
                # Refuse trades whose target cannot clear the cost floor, or whose
                # reward:risk was inverted by the MIN_SL_PCT stop floor. Both are
                # knowable here, before paying the spread to find out.
                ok, why, detail = check_entry_geometry(
                    entry_spot, sl_spot, target_spot, entry_opt_price, entry_qty)
                log.info(f"Entry geometry: {detail}")
                if not ok:
                    log.warning(f"GEOMETRY REJECT — {why}. Skipping {opt_symbol}.")
                    release_symbol_lock(opt_symbol, STRATEGY_NAME)
                    last_entry_candle_fp = sig["candle_fp"]
                    time.sleep(15)
                    continue

                log.info(f"Judas Reversal detected ({signal_type})! Placing BUY order for {opt_symbol} (qty={entry_qty})...")
                order_resp = client.placeorder(
                    strategy=STRATEGY_NAME,
                    symbol=opt_symbol,
                    action="BUY",
                    exchange=opt_exchange,
                    price_type="MARKET",
                    product=PRODUCT,
                    quantity=entry_qty
                )
                log.info(f"Entry Order Response: {order_resp}")

                if order_resp.get("status") == "success":
                    state = "IN_TRADE"
                    active_trade = {
                        "symbol": opt_symbol,
                        "direction": signal_type,
                        "entry_spot": entry_spot,
                        "sl_spot": sl_spot,
                        # Frozen copy of the ENTRY stop. sl_spot is mutated by the
                        # break-even ratchet, so R must be measured against this.
                        "orig_sl_spot": sl_spot,
                        "target_spot": target_spot,
                        "qty": entry_qty,
                        "entry_opt_price": entry_opt_price,
                        # Actual entry fill, so P&L rests on fills at BOTH legs.
                        # entry_opt_price stays as the pre-trade quote for the
                        # geometry/auto-lot maths; slippage is the gap between them.
                        "entry_fill_price": (
                            fetch_fill_price(order_resp.get("orderid"))
                            if isinstance(order_resp, dict) else None
                        ),
                        # Needed to ask the broker what happened to the ENTRY.
                        # detect_external_close() distinguishes "never filled"
                        # from "filled then closed by someone else"; without the
                        # id it can only count positionbook misses.
                        "entry_orderid": (
                            order_resp.get("orderid") if isinstance(order_resp, dict) else None
                        ),
                    }
                    _active_trade = active_trade
                    persist_trade(active_trade)
                    last_entry_candle_fp = sig["candle_fp"]
                    # Rest the wide backstop at the broker. Uses the ACTUAL fill
                    # when we have it, so the -60% level is measured from what we
                    # really paid rather than from a pre-trade quote.
                    _prem_ref = (active_trade.get("entry_fill_price")
                                 or active_trade.get("entry_opt_price"))
                    active_trade["disaster_oid"] = place_disaster_stop(
                        opt_symbol, opt_exchange, _prem_ref, entry_qty)
                    persist_trade(active_trade)
                    log.info(f"Entered Trade! Spot Entry: {entry_spot:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f} | Opt entry: {entry_opt_price}")
                    # Tape context at entry -- diagnostic only, never a gate.
                    # Judas trades off SPOT structure and never looks at the
                    # option's own book, so this is the one place it does. One
                    # extra call per trade (not per cycle), wrapped so it can
                    # never affect the position that was just opened.
                    try:
                        _df_opt = client.history(
                            symbol=opt_symbol, exchange=opt_exchange, interval="1m",
                            start_date=f"{date.today():%Y-%m-%d}",
                            end_date=f"{date.today():%Y-%m-%d}",
                        )
                        _q, _doi, _dpx = oi_price_quadrant(_df_opt)
                        log.info(
                            "TAPE %s quadrant=%s dOI=%+.1f%% dPx=%+.1f%% (%dm window)",
                            opt_symbol, _q or "unclear", _doi, _dpx, QUAD_WINDOW,
                        )
                    except Exception as _terr:
                        log.debug(f"tape annotation failed: {_terr}")
                else:
                    # Entry failed — release lock so other strategies can try
                    release_symbol_lock(opt_symbol, STRATEGY_NAME)

                time.sleep(15)

            elif state == "DONE":
                time.sleep(300)

        except Exception as e:
            log.error(f"Error in strategy loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
