#!/usr/bin/env python
"""
Autonomous HA + 34 EMA Channel Strategy
Monitors Heikin-Ashi daily bias and 34 EMA channel breakouts on NIFTY/SENSEX,
automatically executes option entries, and monitors spot index price for exits.
"""
import os
import re
import sys
import signal
import time
import logging
import json
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
STRATEGY_NAME = "HA 34-EMA Channel"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = os.getenv('PRODUCT', 'MIS')
QUANTITY = int(os.getenv('QUANTITY', '0'))  # 0 = auto-detect from exchange
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_SIZE = QUANTITY  # Will be updated at startup if auto-detected
LOT_MODE = os.getenv('LOT_MODE', 'manual').lower()  # 'manual' or 'auto'
RISK_PCT_PER_TRADE = float(os.getenv('RISK_PCT_PER_TRADE', '1.0'))

# Strike configuration
STRIKE_GAPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
}

# Exchange mapping
_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

def _index_exchange(underlying: str) -> str:
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"

def _option_exchange(underlying: str) -> str:
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"

# Strategy Constants
EMA_PERIOD = 34
STRIKE_OFFSET = os.getenv('STRIKE_OFFSET', 'ATM')  # 'ATM', 'ITM1', 'ITM2', 'OTM1', 'OTM2'
ENTRY_START = dtime(int(os.getenv('ENTRY_START_HOUR', '9')), int(os.getenv('ENTRY_START_MIN', '45')))
ENTRY_END = dtime(int(os.getenv('ENTRY_END_HOUR', '14')), int(os.getenv('ENTRY_END_MIN', '30')))
# CAS (SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/2765/2026, live 2026-08-03):
# cash continuous trading in F&O-underlying stocks ends 15:15, and the index
# spot then teleports on the ~15:28 auction stamp (NIFTY +200.95 pts in one
# tick, 2026-08-03). SL/target here are evaluated against spot, so squareoff
# must complete while spot is still live. Options trade to 15:40, so a 15:10
# market exit fills normally.
EXIT_TIME = dtime(*(int(x) for x in os.getenv('EXIT_TIME', '15:10').split(':')))

# VIX gate: skip the day entirely when INDIA VIX is below this.
# Measured over 3y: NIFTY average intraday range is 0.94% (SENSEX 0.95%), and
# a 34-EMA channel breakout cannot clear its own friction in a box that tight.
# Range expands to 1.24% above VIX 15 and 1.60% above VIX 20.
VIX_GATE_MIN = float(os.getenv('VIX_GATE_MIN', '15.0'))
BREAKOUT_MULT = float(os.getenv('BREAKOUT_MULT', '1.0'))
# Circuit breaker config (replaces COOLDOWN_MINUTES / MAX_TRADES_PER_DAY)
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
DAILY_LOSS_LIMIT_RS = float(os.getenv('DAILY_LOSS_LIMIT_RS', '10000'))
# Option-premium stop (broker-side stop on the option itself) — catches
# premium decay/bleed that the spot-based SL misses (bug 2026-07-16: a CE bled
# 169->82, -51%, while spot held above the spot-SL). Exit if the option premium
# falls >= this % below entry. Broker-side, so it also protects across restarts.
PREMIUM_SL_PCT = float(os.getenv('PREMIUM_SL_PCT', '35'))
# MUST be SL (stop-limit), NOT SL-M. Measured 2026-07-28 from order_logs: SL-M
# was rejected 33/33 times on NFO+BFO options (0 created) while MARKET on the
# same symbols/sessions succeeded 114/114 — NSE/BSE discontinued SL-M in the
# options segment. The service reported those rejections as
# {"status":"success","orderid":null}, so 33 live positions ran with NO stop
# while the strategy logged the stop as placed. Stop-limit needs a limit price
# below the trigger to fill on the way down; this is that buffer.
SL_LIMIT_BUFFER_PCT = float(os.getenv('SL_LIMIT_BUFFER_PCT', '5'))
# Round-trip statutory cost as % of option premium turnover (brokerage is zero on
# Flattrade, but STT/exchange/GST/SEBI/stamp are not). Default 0.12% matches
# Flattrade's calculator: Rs 103.01 on Rs 84,000 options turnover = 12.3 bps.
# Subtracted from every logged trade P&L so the circuit breaker and the logs
# reflect NET money, not gross.
OPT_COST_PCT = float(os.getenv('OPT_COST_PCT', '0.12'))
# Minimum stop distance as % of spot (Volrix-validated 2026-07-28).
# Raw stop = signal candle's extreme, which on a quiet candle is inside the
# index's noise (live: 5.4-5.9 pt / 0.022% stops died in 12-42 seconds).
# Floors the STOP only — the target still uses the raw candle risk.
MIN_SL_PCT = float(os.getenv('MIN_SL_PCT', '0.10'))
# Skip entries on the traded weekly's own expiry day, for these underlyings.
# NIFTY expiry (Tue) is the worst day by a wide margin: DTE-0 ATM premium is
# ~1/3 of a normal day's, so gamma converts a 0.025% adverse spot move into a
# ~19% premium loss. Removing those entries was the single biggest improvement
# in backtest (-3.01% -> +0.24% gross). NOT applied to SENSEX: its own expiry
# (Thu) is its best day in live logs, and it has no backtest support.
SKIP_EXPIRY_DAY_UNDERLYINGS = {
    s.strip().upper() for s in os.getenv('SKIP_EXPIRY_DAY_UNDERLYINGS', 'NIFTY').split(',') if s.strip()
}

# Symbol lock dir (shared across all strategies on this host)
LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)

# Locks must never wedge. Measured 2026-07-29: 9 orphaned .lock files were sitting
# in this dir, some on already-expired contracts, because release only runs on the
# normal exit paths - a crash, restart, or unusual exit branch leaks the file. The
# old acquire() had no staleness check, so a leaked lock on a LIVE contract would
# silently block valid entries forever.
LOCK_TTL_MIN = float(os.getenv('LOCK_TTL_MIN', '360'))   # max plausible hold: one session


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
        return True                                  # unparseable -> reclaim
    if when.date() != date.today():
        return True                                  # previous session
    if (datetime.now() - when).total_seconds() / 60.0 > LOCK_TTL_MIN:
        return True
    if pid and not _pid_alive(pid):
        return True                                  # owner process gone
    return False


def acquire_symbol_lock(symbol, strategy_name):
    """Claim one CONTRACT. True if acquired, already ours, or the holder's lock is stale.

    Prevents two strategies holding the same option at once (quantity/netting mess).
    It cannot prevent OPPOSING bets - CE and PE are different symbols - see
    acquire_direction_lock() for that.
    """
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    if lock_file.exists():
        try:
            parts = lock_file.read_text().split("|")
            owner = parts[0]
            if owner == strategy_name:
                return True
            ts = parts[1] if len(parts) > 1 else ""
            pid = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
            if not _lock_is_stale(ts, pid):
                return False
            log.warning(f"Stale contract lock on {symbol} (owner '{owner}') - reclaiming")
        except Exception:
            return False
    try:
        lock_file.write_text(f"{strategy_name}|{datetime.now().isoformat()}|{os.getpid()}")
        return True
    except Exception:
        return False

def release_symbol_lock(symbol, strategy_name):
    """Release the contract lock if we own it."""
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists():
            owner = lock_file.read_text().split("|", 1)[0]
            if owner == strategy_name:
                lock_file.unlink()
    except Exception:
        pass


# ── Directional lock: one directional VIEW per underlying, across strategies ──
# Measured 2026-07-29: 3 episodes where different strategies simultaneously held
# CE and PE on the SAME underlying (e.g. 07-07 HA-EMA long NIFTY CE while Judas
# bought NIFTY PE). Net delta ~0, double premium, double theta, double cost - a
# straddle nobody designed. The per-contract lock cannot see this because CE and
# PE are different symbols.
#
# Scope is deliberately CROSS-STRATEGY ONLY: a strategy is never blocked by its
# own files, so POV keeps its intended multi-strike/both-side behaviour (11 of the
# 14 opposing episodes were internal to one strategy and are by design).
def _strategy_slug(name):
    return re.sub(r'[^A-Za-z0-9]+', '_', str(name)).strip('_')


def acquire_direction_lock(underlying, side, strategy_name):
    """Claim a DIRECTION (CE/PE) on an underlying. False if another strategy is
    already positioned the opposite way."""
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
                continue                                   # never block on ourselves
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
        (LOCKS_DIR / f"{und}.{me}.{want}.dir").write_text(
            f"{datetime.now().isoformat()}|{os.getpid()}")
    except Exception:
        pass
    return True


def release_direction_lock(underlying, strategy_name, side=None):
    """Drop our directional claim on an underlying (a given side, or all sides)."""
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

# ── Position state persistence (survive restarts with full SL/target context) ──
# Bug 2026-07-13: a service restart orphaned an open position; old adoption only
# knew entry price -> "EOD-exit-only". The snapshot lets a restart re-arm SL/target.
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"ha_ema34_channel_{UNDERLYING.upper()}.json"

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
    if auto_lots < 1:
        log.warning("Risk budget Rs %.0f (%.1f%% of Rs %.0f) is below 1 lot max loss Rs %.0f -- taking 1 lot minimum floor",
                    risk_budget, risk_pct, capital, max_loss_per_lot)
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

def fetch_fill_price(order_id, symbol, max_retries=5, retry_delay=1.2):
    """Average TRADED price for an order, so P&L is measured on real fills.

    Quote-derived P&L (what this strategy used before 2026-07-29) silently
    excludes slippage: it prices the exit at the LTP we happened to observe,
    not at what the broker actually filled. Over 27 live trades that difference
    is the entire question of whether the edge is real, so it is worth the
    extra API calls.

    Returns float on success, or None (caller falls back to the quote).
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


def statutory_cost(entry_px, exit_px, qty):
    """Round-trip statutory cost in rupees for an option BUY->SELL, premium-based.

    Brokerage is zero on Flattrade, but STT/exchange/GST/SEBI/stamp are not.
    OPT_COST_PCT is the round-trip charge as a % of premium turnover; the default
    0.12% matches Flattrade's own calculator (Rs 103.01 of charges on Rs 84,000 of
    options turnover = 12.3 bps). Configure via OPT_COST_PCT if your contract
    note differs.
    """
    if entry_px is None or exit_px is None or not qty:
        return 0.0
    turnover = (float(entry_px) + float(exit_px)) * float(qty)
    return turnover * OPT_COST_PCT / 100.0


def fetch_india_vix():
    """Live INDIA VIX, or None when unavailable.

    None must not be read as 'low volatility' - an API failure is not a
    market signal. The caller keeps trading when this returns None and
    relies on the circuit breakers instead.
    """
    try:
        resp = client.quotes(symbol="INDIAVIX", exchange="NSE_INDEX")
        if resp.get("status") == "success":
            ltp = float((resp.get("data") or {}).get("ltp", 0) or 0)
            if ltp > 0:
                return ltp
    except Exception as e:
        log.warning(f"Could not fetch INDIA VIX: {e}")
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

def is_expiry_today(expiry_str):
    """True when `expiry_str` ('28JUL26' from get_nearest_expiry) is today."""
    if not expiry_str:
        return False
    try:
        return datetime.strptime(expiry_str.upper(), "%d%b%y").date() == date.today()
    except (ValueError, TypeError):
        return False

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

def fetch_lot_size(underlying, idx_exchange, opt_exchange):
    """Fetch actual lot size from option chain. Returns lot size or None."""
    try:
        expiry = get_nearest_expiry(underlying, opt_exchange)
        if not expiry:
            return None
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
    except Exception as e:
        log.error(f"Error fetching lot size: {e}")
    return None

def compute_daily_ha_bias(df_daily):
    if not isinstance(df_daily, pd.DataFrame) or len(df_daily) < 2:
        return None
    df = df_daily.copy()
    df = df.sort_index().reset_index(drop=True)
    n = len(df)
    ha_open = [0.0] * n
    ha_close = [0.0] * n

    ha_open[0] = (df.loc[0, "open"] + df.loc[0, "close"]) / 2.0
    ha_close[0] = (df.loc[0, "open"] + df.loc[0, "high"] + df.loc[0, "low"] + df.loc[0, "close"]) / 4.0

    for i in range(1, n):
        ha_close[i] = (df.loc[i, "open"] + df.loc[i, "high"] + df.loc[i, "low"] + df.loc[i, "close"]) / 4.0
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    prev_ha_open = ha_open[-1]
    prev_ha_close = ha_close[-1]

    if prev_ha_close >= prev_ha_open:
        return "GREEN"
    return "RED"

def compute_ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result

# Shutdown state shared between signal handler and run loop
_shutdown_requested = False
_active_trade = {}
_opt_exchange = None

def safe_cancel_order(order_id, context=""):
    """Cancel an order; treat already complete/cancelled/rejected/not-found as success."""
    try:
        resp = client.cancelorder(order_id=order_id, strategy=STRATEGY_NAME)
        if isinstance(resp, dict):
            if resp.get("status") == "success":
                return True
            msg = str(resp.get("message", resp)).lower()
            if any(k in msg for k in ("complete", "cancel", "reject", "not found", "not open", "no order")):
                return True
        return False
    except Exception as e:
        log.debug(f"cancel {order_id} ({context}) failed: {e}")
        return False

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
                release_direction_lock(UNDERLYING, STRATEGY_NAME)
                log.info("Shutdown complete. Exiting.")
                sys.exit(0)

            if broker_qty is None:
                # UNKNOWN (positionbook non-success, e.g. app restarting -> 502).
                # Do NOT assume flat; keep lock + state so restart adoption re-arms it.
                log.error(f"Shutdown: cannot verify {symbol} position — leaving untouched for restart adoption")
                log.info("Shutdown complete. Exiting.")
                sys.exit(0)
            # Position state known — cancel any resting option SL-M (avoid lingering naked short)
            _sl_oid = _active_trade.get("opt_sl_orderid")
            if _sl_oid:
                safe_cancel_order(_sl_oid, context=f"shutdown-{symbol}")
            if broker_qty <= 0:
                log.info(f"Shutdown: broker reports {symbol} qty={broker_qty} — already flat, no SELL")
                release_symbol_lock(symbol, STRATEGY_NAME)
                release_direction_lock(UNDERLYING, STRATEGY_NAME)
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
                    release_symbol_lock(symbol, STRATEGY_NAME)
                    release_direction_lock(UNDERLYING, STRATEGY_NAME)
                except Exception as e:
                    log.error(f"Failed to close position on shutdown: {e}")
    else:
        log.info("No active position — nothing to close.")

    log.info("Shutdown complete. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

def run_strategy():
    global _active_trade, _opt_exchange, QUANTITY, LOT_SIZE
    log.info(f"Starting Autonomous HA 34-EMA Channel Strategy for {UNDERLYING}...")
    strike_gap = STRIKE_GAPS.get(UNDERLYING.upper(), 50)
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
            QUANTITY = 75  # fallback
            LOT_SIZE = 75
            log.warning(f"Could not detect lot size, using default: {QUANTITY}")
    else:
        LOT_SIZE = QUANTITY
        log.info(f"Using configured lot size: {QUANTITY}")
    # Active trade state
    state = "IDLE"
    active_trade = {}
    trade_date = None
    last_entry_candle_fp = None  # (open,high,low,close) tuple of the candle that triggered last entry
    consecutive_losses = 0
    daily_loss_rs = 0.0
    vix_gate_checked = False

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
        trade_date = date.today()  # seed so the new-day reset on first iteration does NOT wipe the adopted IN_TRADE state
        persist_trade(active_trade)
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
                vix_gate_checked = False   # re-evaluate VIX once per session
                log.info(f"--- New trading day initialized: {trade_date} ---")

            # 1. Fetch daily HA bias (only if in IDLE state)
            if state == "IDLE":
                # Shoonya TPSeries hangs on interval="D" for index tokens.
                # Fetch 5m candles and aggregate to daily OHLC instead.
                daily_start = (today - timedelta(days=10)).strftime("%Y-%m-%d")
                yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                df_intra = client.history(
                    symbol=UNDERLYING,
                    exchange=idx_exchange,
                    interval="5m",
                    start_date=daily_start,
                    end_date=yesterday
                )
                df_daily = None
                if isinstance(df_intra, pd.DataFrame) and not df_intra.empty:
                    df_intra = df_intra.sort_index()
                    # Group by date and aggregate to daily OHLC
                    df_intra["date"] = df_intra.index.date if hasattr(df_intra.index, 'date') else pd.to_datetime(df_intra.index).date
                    df_daily = df_intra.groupby("date").agg(
                        open=("open", "first"),
                        high=("high", "max"),
                        low=("low", "min"),
                        close=("close", "last")
                    ).reset_index(drop=True)
                bias = compute_daily_ha_bias(df_daily)
                if not bias:
                    log.warning("Could not compute HA bias. Retrying in 60s...")
                    time.sleep(60)
                    continue
                log.info(f"Previous-day HA bias: {bias} → trading {'CE' if bias == 'GREEN' else 'PE'} only")

            # Get current time
            now = datetime.now()
            current_time = now.time()

            # 2. Fetch Spot Price (LTP)
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

                # Premium SL-M (POV-style broker stop on the option) — did it fill?
                opt_sl_oid = active_trade.get("opt_sl_orderid")
                if opt_sl_oid:
                    o_st = ""
                    try:
                        st = client.orderstatus(order_id=opt_sl_oid, strategy=STRATEGY_NAME)
                        if isinstance(st, dict):
                            o_st = ((st.get("data") or {}).get("order_status") or "").lower()
                    except Exception:
                        o_st = ""
                    if o_st == "complete":
                        # real fill of the stop order, not the trigger level we asked for
                        exit_px = fetch_fill_price(opt_sl_oid, symbol) \
                            or active_trade.get("opt_sl_price")
                        entry_opt_price = active_trade.get("entry_fill_price") \
                            or active_trade.get("entry_opt_price")
                        log.info(f"!!! Premium SL Hit !!! stop filled on {symbol} @ {exit_px}")
                        if entry_opt_price is not None and exit_px is not None:
                            gross = (exit_px - entry_opt_price) * qty
                            cost = statutory_cost(entry_opt_price, exit_px, qty)
                            trade_pnl = gross - cost
                            if trade_pnl < 0:
                                consecutive_losses += 1
                                daily_loss_rs += abs(trade_pnl)
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} (gross ₹{gross:+.2f} "
                                         f"- cost ₹{cost:.2f}) | Loss streak: {consecutive_losses} "
                                         f"| Daily losses: ₹{daily_loss_rs:.0f}")
                            else:
                                consecutive_losses = 0
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} (gross ₹{gross:+.2f} "
                                         f"- cost ₹{cost:.2f}) | Loss streak reset")
                        release_symbol_lock(symbol, STRATEGY_NAME)
                        release_direction_lock(UNDERLYING, STRATEGY_NAME)
                        state = "DONE"
                        active_trade = {}
                        _active_trade = {}
                        persist_trade({})
                        continue

                # Adopted orphans from prior sessions have no SL/target — log a marker and only do EOD exit
                is_adopted = active_trade.get("adopted") and (sl_spot is None or target_spot is None)
                if is_adopted:
                    log.info(f"Monitoring (adopted) Trade: {symbol} | Spot: {underlying_ltp:.2f} | EOD-exit-only")
                else:
                    log.info(f"Monitoring Trade: {symbol} | Spot: {underlying_ltp:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f}")

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
                    # Cancel the resting premium SL-M first (avoid lingering naked short)
                    opt_sl_oid = active_trade.get("opt_sl_orderid")
                    if opt_sl_oid:
                        safe_cancel_order(opt_sl_oid, context=f"{exit_reason}-{symbol}")
                    # Sell only what the broker ACTUALLY holds — prevents naked-short
                    # exit SELLs (paper entry + live exit after mode toggle, rejected
                    # entry, already-closed position — bug 2026-07-14).
                    bq = live_position_qty(UNDERLYING, symbol)
                    if bq is None:
                        log.warning(f"{exit_reason}: cannot verify broker position for {symbol} — deferring exit")
                        time.sleep(5)
                        continue
                    # Capture option LTP before exit to compute trade P&L (validated against spot)
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
                        # P&L on REAL fills, net of statutory cost. The quote is only
                        # a fallback: pricing the exit at an observed LTP hides the
                        # slippage that decides whether this strategy has an edge.
                        _exit_oid = order_resp.get("orderid") if isinstance(order_resp, dict) else None
                        exit_fill = fetch_fill_price(_exit_oid, symbol) or pre_exit_opt_ltp
                        entry_opt_price = active_trade.get("entry_fill_price") \
                            or active_trade.get("entry_opt_price")
                        if entry_opt_price is not None and exit_fill is not None:
                            gross = (exit_fill - entry_opt_price) * qty
                            cost = statutory_cost(entry_opt_price, exit_fill, qty)
                            trade_pnl = gross - cost
                            _src = "fill" if _exit_oid and exit_fill != pre_exit_opt_ltp else "quote"
                            if trade_pnl < 0:
                                consecutive_losses += 1
                                daily_loss_rs += abs(trade_pnl)
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} (gross ₹{gross:+.2f} "
                                         f"- cost ₹{cost:.2f}, exit={_src} {exit_fill}) "
                                         f"| Loss streak: {consecutive_losses} "
                                         f"| Daily losses: ₹{daily_loss_rs:.0f}")
                            else:
                                consecutive_losses = 0
                                log.info(f"Trade P&L: ₹{trade_pnl:+.2f} (gross ₹{gross:+.2f} "
                                         f"- cost ₹{cost:.2f}, exit={_src} {exit_fill}) "
                                         f"| Loss streak reset")
                    else:
                        log.warning(f"{exit_reason}: broker flat on {symbol} — no long to close; skipping SELL to avoid naked short")

                    # Release symbol lock
                    release_symbol_lock(symbol, STRATEGY_NAME)
                    release_direction_lock(UNDERLYING, STRATEGY_NAME)

                    # Single-entry per day: ANY exit (SL/Target/EOD) ends the day.
                    # Matches the faithful Volrix backtest (re-entry chop removed).
                    state = "DONE"
                    log.info("Trade closed — single-entry mode, done for the day.")
                    active_trade = {}
                    _active_trade = {}
                    persist_trade({})
                else:
                    time.sleep(5)  # Fast poll when in trade
                    continue

            # State Machine: IDLE (Breakout Entry Monitoring)
            elif state == "IDLE":
                if current_time < ENTRY_START:
                    wait_secs = (datetime.combine(today, ENTRY_START) - now).total_seconds()
                    log.info(f"Before entry window. Waiting {int(wait_secs)}s...")
                    time.sleep(min(wait_secs + 1, 60))
                    continue

                if current_time > ENTRY_END:
                    log.info("Past entry window (14:30). Done for today.")
                    state = "DONE"
                    continue

                # VIX gate — evaluated once per session, after ENTRY_START so
                # the quote is live. A tight-range day cannot pay for a channel
                # breakout's friction, so stand aside for the whole session
                # rather than re-testing and drifting into a late entry.
                if not vix_gate_checked:
                    vix_gate_checked = True
                    vix_val = fetch_india_vix()
                    if vix_val is None:
                        log.warning("INDIA VIX unavailable — gate bypassed, circuit breakers still active.")
                    elif vix_val < VIX_GATE_MIN:
                        log.info(
                            f"VIX GATE: INDIA VIX {vix_val:.2f} < {VIX_GATE_MIN:.1f} — "
                            f"intraday range too tight for a 34-EMA breakout. Standing aside today."
                        )
                        state = "DONE"
                        continue
                    else:
                        log.info(f"VIX GATE PASSED: INDIA VIX {vix_val:.2f} >= {VIX_GATE_MIN:.1f}.")

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

                if not isinstance(df_5m, pd.DataFrame) or len(df_5m) < EMA_PERIOD + 2:
                    time.sleep(15)
                    continue

                df_5m = df_5m.sort_index().reset_index(drop=True)
                idx = len(df_5m) - 2  # Last completed candle
                if idx < EMA_PERIOD:
                    time.sleep(15)
                    continue

                # Compute EMA bands
                upper_band = compute_ema_series(df_5m["high"].tolist(), EMA_PERIOD)
                lower_band = compute_ema_series(df_5m["low"].tolist(), EMA_PERIOD)

                candle_close = df_5m.loc[idx, "close"]
                candle_high = df_5m.loc[idx, "high"]
                candle_low = df_5m.loc[idx, "low"]
                ema_upper = upper_band[idx]
                ema_lower = lower_band[idx]

                log.info(f"Spot Close[-2]: {candle_close:.2f} | Upper: {ema_upper:.2f} | Lower: {ema_lower:.2f}")

                # Candle fingerprint for signal-aware cooldown
                current_candle_fp = (float(candle_close), float(candle_high), float(candle_low), float(df_5m.loc[idx, "open"]))

                # If this is the same candle that triggered our last entry, skip — wait for a NEW signal candle
                if last_entry_candle_fp is not None and current_candle_fp == last_entry_candle_fp:
                    time.sleep(15)
                    continue

                signal = None
                entry_spot = candle_close
                sl_spot = None
                target_spot = None

                # Stop-distance floor (Volrix-validated 2026-07-28, run a6777437).
                # The raw stop is the signal candle's extreme, which on a quiet
                # candle sits inside the index's own noise: live stops of 5.4-5.9
                # pts (0.022%) were taken out in 12-42 seconds. Floor it.
                # CRITICAL: the target stays RR x the RAW candle risk. Scaling the
                # target with the floored risk pushes it out of reach and is
                # strictly worse (V1: -6.16% vs V2: -2.03% on the same window).
                risk_floor = entry_spot * (MIN_SL_PCT / 100.0)

                if bias == "GREEN" and candle_close > ema_upper * BREAKOUT_MULT:
                    raw_risk = entry_spot - candle_low
                    if raw_risk > 0:
                        sl_spot = entry_spot - max(raw_risk, risk_floor)
                        target_spot = entry_spot + 2.0 * raw_risk
                        signal = "CE"
                elif bias == "RED" and candle_close < ema_lower * (2.0 - BREAKOUT_MULT):
                    raw_risk = candle_high - entry_spot
                    if raw_risk > 0:
                        sl_spot = entry_spot + max(raw_risk, risk_floor)
                        target_spot = entry_spot - 2.0 * raw_risk
                        signal = "PE"

                if signal:
                    expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
                    if not expiry:
                        time.sleep(15)
                        continue
                    # Expiry-day skip: nearest weekly expires today -> stand down.
                    if UNDERLYING.upper() in SKIP_EXPIRY_DAY_UNDERLYINGS and is_expiry_today(expiry):
                        log.info(f"{UNDERLYING} weekly expires today ({expiry}) — skipping entries for the day "
                                 f"(DTE-0 gamma; backtest-validated)")
                        state = "DONE"
                        continue
                    opt_symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry, STRIKE_OFFSET, signal)
                    if not opt_symbol:
                        time.sleep(15)
                        continue

                    # Symbol lock: skip if another strategy holds this symbol
                    # One directional view per underlying across ALL strategies:
                    # refuse to buy a CE while another strategy holds a PE here.
                    if not acquire_direction_lock(UNDERLYING, signal, STRATEGY_NAME):
                        last_entry_candle_fp = current_candle_fp
                        time.sleep(15)
                        continue
                    if not acquire_symbol_lock(opt_symbol, STRATEGY_NAME):
                        log.info(f"Symbol {opt_symbol} locked by another strategy. Skipping this signal.")
                        last_entry_candle_fp = current_candle_fp  # prevent re-checking same candle
                        time.sleep(15)
                        continue

                    # Capture option entry price (validated; needed for P&L + auto-lot)
                    entry_opt_price = fetch_option_ltp(opt_symbol, opt_exchange, underlying_ltp=underlying_ltp)

                    # Compute entry quantity based on LOT_MODE
                    if LOT_MODE == "auto" and entry_opt_price is not None:
                        capital = fetch_available_capital()
                        if capital is not None and capital > 0:
                            # HA-EMA uses spot-based SL → worst case is full premium loss per unit
                            lots = compute_auto_lots(capital, RISK_PCT_PER_TRADE, entry_opt_price, LOT_SIZE, MAX_LOTS)
                            entry_qty = lots * LOT_SIZE
                            log.info(f"AUTO-LOT: capital ₹{capital:.0f} | risk {RISK_PCT_PER_TRADE}% → {lots} lots × {LOT_SIZE} = {entry_qty} qty (cap: {MAX_LOTS} lots)")
                        else:
                            entry_qty = LOT_SIZE  # fallback to 1 lot
                            log.warning(f"AUTO-LOT: capital unavailable, falling back to 1 lot")
                    else:
                        # Manual mode: existing behavior
                        entry_qty = LOT_SIZE * MAX_LOTS

                    log.info(f"Breakout Signal detected! Placing BUY order for {opt_symbol} (qty={entry_qty})...")
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
                    # Actual entry fill, for honest P&L. Keep entry_opt_price (the
                    # pre-trade quote) for the stop maths, which must be computed
                    # from a price known before the order goes in.
                    entry_fill_price = None
                    if isinstance(order_resp, dict) and order_resp.get("orderid"):
                        entry_fill_price = fetch_fill_price(order_resp.get("orderid"), opt_symbol)
                        if entry_fill_price and entry_opt_price:
                            _slip = (entry_fill_price - entry_opt_price) / entry_opt_price * 1e4
                            log.info(f"Entry fill {entry_fill_price} vs quote {entry_opt_price} "
                                     f"→ slippage {_slip:+.0f} bps")

                    if order_resp.get("status") == "success":
                        state = "IN_TRADE"
                        # Premium stop: SL (stop-LIMIT) SELL on the option at
                        # entry_premium * (1 - PREMIUM_SL_PCT%), tick-aligned to 0.05.
                        # SL-M is NOT usable here - it is rejected outright for
                        # options (measured 33/33 failures), so we send a stop-limit
                        # with the limit set below the trigger so it still fills
                        # while price is falling.
                        opt_sl_orderid = None
                        opt_sl_price = None
                        if entry_opt_price is not None:
                            _raw = entry_opt_price * (1.0 - PREMIUM_SL_PCT / 100.0)
                            opt_sl_price = round(round(_raw / 0.05) * 0.05, 2)
                            _lim = opt_sl_price * (1.0 - SL_LIMIT_BUFFER_PCT / 100.0)
                            opt_sl_limit = max(0.05, round(round(_lim / 0.05) * 0.05, 2))
                            try:
                                sl_resp = client.placeorder(
                                    strategy=STRATEGY_NAME,
                                    symbol=opt_symbol,
                                    action="SELL",
                                    exchange=opt_exchange,
                                    price_type="SL",
                                    trigger_price=opt_sl_price,
                                    price=opt_sl_limit,
                                    product=PRODUCT,
                                    quantity=entry_qty
                                )
                                # An order without an id does NOT exist. The API can
                                # answer status=success with orderid=null when the
                                # broker rejected it; trusting that left 33 live
                                # positions unprotected. Require the id.
                                _oid = sl_resp.get("orderid") if isinstance(sl_resp, dict) else None
                                if isinstance(sl_resp, dict) and sl_resp.get("status") == "success" and _oid:
                                    opt_sl_orderid = _oid
                                    log.info(f"Premium SL placed @ trigger {opt_sl_price} "
                                             f"limit {opt_sl_limit} ({PREMIUM_SL_PCT:.0f}% below "
                                             f"entry {entry_opt_price}) — order {opt_sl_orderid}")
                                else:
                                    log.error(f"PREMIUM SL NOT PLACED — position is UNPROTECTED "
                                              f"at the broker; relying on in-process spot/premium "
                                              f"monitoring only. Response: {sl_resp}")
                            except Exception as e:
                                log.error(f"Failed to place premium SL: {e} — position is "
                                          f"UNPROTECTED at the broker")
                        active_trade = {
                            "symbol": opt_symbol,
                            "direction": signal,
                            "entry_spot": entry_spot,
                            "sl_spot": sl_spot,
                            "target_spot": target_spot,
                            "qty": entry_qty,
                            "entry_opt_price": entry_opt_price,
                            "entry_fill_price": entry_fill_price,
                            "opt_sl_orderid": opt_sl_orderid,
                            "opt_sl_price": opt_sl_price,
                        }
                        _active_trade = active_trade
                        persist_trade(active_trade)
                        last_entry_candle_fp = current_candle_fp
                        log.info(f"Entered Trade! Spot Entry: {entry_spot:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f} | Opt entry: {entry_opt_price} | Prem SL: {opt_sl_price}")
                    else:
                        # Entry failed — release lock so other strategies can try
                        release_symbol_lock(opt_symbol, STRATEGY_NAME)

                # No signal (or entry placed/failed) → pause before re-evaluating to avoid hot-looping the broker
                time.sleep(15)

            elif state == "DONE":
                # Sleep longer when done for the day
                time.sleep(300)

        except Exception as e:
            log.error(f"Error in strategy loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
