#!/usr/bin/env python
"""
POV Wall-Squeeze Strategy
Monitors multiple option strikes (CE and PE) and generates short-squeeze signals
from closed 1-minute option candles, executing trades broker-agnostically via OpenAlgo.
"""
import os
import re
import sys
import signal
import time
import logging
import json
from datetime import datetime, date
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
STRATEGY_NAME = "POV Wall-Squeeze"
UNDERLYING = os.getenv('UNDERLYING', 'NIFTY')
PRODUCT = "MIS"
QUANTITY = int(os.getenv('QUANTITY', '0'))  # 0 = auto-detect from exchange
MAX_LOTS = int(os.getenv('MAX_LOTS', '1'))
LOT_SIZE = QUANTITY
LOT_MODE = os.getenv('LOT_MODE', 'manual').lower()
RISK_PCT_PER_TRADE = float(os.getenv('RISK_PCT_PER_TRADE', '1.0'))

# Strike configuration
STRIKE_GAPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
}

# Exchange mapping — NSE indices trade on NFO, BSE indices on BFO
_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

def _index_exchange(underlying: str) -> str:
    """Return the spot-quote exchange for the given underlying."""
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"

def _option_exchange(underlying: str) -> str:
    """Return the F&O exchange where the underlying's options trade."""
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"

# POV constants
COOLDOWN_MINUTES = int(os.getenv('POV_COOLDOWN_MINUTES', '30'))  # POV signal dedup cooldown (per-symbol action change)
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
DAILY_LOSS_LIMIT_RS = float(os.getenv('DAILY_LOSS_LIMIT_RS', '10000'))
# Protective stops MUST be SL (stop-limit), never SL-M: measured 2026-07-28 from
# order_logs, SL-M was rejected 33/33 times on NFO+BFO options (0 created) while
# MARKET on the same symbols/sessions succeeded 114/114 - the exchanges do not
# accept SL-M in the options segment. Worse, the API reported those rejections as
# {"status":"success","orderid":null}, so live positions ran with NO stop while
# the logs claimed one was armed. A stop-limit needs its limit below the trigger
# to still fill while price falls; this is that buffer, in % of the trigger.
SL_LIMIT_BUFFER_PCT = float(os.getenv('SL_LIMIT_BUFFER_PCT', '5'))
# Round-trip statutory cost as % of option premium turnover (brokerage is zero on
# Flattrade; STT/exchange/GST/SEBI/stamp are not). 0.12% matches Flattrade's own
# calculator: Rs 103.01 on Rs 84,000 options turnover = 12.3 bps. Subtracted from
# every booked trade so the circuit breaker trips on NET money lost.
OPT_COST_PCT = float(os.getenv('OPT_COST_PCT', '0.12'))
# High-conviction gating (cut over-trading / charge bleed — see cost analysis 2026-07-13)
POV_MIN_SCORE = int(os.getenv('POV_MIN_SCORE', '5'))  # 5=STRONG only (all 5 conditions); 4 also allows WATCH
POV_MAX_TRADES_PER_DAY = int(os.getenv('POV_MAX_TRADES_PER_DAY', '4'))  # hard daily entry cap per underlying (backstop)
# Safety-net exits (prevent slow-bleed when SL is cancelled/orphaned by restart)
MAX_HOLD_MINUTES = int(os.getenv('MAX_HOLD_MINUTES', '45'))  # close if held longer than this without hitting T1
DECAY_EXIT_PCT = float(os.getenv('DECAY_EXIT_PCT', '0.60'))  # close if LTP < this fraction of entry price

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

    Parsing only the pipe form made a JSON lock look like owner='{"strategy"...'
    with ts='' -- and _lock_is_stale() treats an unparseable timestamp as STALE,
    so this strategy was silently RECLAIMING PDH's LIVE locks and could open the
    same contract PDH already held. Verified by reproducing PDH's exact body.
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
            return None, "", 0          # unreadable -> caller must not claim it
    parts = raw.split("|")
    ts = parts[1] if len(parts) > 1 else ""
    pid = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
    return (parts[0] if parts else ""), ts, pid

def acquire_symbol_lock(symbol, strategy_name):
    """Claim one CONTRACT. True if acquired, already ours, or the holder's lock is stale.

    Prevents two strategies holding the same option at once (quantity/netting mess).
    It cannot prevent OPPOSING bets - CE and PE are different symbols - see
    acquire_direction_lock() for that.
    """
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    if lock_file.exists():
        owner, ts, pid = _read_lock(lock_file)
        if owner is None:
            # Unreadable body. Standing aside is the safe reading: the previous
            # code fell through to `return False` here too, and claiming a lock
            # we cannot parse is how PDH's live locks got stolen.
            log.warning(f"Unreadable contract lock on {symbol} - standing aside")
            return False
        if owner == strategy_name:
            return True
        if not _lock_is_stale(ts, pid):
            return False
        log.warning(f"Stale contract lock on {symbol} (owner '{owner}') - reclaiming")
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


def sync_direction_locks(positions, strategy_name, underlying):
    """Keep our directional claims in step with the legs actually open.

    POV runs several strikes at once, so a single leg exiting must NOT free the
    direction while other legs on that side are still live. Release a side only
    when no open position uses it.
    """
    sides = {("CE" if str(sym).endswith("CE") else "PE") for sym in (positions or {})}
    for side in ("CE", "PE"):
        if side not in sides:
            release_direction_lock(underlying, strategy_name, side)


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
# Bug 2026-07-13: a service restart orphaned an open position; the old adoption
# path only knew entry price -> "EOD-exit-only" (no SL, no target). The state
# file lets a restarted process re-arm full management on its own positions.
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"pov_wall_squeeze_{UNDERLYING.upper()}.json"


# ---- Live Monitor status sidecar -------------------------------------------
STATUS_FILE = (
    Path("log") / "strategies" / f"{os.getenv('STRATEGY_ID', STRATEGY_NAME)}_status.json"
)


def write_status(state, active_trades=None, indicators=None, last_message=None):
    """Publish a status snapshot for the Live Monitor. Best-effort by design."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state,
            "active_trades": active_trades or [],
            "indicators": indicators or {},
            "last_updated": datetime.now().isoformat(),
            "last_log_message": last_message,
        }
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception:
        pass

def persist_positions(positions):
    """Snapshot tracked positions to disk (called on every open/close)."""
    try:
        snap = {}
        for sym, pos in positions.items():
            p = dict(pos)
            et = p.get("entry_time")
            if isinstance(et, datetime):
                p["entry_time"] = et.isoformat()
            snap[sym] = p
        STATE_FILE.write_text(json.dumps(snap))
    except Exception as e:
        log.debug(f"persist_positions failed: {e}")

def load_persisted_positions():
    """Load the position snapshot from a previous run. {} when absent/corrupt."""
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            for p in data.values():
                et = p.get("entry_time")
                if isinstance(et, str):
                    try:
                        p["entry_time"] = datetime.fromisoformat(et)
                    except ValueError:
                        p["entry_time"] = None
            return data
    except Exception as e:
        log.warning(f"load_persisted_positions failed: {e}")
    return {}


def is_position_claimed_by_peer(symbol, my_strategy_name):
    """Check if ANY peer strategy has claimed this symbol in its state file or holds its active lock."""
    try:
        # 1. Check lock file
        lock_f = LOCKS_DIR / f"{symbol}.lock"
        if lock_f.exists():
            _owner, _ts, _pid = _read_lock(lock_f)
            if _owner and _owner != my_strategy_name and not _lock_is_stale(_ts, _pid):
                return True, f"lock held by '{_owner}' (pid {_pid})"
        # 2. Check all state files in log/strategies/state/
        state_dir = Path("log") / "strategies" / "state"
        if state_dir.exists():
            for sf in state_dir.glob("*.json"):
                try:
                    data = json.loads(sf.read_text())
                    if isinstance(data, dict):
                        if data.get("symbol") == symbol and not data.get("adopted"):
                            return True, f"state file {sf.name}"
                        if symbol in data and not (data[symbol] or {}).get("adopted"):
                            return True, f"state file {sf.name}"
                except Exception:
                    pass
    except Exception:
        pass
    return False, None

def reconcile_orphan_positions(underlying):
    """Check positionbook for open positions matching this underlying. Returns list of dicts."""
    found = []
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return found
        for pos in pb.get("data", []):
            qty = int(pos.get("quantity", 0) or 0)
            sym = pos.get("symbol", "") or ""
            if qty != 0 and underlying.upper() in sym.upper():
                found.append({
                    "symbol": sym,
                    "qty": abs(qty),
                    "entry_price": float(pos.get("average_price", 0) or 0),
                })
    except Exception as e:
        log.debug(f"Reconcile failed: {e}")
    return found

def live_position_qty(underlying, symbol):
    """Return current qty on `symbol` per the broker's positionbook, 0 if absent."""
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict) or pb.get("status") != "success":
            return None  # cannot verify
        for pos in pb.get("data", []):
            if (pos.get("symbol", "") or "").upper() == symbol.upper():
                return abs(int(pos.get("quantity", 0) or 0))
        return 0
    except Exception as e:
        log.debug(f"live_position_qty failed for {symbol}: {e}")
        return None

def safe_cancel_order(order_id, context=""):
    """Cancel an order, treating 'already complete/cancelled/rejected' as success.

    The sandbox engine (and many live brokers) return an error when you try to
    cancel an order that's already filled — but for our purposes that's the
    desired end state: the order is no longer active. Map those terminal-status
    errors to a clean no-op so callers can audit-log cleanly.

    Returns: (True, msg) on cancel-or-already-terminal; (False, err_msg) otherwise.
    """
    try:
        resp = client.cancelorder(order_id=order_id, strategy=STRATEGY_NAME)
    except Exception as e:
        return False, f"cancelorder threw: {e}"

    if not isinstance(resp, dict):
        return True, f"non-dict response (assumed ok): {resp}"

    if resp.get("status") == "success":
        return True, "cancelled"

    msg = str(resp.get("message", "")).lower()
    # Terminal states the broker reports — already what we want
    terminal = ("complete", "cancelled", "canceled", "rejected", "trigger pending", "no such order")
    if any(t in msg for t in terminal):
        return True, f"already terminal: {resp.get('message', '')}"

    return False, f"{resp.get('message', resp)}"

def fetch_fill_price(order_id, context="", max_retries=4, retry_delay=0.7):
    """Actual average fill price for an order, or None if unreadable.

    P&L must be booked from the broker's fill, never from a quote. A MARKET exit
    decided off a price the strategy merely *observed* can fill points away
    (bug 2026-07-28: target seen at 48.90, filled 37.80 -> logged +1056 vs real
    +348), and daily_loss_rs / consecutive_losses inherit the error, so the
    circuit breaker ends up armed on fictional numbers.
    """
    if not order_id:
        return None
    for attempt in range(max_retries):
        try:
            st = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
            if isinstance(st, dict) and st.get("status") == "success":
                d = st.get("data", {}) or {}
                for key in ("average_price", "averageprice", "avg_price", "price"):
                    try:
                        px = float(d.get(key) or 0)
                    except (TypeError, ValueError):
                        px = 0.0
                    if px > 0:
                        return px
                if str(d.get("order_status", "")).lower() in ("rejected", "cancelled", "canceled"):
                    return None
        except Exception as e:
            log.debug(f"fetch_fill_price {order_id} attempt {attempt + 1}: {e}")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.warning(f"Could not read fill price for order {order_id} {context}".strip())
    return None


def confirm_entry_fill(order_id, symbol, context="", max_retries=6, retry_delay=0.7):
    """Did the ENTRY actually fill? Returns (state, fill_price).

        'complete' -> the broker confirms a fill; fill_price is the real average
        'dead'     -> the broker confirms rejected/cancelled; do NOT trade it
        'unknown'  -> still pending, or unreadable, after max_retries

    fetch_fill_price() collapses 'rejected' and 'unreadable' into the same None,
    which is what let a REJECTED order be treated as a position on 2026-08-14:
    the caller fell back to the pre-trade quote, armed a stop and logged an
    entry for SENSEX20AUG2677800CE, which never existed. Callers need the three
    states kept apart, because the correct response differs for each.
    """
    if not order_id:
        return "unknown", None
    for attempt in range(max_retries):
        try:
            st = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
            if isinstance(st, dict) and st.get("status") == "success":
                d = st.get("data", {}) or {}
                status = str(d.get("order_status", "")).strip().lower()
                if status in ("rejected", "cancelled", "canceled"):
                    return "dead", None
                for key in ("average_price", "averageprice", "avg_price", "price"):
                    try:
                        px = float(d.get(key) or 0)
                    except (TypeError, ValueError):
                        px = 0.0
                    if px > 0:
                        return "complete", px
                if status in ("complete", "completed", "filled"):
                    # Filled but the average is unreadable -- still a real position.
                    return "complete", None
        except Exception as e:
            log.debug(f"confirm_entry_fill {order_id} attempt {attempt + 1}: {e}")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.warning(f"Entry fill state undetermined for order {order_id} {context}".strip())
    return "unknown", None

def statutory_cost(entry_px, exit_px, qty):
    """Round-trip statutory cost in rupees for an option BUY->SELL, premium-based.

    Brokerage is zero on Flattrade, but STT/exchange/GST/SEBI/stamp are not.
    OPT_COST_PCT is the round-trip charge as a % of premium turnover; the default
    0.12% matches Flattrade's own calculator (Rs 103.01 of charges on Rs 84,000 of
    options turnover = 12.3 bps).
    """
    if entry_px is None or exit_px is None or not qty:
        return 0.0
    return (float(entry_px) + float(exit_px)) * float(qty) * OPT_COST_PCT / 100.0


def book_trade_pnl(symbol, exit_px, entry_px, qty, consecutive_losses, daily_loss_rs, source="fill"):
    """Book realized P&L NET of statutory cost and update the circuit-breaker counters.

    `source` records whether the price is a broker fill or a degraded estimate so
    the log states which. Cost is subtracted because the circuit breaker should
    trip on money actually lost, not on a gross figure that flatters the strategy.
    Returns the updated (consecutive_losses, daily_loss_rs).
    """
    if exit_px is None or entry_px is None:
        log.warning(f"P&L not booked for {symbol}: exit={exit_px} entry={entry_px} (unverifiable)")
        return consecutive_losses, daily_loss_rs
    gross = (float(exit_px) - float(entry_px)) * qty
    cost = statutory_cost(entry_px, exit_px, qty)
    trade_pnl = gross - cost
    detail = (f"[{source} {float(exit_px):.2f} vs entry {float(entry_px):.2f} | "
              f"gross ₹{gross:+.2f} - cost ₹{cost:.2f}]")
    if trade_pnl < 0:
        consecutive_losses += 1
        daily_loss_rs += abs(trade_pnl)
        log.info(f"Trade P&L: ₹{trade_pnl:+.2f} {detail} | Loss streak: {consecutive_losses} | Daily losses: ₹{daily_loss_rs:.0f}")
    else:
        consecutive_losses = 0
        log.info(f"Trade P&L: ₹{trade_pnl:+.2f} {detail} | Loss streak reset")
    return consecutive_losses, daily_loss_rs

def verified_exit_sell(symbol, opt_exchange, qty, sl_oid, reason):
    """Cancel the protective SL and SELL only what the broker ACTUALLY holds.
    Prevents naked-short exit SELLs (paper entry + live exit after a mode toggle,
    rejected/partial entry, or an already-closed position — bug 2026-07-14).
    Returns ('sold' | 'flat' | 'unknown', fill_price_or_None).
    """
    bq = live_position_qty(UNDERLYING, symbol)
    if bq is None:
        log.warning(f"{reason}: cannot verify broker position for {symbol} — deferring exit")
        return "unknown", None
    if sl_oid:
        ok, msg = safe_cancel_order(sl_oid, context=f"{reason}-{symbol}")
        (log.info if ok else log.warning)(f"{reason}: cancel SL {sl_oid} → {msg}")
    if bq <= 0:
        log.warning(f"{reason}: broker flat on {symbol} — no long to close; skipping SELL to avoid naked short")
        return "flat", None
    close_qty = min(bq, qty)
    resp = client.placeorder(
        strategy=STRATEGY_NAME, symbol=symbol, action="SELL",
        exchange=opt_exchange, price_type="MARKET",
        product=PRODUCT, quantity=close_qty)
    log.info(f"{reason} exit response for {symbol}: {resp}")
    fill_px = None
    if isinstance(resp, dict) and resp.get("status") == "success":
        fill_px = fetch_fill_price(resp.get("orderid"), context=f"({reason} {symbol})")
    return "sold", fill_px

# Consecutive positionbook misses required before RECONCILE will cancel a
# protective stop on evidence it could not confirm. One miss is not proof.
RECON_MISS_LIMIT = int(os.getenv("RECON_MISS_LIMIT", "3"))
_recon_miss = {}   # symbol -> consecutive unexplained positionbook misses

def order_state(order_id, context=""):
    """Terminal state of an order per the broker: 'complete', 'rejected',
    'cancelled', 'pending', or None when it cannot be determined.

    The positionbook is NOT sufficient evidence on its own -- see
    sync_positions_with_book below for what that cost on 2026-08-14.
    """
    if not order_id:
        return None
    try:
        r = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
        d = (r.get("data") or {}) if isinstance(r, dict) else {}
        st = str(d.get("order_status", "")).strip().lower()
        if st in ("complete", "completed", "filled"):
            return "complete"
        if st in ("rejected",):
            return "rejected"
        if st in ("cancelled", "canceled"):
            return "cancelled"
        if st:
            return "pending"
    except Exception as e:
        log.debug(f"order_state({order_id}) {context} failed: {e}")
    return None


def sync_positions_with_book(positions, underlying):
    """Drop tracked positions the broker no longer holds -- but only on POSITIVE
    evidence, never on a bare positionbook miss.

    2026-08-14, live money: POV opened three SENSEX legs at 12:49. The
    positionbook did not list them, so this function pruned all three and
    cancelled their stop-losses:
        77800CE  entry REJECTED           -> pruning was correct
        78100CE  entry COMPLETE @ 333.05  -> live position, SL cancelled
        77900CE  entry COMPLETE @ 433.95  -> live position, SL cancelled
    77900CE's stop was 420.1 and the leg was trading 426-436 when it was
    pruned, so it was genuinely open and simply unprotected from then on. Both
    ran naked to the broker's MIS auto-squareoff, and neither P&L ever reached
    the strategy's books or its circuit breakers. This is the second occurrence
    of this failure mode; the July one lost 75-80% on three legs the same way.

    A missing positionbook row is ambiguous -- it can equally mean the broker
    simply is not reporting that leg. So:
      * entry rejected/cancelled  -> the position never existed. Prune.
      * SL order complete         -> stopped out. Prune.
      * entry complete, SL live   -> DISCREPANCY. Keep the position, KEEP THE
                                     STOP ARMED, and shout. max-hold and the
                                     decay floor remain as backstops.
      * nothing determinable      -> require MISS_LIMIT consecutive misses
                                     before touching a protective stop.
    """
    pruned = 0
    for symbol in list(positions.keys()):
        qty = live_position_qty(underlying, symbol)
        if qty is None:
            _recon_miss.pop(symbol, None)
            continue  # could not verify; leave intact
        if qty != 0:
            _recon_miss.pop(symbol, None)
            continue

        pos = positions.get(symbol, {})
        sl_oid = pos.get("sl_orderid")
        entry_state = order_state(pos.get("entry_orderid"), f"entry {symbol}")
        sl_state = order_state(sl_oid, f"sl {symbol}") if sl_oid else None

        if entry_state == "complete" and sl_state not in ("complete", "rejected", "cancelled"):
            log.error(
                "RECONCILE DISCREPANCY: %s absent from positionbook but its ENTRY "
                "order is COMPLETE and the SL is %s. Treating the position as LIVE "
                "and leaving the stop armed. Verify manually.",
                symbol, sl_state or "unknown",
            )
            _recon_miss.pop(symbol, None)
            continue

        if entry_state in ("rejected", "cancelled"):
            reason = f"entry {entry_state}"
        elif sl_state == "complete":
            reason = "SL filled"
        else:
            misses = _recon_miss.get(symbol, 0) + 1
            _recon_miss[symbol] = misses
            if misses < RECON_MISS_LIMIT:
                log.warning(
                    "RECONCILE: %s absent from positionbook (%s/%s) and its state is "
                    "undetermined -- holding the stop until confirmed.",
                    symbol, misses, RECON_MISS_LIMIT,
                )
                continue
            reason = f"absent {misses}x, state undetermined"

        if sl_oid and sl_state != "complete":
            ok, msg = safe_cancel_order(sl_oid, context=f"reconcile-{symbol}")
            level = log.info if ok else log.error
            level(f"RECONCILE: {symbol} pruned ({reason}); cancel SL {sl_oid} → {msg}")
        else:
            log.info(f"RECONCILE: {symbol} pruned ({reason}); no SL to cancel")
        _recon_miss.pop(symbol, None)
        release_symbol_lock(symbol, STRATEGY_NAME)
        del positions[symbol]
        persist_positions(positions)
        sync_direction_locks(positions, STRATEGY_NAME, UNDERLYING)
        pruned += 1
    return pruned

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
                if underlying_ltp is None or ltp < underlying_ltp * 0.2:
                    return ltp
                log.warning(f"Option LTP {ltp:.2f} suspiciously close to spot {underlying_ltp:.2f} for {opt_symbol}; retry {attempt+1}/{max_retries}")
        except Exception as e:
            log.warning(f"Option LTP fetch failed for {opt_symbol}: {e}; retry {attempt+1}/{max_retries}")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.error(f"Failed to get valid option LTP for {opt_symbol} after {max_retries} attempts")
    return None

# Per-underlying OI thresholds.
#
# Absolute open-interest counts DO NOT port across books. Measured
# 2026-08-05..08-07 on the population POV actually scans (front weekly,
# ATM +/- 2, both sides): NIFTY median 4-bar positive OI change 159,575 and
# median |dOI| 79,430, versus SENSEX 5,160 and 1,460 -- roughly 31x and 54x
# smaller at the same moneyness.
#
# One absolute constant therefore gated the two books in OPPOSITE directions:
#     pre-gate >= 50,000   NIFTY 71% pass   SENSEX 11%  (starved it)
#     c2       <  30,000   NIFTY 26% pass   SENSEX 95%  (free point)
# which is why POV traded NIFTY normally but stopped taking SENSEX trades
# after 2026-07-30: as the SENSEX weekly moved away from expiry its 5-minute
# OI churn fell under a floor sized for NIFTY, and the evaluator returned
# score 0 on every leg, every poll (45/45 on 2026-08-07).
#
# The SENSEX values are NIFTY's rescaled by SENSEX's own churn, so both books
# get the SAME selectivity (SENSEX lands at 76% / 30% against NIFTY's
# 71% / 26%). NIFTY is unchanged -- it is the working book and must not move.
# Unlisted underlyings keep NIFTY's values.
_DEF_PRE_OI_MIN = {"NIFTY": 50000, "SENSEX": 1600}
_DEF_OI_ABS_THRESHOLD = {"NIFTY": 30000, "SENSEX": 550}
_U = UNDERLYING.upper()
PRE_OI_MIN = float(os.getenv("PRE_OI_MIN", _DEF_PRE_OI_MIN.get(_U, 50000)))
PRE_LOOKBACK = 4
OI_ABS_THRESHOLD = float(os.getenv("OI_ABS_THRESHOLD", _DEF_OI_ABS_THRESHOLD.get(_U, 30000)))
OI_PCT_MIDCP = 0.07
VOL_MULT = 3.0
RANGE_MULT = 2.0
WICK_MAX = 0.15

# Cooldown and state tracking
_state = {}


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

def fetch_lot_size(underlying, idx_exchange, opt_exchange):
    """Actual contract lot size, or None if it genuinely cannot be determined.

    TWO independent sources. Relying on optionchain alone produced invalid
    orders on 2026-08-12: it returned 404 "No strikes found for NIFTY expiring
    18-AUG-26 ... update master contract" all session on BOTH indices, even
    though the master held 462 CE rows for that expiry. Detection fell through
    to a hardcoded guess and every order was rejected with "Quantity must be in
    multiples of lot size". symbol() answered correctly throughout.
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
# Thresholds are upstream's: below these the move is too small to call.
QUAD_WINDOW = int(os.getenv("QUAD_WINDOW", "15"))   # candles (1m) to look back
QUAD_OI_MIN_PCT = 1.0                               # upstream OI_MILD_PCT
QUAD_PRICE_MIN_PCT = 1.0                            # upstream PRICE_SIG_PCT / 3


def oi_price_quadrant(df, window=None):
    """Positioning read: OI change x price change over `window` candles.

    The four-quadrant label every options desk uses, and the one openmtops
    narrates in plain English:
        OI up   + price up   -> long buildup    (new longs paying up)
        OI up   + price down -> fresh writing   (new shorts, sellers in control)
        OI down + price up   -> short covering  (shorts buying back = squeeze)
        OI down + price down -> long unwinding  (longs giving up)

    DIAGNOSTIC ONLY. This never gates a trade. It is recorded at entry so the
    exit study has something to explain outcomes against -- POV's edge already
    lives in positioning rather than price geometry, so the tape state at fill
    is the natural covariate to test. If outcomes separate by quadrant it
    becomes a pre-registered filter hypothesis; if not, nothing is lost.

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

def evaluate_pov(symbol, df, is_midcp=False):
    """
    Evaluate short-squeeze pattern on closed 1-minute candles.
    df columns must include: open, high, low, close, volume, oi
    """
    if len(df) < 6:
        return {"action": "WAIT", "score": 0, "is_new": False}

    # Calculate oi_change on the DataFrame first to avoid boundary issues
    df = df.copy()
    df["oi_change"] = df["oi"].diff().fillna(0)

    # Format data to list of dicts
    candles = df.tail(10).to_dict(orient='records')

    cur = candles[-1]   # Most recent CLOSED candle (broker returns only closed bars)
    prev = candles[-2]

    # Require recent positive OI build-up into the trigger candle (last PRE_LOOKBACK candles incl. cur)
    pos_oi_sum = sum(max(0, c.get("oi_change", 0)) for c in candles[-PRE_LOOKBACK:])
    if pos_oi_sum < PRE_OI_MIN:
        return _dedup_action(symbol, "WAIT", 0, None, None, None, None, None)

    last5_vols = [c.get("volume", 0) for c in candles[-6:-1]]
    avg_vol = sum(last5_vols) / len(last5_vols) if last5_vols else 0
    c1 = cur.get("volume", 0) > avg_vol * VOL_MULT

    oi_chg = abs(cur.get("oi_change", 0))
    threshold = max(cur.get("oi", 1), 1) * OI_PCT_MIDCP if is_midcp else OI_ABS_THRESHOLD
    c2 = oi_chg < threshold

    cur_range = cur.get("high", 0) - cur.get("low", 0)
    prev_range = prev.get("high", 0) - prev.get("low", 0)
    c3 = (cur_range > prev_range * RANGE_MULT) if prev_range > 0 else False

    lo = cur.get("low", 0)
    op = cur.get("open", 0)
    cl = cur.get("close", 0)
    body_lo = min(op, cl)
    c4 = ((body_lo - lo) / cur_range < WICK_MAX) if cur_range > 0 else False

    c5 = cl > op

    score = sum([c1, c2, c3, c4, c5])
    action = "STRONG" if score == 5 else ("WATCH" if score == 4 else "WAIT")

    entry = sl = t1 = t2 = t3 = None
    if score >= 4:
        entry = round(cl, 2)
        sl = round(lo, 2)
        risk = max(entry - sl, 0.5)
        t1 = round(entry + risk * 1.5, 2)
        t2 = round(entry + risk * 3.0, 2)
        t3 = round(entry + risk * 5.0, 2)

    return _dedup_action(symbol, action, score, entry, sl, t1, t2, t3)


def _dedup_action(symbol, action, score, entry, sl, t1, t2, t3):
    now = datetime.now()
    prev_state = _state.get(symbol, {})
    action_changed = action != prev_state.get("action")
    cooldown_reset = False
    if not action_changed and action in {"STRONG", "WATCH"}:
        prev_time = prev_state.get("time")
        if prev_time:
            elapsed = (now - prev_time).total_seconds() / 60
            cooldown_reset = elapsed >= COOLDOWN_MINUTES

    is_new = False
    if action_changed or cooldown_reset:
        is_new = True
        _state[symbol] = {"action": action, "time": now}

    return {
        "action": action,
        "score": score,
        "is_new": is_new,
        "entry": entry,
        "sl": sl,
        "t1": t1,
        "t2": t2,
        "t3": t3,
    }


# Shutdown state shared between signal handler and run loop
_shutdown_requested = False
_positions = {}
_opt_exchange = None

def _graceful_shutdown(signum, frame):
    """Handle Ctrl+C / SIGTERM: close active positions, cancel pending SL orders, then exit."""
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    log.info(f"\n{'='*60}")
    log.info(f"SHUTDOWN SIGNAL RECEIVED ({sig_name}) — cleaning up...")
    log.info(f"{'='*60}")

    if _positions and _opt_exchange:
        # Fetch positionbook ONCE so we can verify each tracked position before closing
        broker_qtys = None  # None = UNKNOWN (API unreachable / non-success) — touch nothing
        try:
            pb = client.positionbook()
            if isinstance(pb, dict) and pb.get("status") == "success":
                broker_qtys = {}
                for pos in pb.get("data", []):
                    sym_u = (pos.get("symbol", "") or "").upper()
                    if sym_u:
                        broker_qtys[sym_u] = int(pos.get("quantity", 0) or 0)
            else:
                log.error("Shutdown: positionbook returned non-success — position state UNKNOWN")
        except Exception as e:
            log.error(f"Shutdown: positionbook fetch failed: {e} — position state UNKNOWN")

        if broker_qtys is None:
            # CRITICAL: cannot verify positions (e.g. app restarting -> 502).
            # Do NOT cancel SLs, do NOT assume flat. Leave everything intact for
            # state-file adoption on next boot. (Bug 2026-07-13: an empty dict
            # here mis-read an open position as flat and orphaned it unmanaged.)
            log.error("Shutdown: leaving positions and SL orders untouched for restart adoption.")
            log.info("Shutdown complete. Exiting.")
            sys.exit(0)

        for symbol, pos in list(_positions.items()):
            # ALWAYS cancel the pending SL order (safe regardless of position state)
            sl_oid = pos.get("sl_orderid")
            if sl_oid:
                ok, msg = safe_cancel_order(sl_oid, context=f"shutdown-{symbol}")
                level = log.info if ok else log.warning
                level(f"Shutdown: cancel SL {sl_oid} for {symbol} → {msg}")

            # CRITICAL: only SELL what the broker still says we own.
            # Repeated restarts of a stale _positions dict had been firing repeated
            # SELLs, flipping us net-SHORT on options we no longer held.
            broker_qty = broker_qtys.get(symbol.upper(), 0)
            if broker_qty <= 0:
                log.info(f"Shutdown: broker reports {symbol} qty={broker_qty} — already flat, no SELL")
                release_symbol_lock(symbol, STRATEGY_NAME)
                continue

            close_qty = min(broker_qty, pos.get("qty", QUANTITY))
            log.info(f"Closing position: {symbol} (broker qty={broker_qty}, closing {close_qty})")
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
                log.info(f"Shutdown exit response for {symbol}: {resp}")
                release_symbol_lock(symbol, STRATEGY_NAME)
            except Exception as e:
                log.error(f"Failed to close {symbol} on shutdown: {e}")
    else:
        log.info("No active positions — nothing to close.")

    write_status("INACTIVE")
    log.info("Shutdown complete. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

def run_strategy():
    global _positions, _opt_exchange, QUANTITY, LOT_SIZE
    log.info(f"Starting POV Wall-Squeeze Strategy for underlying: {UNDERLYING}...")
    strike_gap = STRIKE_GAPS.get(UNDERLYING.upper(), 50)
    is_midcp = UNDERLYING.upper() == "MIDCPNIFTY"
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
            # NEVER guess. The old fallback was a hardcoded 75 -- NIFTY's lot
            # size before the 2025-12-31 change to 65, and never correct for
            # SENSEX (20). On 2026-08-12 detection failed and that guess got
            # every order rejected: "Quantity must be in multiples of lot size".
            # Analyzer rejects a wrong size outright; LIVE would risk a
            # wrong-sized REAL position. Stand down instead.
            log.error(
                "Lot size undetectable for %s (both optionchain and symbol() "
                "failed) -- standing down. Set QUANTITY explicitly to override.",
                UNDERLYING,
            )
            sys.exit(1)
    else:
        LOT_SIZE = QUANTITY
        log.info(f"Using configured lot size: {QUANTITY}")

    positions = {}  # symbol -> {qty, sl_orderid, target_price, entry_opt_price, entry_candle_fp}
    _positions = positions
    consecutive_losses = 0
    daily_loss_rs = 0.0
    halted = False
    trade_date_pov = None
    trades_today = 0

    # Adopt orphan positions on boot — restore full SL/target context when we
    # have a persisted snapshot for the symbol; EOD-exit-only otherwise.
    persisted = load_persisted_positions()
    orphans = reconcile_orphan_positions(UNDERLYING)
    for orphan in orphans:
        sym = orphan["symbol"]
        saved = persisted.get(sym)
        if saved:
            pos = dict(saved)
            pos["qty"] = orphan["qty"]  # broker is authoritative on qty
            # Verify the persisted SL order is still working; re-arm if not
            sl_oid = pos.get("sl_orderid")
            sl_ok = False
            if sl_oid:
                try:
                    st = client.orderstatus(order_id=sl_oid, strategy=STRATEGY_NAME)
                    o_st = ((st.get("data") or {}).get("order_status") or "").lower()
                    sl_ok = st.get("status") == "success" and o_st in ("open", "trigger pending", "pending")
                except Exception:
                    sl_ok = False
            if not sl_ok and pos.get("sl_price"):
                pos["sl_orderid"] = None
                try:
                    _trg = float(pos["sl_price"])
                    _lim = max(0.05, round(round((_trg * (1.0 - SL_LIMIT_BUFFER_PCT / 100.0))
                                                 / 0.05) * 0.05, 2))
                    sl_resp = client.placeorder(
                        strategy=STRATEGY_NAME, symbol=sym, action="SELL",
                        exchange=opt_exchange, price_type="SL",
                        trigger_price=_trg, price=_lim, product=PRODUCT,
                        quantity=pos["qty"])
                    _oid = sl_resp.get("orderid") if isinstance(sl_resp, dict) else None
                    if sl_resp.get("status") == "success" and _oid:
                        pos["sl_orderid"] = _oid
                        log.info(f"Re-armed SL for {sym} @ trigger {_trg} limit {_lim} "
                                 f"— order {_oid}")
                    else:
                        log.error(f"Re-arm SL for {sym} FAILED — position UNPROTECTED "
                                  f"at the broker: {sl_resp}")
                except Exception as e:
                    log.error(f"Failed to re-arm SL for {sym}: {e} — position UNPROTECTED")
            log.warning(
                f"Adopting position with RESTORED context: {sym} qty={pos['qty']} "
                f"@ {pos.get('entry_opt_price')} | SL: {pos.get('sl_price')} | T1: {pos.get('target_price')}")
            positions[sym] = pos
        else:
            is_claimed, peer_detail = is_position_claimed_by_peer(sym, STRATEGY_NAME)
            if is_claimed:
                log.info(f"Orphan {sym} is claimed by peer ({peer_detail}) — skipping adoption")
                continue
            log.warning(f"Adopting unknown orphan (EOD-exit-only): {sym} qty={orphan['qty']} @ {orphan['entry_price']}")
            positions[sym] = {
                "qty": orphan["qty"],
                "sl_orderid": None,
                "target_price": None,
                "entry_opt_price": orphan["entry_price"],
                "entry_candle_fp": None,
                "adopted": True,
            }
            acquire_symbol_lock(sym, STRATEGY_NAME)
    # Rewrite snapshot to current reality (drops stale entries broker no longer holds)
    persist_positions(positions)
    sync_direction_locks(positions, STRATEGY_NAME, UNDERLYING)

    while True:
        try:
            today_pov = date.today()
            if trade_date_pov != today_pov:
                trade_date_pov = today_pov
                consecutive_losses = 0
                daily_loss_rs = 0.0
                halted = False
                trades_today = 0
                log.info(f"--- New trading day initialized: {trade_date_pov} ---")

            # Circuit breaker — once halted, only manage existing positions, no new entries
            if not halted:
                if consecutive_losses >= LOSS_STREAK_LIMIT:
                    log.warning(f"CIRCUIT BREAKER: {consecutive_losses} consecutive losses. New entries halted.")
                    halted = True
                elif daily_loss_rs >= DAILY_LOSS_LIMIT_RS:
                    log.warning(f"CIRCUIT BREAKER: ₹{daily_loss_rs:.0f} daily losses exceed ₹{DAILY_LOSS_LIMIT_RS:.0f}. New entries halted.")
                    halted = True

            # Reconcile tracked positions with broker positionbook — drops stale entries
            # and cancels orphan SL orders if the position was closed externally
            # (e.g. by another strategy's shutdown handler, manual close, MIS squareoff).
            try:
                pruned = sync_positions_with_book(positions, UNDERLYING)
                if pruned:
                    log.info(f"RECONCILE: pruned {pruned} stale position(s) from tracking")
            except Exception as e:
                log.error(f"RECONCILE pass failed: {e}")

            # 1. Resolve nearest options expiry dynamically
            expiry = get_nearest_expiry(UNDERLYING, opt_exchange)
            if not expiry:
                log.warning("Could not retrieve nearest expiry date. Retrying in 15s...")
                time.sleep(15)
                continue

            # 2. Fetch current underlying index price (LTP)
            quotes_resp = client.quotes(symbol=UNDERLYING, exchange=idx_exchange)
            if not quotes_resp or quotes_resp.get("status") != "success" or "data" not in quotes_resp:
                log.warning(f"Failed to fetch quotes for underlying {UNDERLYING}. Retrying...")
                time.sleep(15)
                continue

            underlying_ltp = float(quotes_resp["data"]["ltp"])
            atm_strike = round(underlying_ltp / strike_gap) * strike_gap
            log.info(f"Underlying LTP: {underlying_ltp}, ATM Strike: {atm_strike}, Expiry: {expiry}")
            # Publish sidecar for Live Monitor
            _pov_trades = []
            for _sym, _pos in positions.items():
                _dir = "CE" if _sym.upper().endswith("CE") else ("PE" if _sym.upper().endswith("PE") else "UNKNOWN")
                _pov_trades.append({
                    "symbol": _sym,
                    "direction": _dir,
                    "entry_price": float(_pos.get("entry_opt_price") or 0.0) or None,
                    "stop_loss": float(_pos.get("sl_price") or 0.0) or None,
                    "target": float(_pos.get("target_price") or _pos.get("t1") or 0.0) or None,
                    "current_price": float(_pos.get("live_ltp") or _pos.get("entry_opt_price") or 0.0) or None,
                    "type": _dir,
                })
            _pov_st = "IN_TRADE" if positions else "IDLE"
            write_status(
                _pov_st,
                active_trades=_pov_trades,
                indicators={
                    "regime": f"ATM {atm_strike}",
                    "phase": "IN_TRADE" if positions else "SCANNING",
                    "spot": float(underlying_ltp or 0.0) if underlying_ltp else None,
                },
                last_message=f"Underlying LTP: {underlying_ltp}, ATM Strike: {atm_strike}"
            )

            # Define 6 option legs to track (ATM-2 to ATM+2)
            legs = [
                ("CE", "OTM2"),
                ("CE", "OTM1"),
                ("CE", "ATM"),
                ("PE", "ATM"),
                ("PE", "OTM1"),
                ("PE", "OTM2"),
            ]

            # 3. Resolve symbols and evaluate POV pattern for each leg
            today_str = date.today().strftime("%Y-%m-%d")

            for option_type, offset in legs:
                symbol = get_option_symbol(UNDERLYING, idx_exchange, expiry, offset, option_type)
                if not symbol:
                    continue

                log.info(f"Tracking leg: {symbol} ({option_type} {offset})")

                # Fetch 1m candles for the option contract
                df_opt = client.history(
                    symbol=symbol,
                    exchange=opt_exchange,
                    interval="1m",
                    start_date=today_str,
                    end_date=today_str
                )

                if not isinstance(df_opt, pd.DataFrame) or df_opt.empty:
                    continue

                # DataFrame index is a tz-aware timestamp; sort to guarantee order
                df_opt = df_opt.sort_index().reset_index(drop=True)

                # Evaluate POV short-squeeze pattern
                res = evaluate_pov(symbol, df_opt, is_midcp)
                log.info(f"Symbol: {symbol} | Action: {res['action']} | Score: {res['score']}/5")

                # 4. Manage active position exits
                pos = positions.get(symbol)
                if pos:
                    sl_oid = pos.get("sl_orderid")
                    target_price = pos.get("target_price")

                    # Check if SL order already filled
                    sl_filled = False
                    if sl_oid:
                        try:
                            st = client.orderstatus(order_id=sl_oid, strategy=STRATEGY_NAME)
                            if st.get("status") == "success" and st.get("data", {}).get("order_status") == "complete":
                                sl_filled = True
                        except Exception:
                            pass

                    if sl_filled:
                        log.info(f"SL filled for {symbol}. Position closed by system.")
                        sl_fill = fetch_fill_price(sl_oid, context=f"(SL {symbol})")
                        _src = "fill"
                        if sl_fill is None:
                            sl_fill = pos.get("sl_price")  # trigger price as last resort
                            _src = "est-trigger"
                        consecutive_losses, daily_loss_rs = book_trade_pnl(
                            symbol, sl_fill, pos.get("entry_opt_price"),
                            pos.get("qty", QUANTITY), consecutive_losses, daily_loss_rs,
                            source=_src)
                        release_symbol_lock(symbol, STRATEGY_NAME)
                        del positions[symbol]
                        persist_positions(positions)
                        sync_direction_locks(positions, STRATEGY_NAME, UNDERLYING)
                        continue

                    # Live option LTP, fetched once per cycle for this position.
                    # Exit checks MUST use the live price, not df_opt.iloc[-2]["close"]:
                    # the completed-candle close left the strategy blind for ~1-2 min, so
                    # T1 was only seen long after it was crossed (bug 2026-07-28: T1 40.10
                    # first observed at 48.90; the market exit then filled at 37.80).
                    # fetch_option_ltp() spot-sanity-checks the quote before returning it.
                    live_ltp = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)
                    if live_ltp is None and len(df_opt) >= 2:
                        live_ltp = float(df_opt.iloc[-2]["close"])  # degraded fallback

                    # Target hit -> close and book P&L from the ACTUAL fill
                    if target_price is not None and live_ltp is not None and live_ltp >= target_price:
                        log.info(f"Target reached for {symbol}! LTP {live_ltp:.2f} >= T1 {target_price:.2f}")
                        outcome, fill_px = verified_exit_sell(
                            symbol, opt_exchange, pos.get("qty", QUANTITY), sl_oid, "Target-exit")
                        if outcome == "unknown":
                            continue  # broker unverifiable — keep tracking, retry next cycle
                        if outcome == "sold":
                            consecutive_losses, daily_loss_rs = book_trade_pnl(
                                symbol, fill_px if fill_px is not None else live_ltp,
                                pos.get("entry_opt_price"), pos.get("qty", QUANTITY),
                                consecutive_losses, daily_loss_rs,
                                source="fill" if fill_px is not None else "est-ltp")
                        release_symbol_lock(symbol, STRATEGY_NAME)
                        del positions[symbol]
                        persist_positions(positions)
                        sync_direction_locks(positions, STRATEGY_NAME, UNDERLYING)

                    # Premium path, one line per monitored cycle. POV trades the
                    # FRONT weekly, so its contracts drop out of the broker's
                    # master within days and no post-hoc study can reconstruct
                    # what a position did between entry and exit: of 28 round
                    # trips only 2 were still replayable on 2026-08-07. Without
                    # this line there is no way to tell whether POV gives back
                    # open profit (the question that produced Judas's break-even
                    # ratchet) -- live_ltp is already fetched every cycle here
                    # and was simply being discarded.
                    if live_ltp is not None and pos.get("entry_opt_price"):
                        _e = float(pos["entry_opt_price"])
                        _sl = pos.get("sl_price")
                        _r = (_e - float(_sl)) if _sl else 0.0
                        log.info(
                            f"PATH {symbol} ltp={live_ltp:.2f} entry={_e:.2f} "
                            f"R={_r:.2f} rmult={((live_ltp - _e) / _r):+.2f}"
                            if _r > 0 else
                            f"PATH {symbol} ltp={live_ltp:.2f} entry={_e:.2f}"
                        )

                    # ── Safety-net exits: max-hold-time and premium-decay ──
                    # Prevents slow-bleed when SL is cancelled (e.g. by RECONCILE on restart)
                    # and position sits unmonitored. Based on live evidence 2026-07-02:
                    # 3 SENSEX PE legs held 3+ hours lost 75-80% because SLs were cancelled
                    # at process restart and nobody closed them until premiums hit near-zero.
                    if symbol in positions and live_ltp is not None:
                        opt_ltp = live_ltp
                        entry_opt_price = pos.get("entry_opt_price")
                        entry_time = pos.get("entry_time")
                        _force_exit = None
                        # Max-hold: close if held > MAX_HOLD_MINUTES without hitting T1
                        if entry_time is not None:
                            hold_mins = (datetime.now() - entry_time).total_seconds() / 60.0
                            if hold_mins >= MAX_HOLD_MINUTES:
                                _force_exit = f"Max-hold exit ({hold_mins:.0f}min ≥ {MAX_HOLD_MINUTES}min)"
                        # Premium-decay: close if LTP < DECAY_EXIT_PCT of entry
                        if _force_exit is None and entry_opt_price is not None and entry_opt_price > 0:
                            if opt_ltp < entry_opt_price * DECAY_EXIT_PCT:
                                _force_exit = f"Decay exit (LTP {opt_ltp:.2f} < {DECAY_EXIT_PCT:.0%} of entry {entry_opt_price:.2f})"
                        if _force_exit:
                            log.warning(f"!!! {_force_exit} !!! Closing {symbol}...")
                            outcome, fill_px = verified_exit_sell(
                                symbol, opt_exchange, pos.get("qty", QUANTITY), sl_oid, "Force-exit")
                            if outcome == "unknown":
                                continue  # broker unverifiable — keep tracking, retry next cycle
                            if outcome == "sold":
                                consecutive_losses, daily_loss_rs = book_trade_pnl(
                                    symbol, fill_px if fill_px is not None else opt_ltp,
                                    entry_opt_price, pos.get("qty", QUANTITY),
                                    consecutive_losses, daily_loss_rs,
                                    source="fill" if fill_px is not None else "est-ltp")
                            release_symbol_lock(symbol, STRATEGY_NAME)
                            del positions[symbol]
                            persist_positions(positions)
                            sync_direction_locks(positions, STRATEGY_NAME, UNDERLYING)
                    continue  # skip entry check while in a position

                # 5. Trigger trades on STRONG / WATCH transitions
                # Halt entries if circuit breaker is tripped
                if halted:
                    continue

                if res["score"] >= POV_MIN_SCORE and res["is_new"] and res["entry"] is not None:
                    if trades_today >= POV_MAX_TRADES_PER_DAY:
                        log.info(f"Daily trade cap reached ({trades_today}/{POV_MAX_TRADES_PER_DAY}) for {UNDERLYING}. Skipping {symbol} entry.")
                        continue
                    if not positions.get(symbol):
                        # One directional view per underlying across ALL strategies:
                        # never buy a PE here while another strategy holds a CE.
                        _side = "CE" if str(symbol).endswith("CE") else "PE"
                        if not acquire_direction_lock(UNDERLYING, _side, STRATEGY_NAME):
                            continue
                        # Symbol lock: skip if another strategy holds this symbol
                        if not acquire_symbol_lock(symbol, STRATEGY_NAME):
                            log.info(f"Symbol {symbol} locked by another strategy. Skipping this signal.")
                            continue

                        # Capture entry option price (validated; needed for P&L + auto-lot)
                        entry_opt_price = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)

                        # Compute entry quantity based on LOT_MODE
                        if LOT_MODE == "auto" and entry_opt_price is not None and res["sl"] is not None:
                            capital = fetch_available_capital()
                            if capital is not None and capital > 0:
                                # POV uses premium-based SL → max loss = entry - SL per unit
                                max_loss_per_unit = max(entry_opt_price - res["sl"], 0.5)
                                lots = compute_auto_lots(capital, RISK_PCT_PER_TRADE, max_loss_per_unit, LOT_SIZE, MAX_LOTS)
                                entry_qty = lots * LOT_SIZE
                                log.info(f"AUTO-LOT: capital ₹{capital:.0f} | risk {RISK_PCT_PER_TRADE}% | loss/unit ₹{max_loss_per_unit:.2f} → {lots} lots × {LOT_SIZE} = {entry_qty} qty")
                            else:
                                entry_qty = LOT_SIZE
                                log.warning("AUTO-LOT: capital unavailable, falling back to 1 lot")
                        else:
                            entry_qty = LOT_SIZE * MAX_LOTS

                        log.info(f"!!! SHORT SQUEEZE DETECTED on {symbol} !!! Placing BUY order (qty={entry_qty})...")
                        order_resp = client.placeorder(
                            strategy=STRATEGY_NAME,
                            symbol=symbol,
                            action="BUY",
                            exchange=opt_exchange,
                            price_type="MARKET",
                            product=PRODUCT,
                            quantity=entry_qty
                        )
                        log.info(f"Entry Order Response: {order_resp}")
                        if order_resp.get("status") == "success":
                            # ACCEPTANCE IS NOT A FILL. placeorder returning
                            # success only means the order was taken; the
                            # exchange can still reject it. On 2026-08-14
                            # SENSEX20AUG2677800CE was accepted, then REJECTED,
                            # and the old code could not tell that apart from
                            # "fill price unreadable" -- both surfaced as None.
                            # It fell back to the pre-trade QUOTE, armed a stop
                            # and logged "Trade entered ... Opt entry: 499.45"
                            # for a position that never existed.
                            _state, _fill_entry = confirm_entry_fill(
                                order_resp.get("orderid"), symbol, context=f"(entry {symbol})")
                            if _state == "dead":
                                log.error(
                                    "ENTRY NOT FILLED for %s (order %s came back %s) — "
                                    "no stop armed, no position recorded.",
                                    symbol, order_resp.get("orderid"), _state)
                                release_symbol_lock(symbol, STRATEGY_NAME)
                                continue
                            if _fill_entry is not None:
                                entry_opt_price = _fill_entry
                            elif _state == "unknown":
                                # Treat as LIVE deliberately: an unconfirmed order
                                # may still fill, and an untracked real position
                                # with no stop is far worse than a phantom one.
                                # RECONCILE resolves it either way now.
                                log.warning(
                                    "Entry fill UNCONFIRMED for %s (order %s) — tracking as "
                                    "live on the pre-trade quote %.2f; RECONCILE will settle it.",
                                    symbol, order_resp.get("orderid"), float(entry_opt_price or 0))
                            sl_orderid = None

                            # Place the protective stop as SL (stop-LIMIT).
                            # SL-M is rejected outright for options (measured
                            # 33/33 rejections), and the API used to report that
                            # as success with orderid=null - which silently left
                            # positions with no stop at all.
                            if res["sl"] is not None:
                                _trg = float(res["sl"])
                                _lim = max(0.05, round(round((_trg * (1.0 - SL_LIMIT_BUFFER_PCT / 100.0))
                                                             / 0.05) * 0.05, 2))
                                sl_resp = client.placeorder(
                                    strategy=STRATEGY_NAME,
                                    symbol=symbol,
                                    action="SELL",
                                    exchange=opt_exchange,
                                    price_type="SL",
                                    trigger_price=_trg,
                                    price=_lim,
                                    product=PRODUCT,
                                    quantity=entry_qty
                                )
                                log.info(f"SL Order Response: {sl_resp}")
                                _oid = sl_resp.get("orderid") if isinstance(sl_resp, dict) else None
                                if sl_resp.get("status") == "success" and _oid:
                                    sl_orderid = _oid
                                    log.info(f"SL armed for {symbol} @ trigger {_trg} "
                                             f"limit {_lim} — order {_oid}")
                                else:
                                    log.error(f"SL NOT PLACED for {symbol} — position is "
                                              f"UNPROTECTED at the broker: {sl_resp}")

                            positions[symbol] = {
                                "qty": entry_qty,
                                "sl_orderid": sl_orderid,
                                # RECONCILE needs to be able to ask the broker
                                # what actually happened to the ENTRY before it
                                # will cancel a protective stop. Without this it
                                # can only see a positionbook miss, which on
                                # 2026-08-14 pruned two live legs.
                                "entry_orderid": order_resp.get("orderid"),
                                "sl_price": res["sl"],
                                "target_price": res["t1"],
                                "entry_opt_price": entry_opt_price,
                                "entry_time": datetime.now(),
                            }
                            trades_today += 1
                            persist_positions(positions)
                            sync_direction_locks(positions, STRATEGY_NAME, UNDERLYING)
                            log.info(f"Trade entered: {symbol} | SL: {res['sl']} | T1: {res['t1']} | T2: {res['t2']} | T3: {res['t3']} | Opt entry: {entry_opt_price}")
                            # REALISED geometry, not the intended geometry.
                            # sl/t1/t2/t3 are computed from the SIGNAL CANDLE
                            # CLOSE, but the position fills elsewhere, and the
                            # stop is never re-derived from the fill. Measured
                            # over the first 6 live entries, fills deviated from
                            # the signal close by -15.8% to +4.1%, so T1 -- which
                            # the code believes is always 1.5R -- actually landed
                            # anywhere from 0.72R to 6.36R, and on 2 of 6 trades
                            # T1 paid LESS than the stop risked.
                            #
                            # This is the same class as Judas's MIN_EFFECTIVE_RR
                            # bug (stated R:R != actual R:R), reached via fill
                            # slippage rather than stop flooring. Judas is immune
                            # because its geometry is in SPOT, which an option
                            # fill cannot move.
                            #
                            # Logged, deliberately NOT corrected: POV is the only
                            # positive-expectancy strategy here (+Rs 108/trade)
                            # and it earned that WITH this geometry, so silently
                            # moving its targets is an untested change. This line
                            # collects the evidence to decide on.
                            try:
                                _rk = float(entry_opt_price) - float(res["sl"])
                                if _rk > 0:
                                    _r1 = (float(res["t1"]) - float(entry_opt_price)) / _rk
                                    _slpct = 100.0 * _rk / float(entry_opt_price)
                                    log.info("GEOMETRY %s fill=%.2f sl=%.2f risk=%.2f "
                                             "(%.1f%% of premium) T1=%.2f -> %.2fR actual "
                                             "(intended 1.50R)", symbol, entry_opt_price,
                                             res["sl"], _rk, _slpct, res["t1"], _r1)
                                    if _r1 < 1.0:
                                        log.warning("GEOMETRY INVERTED on %s: T1 pays "
                                                    "%.2fR against 1.00R of risk -- the "
                                                    "first target returns less than the "
                                                    "stop risks", symbol, _r1)
                                else:
                                    log.warning("GEOMETRY %s: stop is at or above the "
                                                "fill (fill %.2f, sl %.2f) -- risk is "
                                                "not measurable", symbol,
                                                entry_opt_price, res["sl"])
                            except Exception as _gerr:
                                log.debug("geometry log failed: %s", _gerr)
                            # Tape context at entry -- diagnostic only, never a
                            # gate. The OI x price quadrant from openmtops'
                            # narrative.py: it is the read POV's own edge lives
                            # in (positioning, not price geometry), and having
                            # it on every fill lets the exit study ask whether
                            # outcomes separate by what the tape was doing.
                            _q, _doi, _dpx = oi_price_quadrant(df_opt)
                            log.info(
                                "TAPE %s quadrant=%s dOI=%+.1f%% dPx=%+.1f%% (%dm window)",
                                symbol, _q or "unclear", _doi, _dpx, QUAD_WINDOW,
                            )
                        else:
                            # Entry failed — release lock so other strategies can try
                            release_symbol_lock(symbol, STRATEGY_NAME)

        except Exception as e:
            log.error(f"Error in strategy execution loop: {e}")

        # Poll every 15 seconds
        time.sleep(15)


if __name__ == "__main__":
    run_strategy()
