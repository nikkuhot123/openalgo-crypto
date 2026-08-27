#!/usr/bin/env python
"""
PDH/PDL/PMH/PML + EMA 9/21 Strategy for NIFTY and SENSEX index options.

The user's daily-bias playbook, mechanised exactly as encoded in
backtesting/prior_levels_ema/backtest.py (OpenAlgo-native harness) and
backtesting/prior_levels_ema/volrix_strategy.py (Volrix premium harness):

  PDH / PDL  = previous session's daily high / low          -> STRONG bias
  PMH / PML  = today's pre-market range (09:15..09:30) H/L -> LIGHT bias
  EMA 9/21   = ewm(span, adjust=False) on 15m closes; a bias fires only when
               the fast EMA sits on the same side (use_ema gate, per symbol).
  Tier rule  = close > PDH (or > PMH when inside) -> CE; close < PDL (or < PML)
               -> PE; inside the PM range -> stand down.

Modes: OVERNIGHT (entry near the close, carry through the gap, exit next open —
the only mode that backtests positive) and INTRADAY (first aligned close beyond
a level in [09:30, 14:30), EOD square-off — kept for completeness; it is NEGATIVE
in every backtest, see verdict). Set MODE=intraday to trade it explicitly.

Both indices run from this one script — UNDERLYING=NIFTY or UNDERLYING=SENSEX
(exchange, option exchange, lot size and expiry resolve from it). Run two
instances to trade both.

BACKTEST VERDICT -- DO NOT DEPLOY WITH REAL MONEY.

1) OpenAlgo-native harness (directional edge on the index, futures proxy,
   0.018% fees + Rs 20/order, 13 months of 1m data 2025-06-23..2026-07-28):

       NIFTY  intraday  every grid variant        PF 0.59-0.83  net all negative
       SENSEX intraday  every grid variant          PF 0.70-...  net all negative

       NIFTY  overnight 15m EMA-on SL 0.2% RR 2 exit 09:30
              full 224 t  +126,867 Rs  PF 1.18  Sharpe 1.78
              H1 (Jun-Dec 2025)        PF 0.85  net -36,613   <- NEGATIVE
              H2 (Jan-Jul 2026)        PF 1.35  net +163,479
       SENSEX overnight 15m EMA-off SL 0.2% RR 2 exit 09:20
              full 106 t  +108,395 Rs  PF 1.45  Sharpe 3.23
              H1 +83,252 (PF 1.60) | H2 +7,769 (PF 1.08)     <- positive both halves
       SENSEX overnight 5m  EMA-on exit 09:20  H2 -26,983 (PF 0.73) -> fragile

   The NIFTY edge is a 2026-regime phenomenon; its 2025 half loses. SENSEX 15m
   is the only config positive in BOTH halves of its window. Weekday shape of
   the winners: Friday carries most of the book (weekend gap), Wed/Thu flat.

2) Volrix premium harness (real ATM weekly option premiums, 15m, 1 lot,
   0.5% option slippage/side + Rs 20/order, expiry-day entries skipped;
   free-tier windows: IS 2026-02-02..06-05, OOS 2026-06-08..08-04):

                    trades   net Rs    PF     win%
    NIFTY  IS          56   +52,041  1.34   46.4
    NIFTY  OOS         27   +11,482  1.31   44.4
    SENSEX IS          57  +119,701  1.78   47.4
    SENSEX OOS         28    +5,566  1.12   46.4
    NIFTY  INTRA IS    83   -29,120  0.76   44.6   <- intraday bleeds
                                                     (theta+spread, not just edge)

   Both indices are positive in-sample AND out-of-sample AT REAL PREMIUMS;
   SENSEX OOS is barely positive (PF 1.12, +5.6k over 28 trades). Intraday is
   negative everywhere; do not trade that mode with money.

   Cross-engine agreement: the directionality is real and the overnight gap is
   where it pays. The 2025 NIFTY half, the SENSEX 5m second half and the OOS
   fragility all say: at most a small, weekend-oriented, SENSEX-leaning
   overnight book; the consistency the user wants is NOT there yet.

Treat this file as a faithful encoding of the playbook — not as a validated
system. Run it in analyzer mode first.
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

api_key = os.getenv("OPENALGO_API_KEY")
host = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
ws_url = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")

if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=api_key, host=host, ws_url=ws_url)

# ---------------------------------------------------------------- parameters
STRATEGY_NAME = "PDH-PML EMA"
UNDERLYING = os.getenv("UNDERLYING", "NIFTY").upper()
MODE = os.getenv("MODE", "overnight").lower()  # 'overnight' | 'intraday'
# MIS is an INTRADAY product: the broker force-squares it around 15:20-15:30,
# which would close an overnight carry the same evening it was opened and
# delete the only edge this strategy has (the gap). Overnight therefore
# defaults to NRML; intraday keeps MIS.
PRODUCT = os.getenv("PRODUCT", "NRML" if MODE == "overnight" else "MIS")
QUANTITY = int(os.getenv("QUANTITY", "0"))  # 0 = detect the contract lot size
MAX_LOTS = int(os.getenv("MAX_LOTS", "1"))
LOT_MODE = os.getenv("LOT_MODE", "manual").lower()  # 'manual' | 'auto'
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", "1.0"))
LOT_SIZE = QUANTITY  # contract size; order qty = lots * LOT_SIZE
# Seconds to wait for the exchange master to publish a lot size on a cold
# start. The 09:10 schedule start precedes the master being queryable.
LOT_SIZE_WAIT_SECS = int(os.getenv("LOT_SIZE_WAIT_SECS", "600"))
# Strike selection passed to optionsymbol. MUST be one of ATM / ITM1-ITM50 /
# OTM1-OTM50 as a STRING -- the endpoint rejects numbers ("Not a valid
# string"). Both harnesses priced the ATM weekly, so ATM is the default.
STRIKE_OFFSET = os.getenv("STRIKE_OFFSET", "ATM")
# Both harnesses take exactly ONE entry per session -- keep live in step.
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "1"))
DRY_RUN = os.getenv("DRY_RUN", "1") in ("1", "true", "yes")

_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}


def _index_exchange(underlying: str) -> str:
    return "BSE_INDEX" if underlying.upper() in _BSE_UNDERLYINGS else "NSE_INDEX"


def _option_exchange(underlying: str) -> str:
    return "BFO" if underlying.upper() in _BSE_UNDERLYINGS else "NFO"


# Per-underlying backtested winners (overnight 15m), env vars override.
# SENSEX: EMA off (levels alone), exit 09:20. NIFTY: EMA on, exit 09:30.
_DEF_EXIT = {"NIFTY": "09:30", "SENSEX": "09:20"}
_DEF_EMA = {"NIFTY": "on", "SENSEX": "off"}

USE_EMA = os.getenv("USE_EMA", _DEF_EMA[UNDERLYING]).lower() == "on"
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
TIERS = os.getenv("TIERS", "both").lower()  # 'both' | 'strong'
PM_START = dtime(*[int(x) for x in os.getenv("PM_START", "09:15").split(":")])
PM_WINDOW = int(os.getenv("PM_WINDOW_MIN", "15"))  # minutes of PM range

EXIT_TIME = dtime(*[int(x) for x in os.getenv("EXIT_TIME", _DEF_EXIT[UNDERLYING]).split(":")])
ENTRY_TIME = dtime(*[int(x) for x in os.getenv("ENTRY_TIME", "15:05").split(":")])
ENTRY_END = dtime(*[int(x) for x in os.getenv("ENTRY_END", "14:30").split(":")])
EOD_EXIT = dtime(*[int(x) for x in os.getenv("EOD_EXIT", "15:10").split(":")])
# CAS (SEBI circular 2026-01-16, live 2026-08-03): spot stops updating at 15:15
# and teleports on the ~15:28 auction stamp. Entries and EOD exits must complete
# while spot is live; options trade to 15:40 so a 15:10 exit fills.

# Spot stop / target as FRACTION of spot (backtest winner: 0.2% / 0.4%).
SL_FRAC = float(os.getenv("SL_FRAC", "0.002"))
TGT_FRAC = float(os.getenv("TGT_FRAC", "0.004"))
# Premium-side protective stop as % of entry premium (approx delta-adjusted so
# it mirrors the 0.2% spot stop on an ATM option).
PREMIUM_SL_PCT = float(os.getenv("PREMIUM_SL_PCT", "0.25"))
OPT_COST_PCT = float(os.getenv("OPT_COST_PCT", "0.12"))  # statutory, % of turnover
SPREAD_PCT_OF_PREMIUM = float(os.getenv("SPREAD_PCT_OF_PREMIUM", "0.5"))
# Skip the traded weekly's expiry day: Volrix showed DTE-0 ATM premium is ~1/3
# of a normal day's and its expiry-day rows were the only distorted ones.
SKIP_EXPIRY_DAY_UNDERLYINGS = {
    s.strip().upper()
    for s in os.getenv("SKIP_EXPIRY_DAY_UNDERLYINGS", "NIFTY, SENSEX").split(",")
    if s.strip()
}


LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"prior_levels_ema_{UNDERLYING.upper()}.json"


# ------------------------------------------------------------ locks + state
def _strategy_slug(name):
    return re.sub(r'[^A-Za-z0-9]+', '_', str(name)).strip('_')


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:
        return False


def _lock_is_stale(ts_str, pid):
    """Stale if past TTL or owner process died. A live owner keeps the lock."""
    if pid and _pid_alive(pid):
        try:
            age = (datetime.now() - datetime.fromisoformat(str(ts_str))).total_seconds()
        except (ValueError, TypeError):
            age = None
        if age is not None and age < 86400:
            return False  # live owner within 24h is valid (covers overnight carry)
    try:
        when = datetime.fromisoformat(str(ts_str))
        if (datetime.now() - when).total_seconds() > 24 * 3600:
            return True
    except Exception:
        return True
    return not _pid_alive(pid)


def _read_lock(path):
    """(owner, iso_ts, pid) from a lock file, in EITHER convention."""
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


def acquire_instance_lock(underlying, strategy_name):
    """Ensure only ONE instance of this strategy runs on an underlying."""
    if DRY_RUN:
        return True
    lock_file = LOCKS_DIR / f"{underlying}.lock"
    try:
        if lock_file.exists():
            owner, ts, pid = _read_lock(lock_file)
            if owner == strategy_name and _pid_alive(pid):
                return True
            if not _lock_is_stale(ts, pid):
                log.warning("instance lock held by %s pid %s (%s)", owner, pid, underlying)
                return False
        lock_file.write_text(f"{strategy_name}|{datetime.now().isoformat()}|{os.getpid()}")
        return True
    except Exception as e:
        log.error("acquire_instance_lock failed: %s", e)
        return False


def release_instance_lock(underlying, strategy_name):
    if DRY_RUN:
        return
    lock_file = LOCKS_DIR / f"{underlying}.lock"
    try:
        if lock_file.exists():
            owner, ts, pid = _read_lock(lock_file)
            if owner == strategy_name and (not pid or pid == os.getpid()):
                lock_file.unlink()
    except Exception as e:
        log.error("release_instance_lock failed: %s", e)


def acquire_symbol_lock(symbol, strategy_name):
    """Claim one OPTION CONTRACT. True if acquired, already ours, or holder is stale."""
    if DRY_RUN:
        return True
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists():
            owner, ts, pid = _read_lock(lock_file)
            if owner is None:
                log.warning("unreadable contract lock on %s -- standing aside", symbol)
                return False
            if owner == strategy_name:
                return True
            if not _lock_is_stale(ts, pid):
                log.warning("contract lock held by %s pid %s (%s)", owner, pid, symbol)
                return False
            log.warning("stale contract lock on %s (owner '%s') -- reclaiming", symbol, owner)
        lock_file.write_text(f"{strategy_name}|{datetime.now().isoformat()}|{os.getpid()}")
        return True
    except Exception as e:
        log.error("acquire_symbol_lock failed: %s", e)
        return False


def release_symbol_lock(symbol, strategy_name):
    if DRY_RUN or not symbol:
        return
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists():
            owner, ts, pid = _read_lock(lock_file)
            if owner == strategy_name and (not pid or pid == os.getpid()):
                lock_file.unlink()
    except Exception as e:
        log.error("release_symbol_lock failed: %s", e)


def acquire_direction_lock(underlying, side, strategy_name):
    """Claim a DIRECTION (CE/PE) on an underlying. False if another strategy is
    already positioned the opposite way."""
    if DRY_RUN:
        return True
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
                continue  # never block on ourselves
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
                log.info("DIRECTION CONFLICT on %s: '%s' already holds %s, we want %s -- standing aside",
                         und, owner, held, want)
                return False
    except Exception as e:
        log.debug("direction lock scan failed: %s", e)
    try:
        (LOCKS_DIR / f"{und}.{me}.{want}.dir").write_text(
            f"{datetime.now().isoformat()}|{os.getpid()}")
    except Exception:
        pass
    return True


def release_direction_lock(underlying, strategy_name, side=None):
    if DRY_RUN:
        return
    und = str(underlying).upper()
    me = _strategy_slug(strategy_name)
    pat = f"{und}.{me}.*.dir" if side is None else f"{und}.{me}.{str(side).upper()}.dir"
    try:
        for f in LOCKS_DIR.glob(pat):
            f.unlink()
    except Exception:
        pass



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

def persist_state(trade, day):
    try:
        STATE_FILE.write_text(json.dumps({"trade": trade or {}, "day": day or {}}))
    except Exception as e:
        log.error("persist_state failed: %s", e)


def load_state():
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            return data.get("trade") or {}, data.get("day") or {}
    except Exception:
        pass
    return {}, {}


def entries_used(day_state, day):
    """How many entries the strategy has already taken on `day`."""
    try:
        return int((day_state or {}).get(str(day), 0))
    except (TypeError, ValueError):
        return 0


def day_budget_left(day_state, day, cap=None):
    """False once the session's entry budget is spent (harness: one per day)."""
    return entries_used(day_state, day) < (MAX_TRADES_PER_DAY if cap is None else cap)


def mark_entry(day_state, day):
    """Count an entry against `day`; older sessions are dropped."""
    used = entries_used(day_state, day)
    day_state.clear()
    day_state[str(day)] = used + 1
    return day_state



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

def reconcile_orphan_position(underlying):
    """Adopt an open broker position on this underlying after a restart."""
    try:
        pb = client.positionbook()
        rows = (pb.get("data") or pb.get("positions") or []) if isinstance(pb, dict) else []
        for row in rows:
            sym = str(row.get("symbol") or row.get("tradingsymbol") or "")
            qty = abs(int(row.get("quantity") or row.get("qty") or 0))
            signed = int(row.get("quantity") or row.get("qty") or 0)
            if sym.startswith(underlying) and qty > 0:
                side = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else "long"
                return {
                    "symbol": sym,
                    "side": side,
                    "qty": qty,
                    "entry_px": float(row.get("average_price") or row.get("average") or 0),
                    "entry_spot": None,
                    "entry_day": str(date.today()),
                    "sl_oid": None,
                    "adopted": True,
                    "sign": 1 if signed > 0 else -1,
                }
    except Exception as e:
        log.warning("reconcile_orphan_position: %s", e)
    return None


def live_position_qty(underlying, symbol):
    """Broker qty on `symbol`: >0 held, 0 absent, None if unverifiable."""
    if DRY_RUN:
        return 1 if symbol == (_active_trade or {}).get("symbol") else 0
    try:
        pb = client.positionbook()
        if not isinstance(pb, dict):
            return None
        rows = pb.get("data") or pb.get("positions") or []
        for row in rows:
            if str(row.get("symbol") or row.get("tradingsymbol") or "") == symbol:
                return abs(int(row.get("quantity") or row.get("qty") or 0))
        return 0  # positionbook OK, symbol absent -> flat
    except Exception as e:
        log.warning("live_position_qty: %s", e)
        return None


# ------------------------------------------------------------ broker helpers
def fetch_available_capital():
    try:
        resp = client.funds()
        if isinstance(resp, dict) and resp.get("status") == "success":
            data = resp.get("data") or {}
            for key in ("availablecash", "available_cash", "cash", "balance"):
                if data.get(key) is not None:
                    return float(data[key])
    except Exception as e:
        log.warning("fetch_available_capital: %s", e)
    return None


def compute_auto_lots(capital, risk_pct, max_loss_per_unit, lot_size, hard_cap_lots):
    """Lot count from the risk budget; max_loss_per_unit is Rs per contract."""
    if not capital or max_loss_per_unit <= 0 or lot_size <= 0:
        return 1
    budget = capital * risk_pct / 100.0
    return max(1, min(int(budget / (max_loss_per_unit * lot_size)), hard_cap_lots))


def fetch_option_ltp(opt_symbol, opt_exchange, max_retries=3, retry_delay=1.0):
    for attempt in range(max_retries):
        try:
            # client.quotes(), NOT client.quote(). The SDK has no singular
            # method, so every call raised AttributeError, fetch_option_ltp
            # returned None on every invocation, and the strategy could never
            # price a leg -- no entry was possible from deployment until
            # 2026-08-07, including the first overnight carry. Every other
            # strategy in this repo already calls quotes(); this one was alone.
            resp = client.quotes(symbol=opt_symbol, exchange=opt_exchange)
            if isinstance(resp, dict) and resp.get("status") not in (None, "success"):
                log.warning("fetch_option_ltp %s: status=%s", opt_symbol, resp.get("status"))
                resp = {}
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict):
                ltp = data.get("ltp") or data.get("last_price") or data.get("close")
                if ltp:
                    return float(ltp)
        except Exception as e:
            log.warning("fetch_option_ltp attempt %s: %s", attempt + 1, e)
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    return None


def fetch_fill_price(order_id, symbol, max_retries=4, retry_delay=0.8):
    """Average TRADED price for an order. P&L must rest on fills, never quotes.

    Was client.orderhistory(), which the SDK does not have -- so this raised
    AttributeError on every attempt and always returned None, exactly like
    fetch_option_ltp. Mirrors judas_swing_strategy: orderstatus returns a DICT
    (not the list this used to walk), with tradebook as the fallback.
    """
    for attempt in range(max_retries):
        try:
            r = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
            d = (r.get("data") or {}) if isinstance(r, dict) else {}
            for k in ("average_price", "averageprice", "avgprice", "avg_price", "price"):
                v = d.get(k)
                if v not in (None, "", 0, "0") and float(v) > 0:
                    return float(v)
        except Exception as e:
            log.debug("orderstatus %s attempt %s: %s", order_id, attempt + 1, e)
        try:
            tb = client.tradebook()
            if isinstance(tb, dict) and tb.get("status") == "success":
                for t in tb.get("data", []) or []:
                    if str(t.get("orderid", "")) == str(order_id):
                        for k in ("average_price", "averageprice", "price", "tradeprice"):
                            v = t.get(k)
                            if v not in (None, "", 0, "0") and float(v) > 0:
                                return float(v)
        except Exception as e:
            log.debug("tradebook %s attempt %s: %s", order_id, attempt + 1, e)
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    log.warning("fetch_fill_price %s: no fill price after %s attempts", symbol, max_retries)
    return None


def safe_cancel_order(order_id, context=""):
    try:
        resp = client.cancelorder(order_id=order_id, strategy=STRATEGY_NAME)
        return isinstance(resp, dict) and resp.get("status") in ("success", "cancelled", "failed")
    except Exception as e:
        log.warning("safe_cancel_order %s: %s", context, e)
        return False


def statutory_cost(entry_px, exit_px, qty):
    if entry_px is None or exit_px is None or not qty:
        return 0.0
    turnover = 2 * (float(entry_px) + float(exit_px)) * qty
    return turnover * OPT_COST_PCT / 100.0


def _tick(px):
    return round(round(float(px) / 0.05) * 0.05, 2)


def breakeven_points(opt_premium, qty):
    """Index points spot must travel for the trade to reach zero (delta 0.5)."""
    if not opt_premium or not qty:
        return None
    stat = statutory_cost(opt_premium, opt_premium, qty)
    spread = SPREAD_PCT_OF_PREMIUM / 100.0 * opt_premium
    return (stat + spread) / (0.5 * qty) if qty else None


def check_entry_geometry(entry_spot, sl_spot, target_spot, opt_premium, qty):
    """Reject broken geometry BEFORE sending the order. (ok, reason, detail)."""
    be = breakeven_points(opt_premium, qty)
    risk = abs(entry_spot - sl_spot)
    reward = abs(target_spot - entry_spot)
    if not be:
        return False, "no-breakeven", {}
    if risk <= 0:
        return False, "zero-risk", {}
    detail = {"breakeven_pts": round(be, 2), "effective_rr": round(reward / risk, 2)}
    if reward <= 1.5 * be:
        return False, "target-below-breakeven", detail
    if reward / risk < 1.2:
        return False, "effective-rr-too-low", detail
    return True, None, detail


# ---------------------------------------------------------------- data+signal
def fetch_minute_history(underlying, idx_exchange, days=4):
    """1m OHLC for the last `days` sessions (live history, OPEN-timestamped)."""
    end = date.today()
    start = end - timedelta(days=days)
    cols = ["open", "high", "low", "close", "volume"]
    try:
        df = client.history(
            symbol=underlying,
            exchange=idx_exchange,
            interval="1m",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame(columns=cols)
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")
        for c in cols:
            if c not in df.columns:
                df[c] = pd.NA
        return df.sort_index()[cols]
    except Exception as e:
        log.warning("fetch_minute_history: %s", e)
        return pd.DataFrame(columns=cols)


def resample_15m(df_1m):
    """15m OHLC from 1m bars, matching the backtest harness resampling."""
    if df_1m is None or df_1m.empty:
        return df_1m
    return (
        df_1m.resample("15min", origin="start_day", offset="9h15min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def fetch_prior_levels(df_1m, today):
    """(pdh, pdl) from the last COMPLETED session's 1m-implied daily H/L."""
    if df_1m is None or df_1m.empty:
        return None, None
    s = df_1m[df_1m.index.date < today]
    if s.empty:
        return None, None
    last = s.index.date.max()
    day = s[s.index.date == last]
    return float(day["high"].max()), float(day["low"].min())


def pm_range(df_1m, today, pm_start, pm_end):
    """Pre-market high/low for rows with open time in [pm_start, pm_end)."""
    if df_1m is None or df_1m.empty:
        return None, None
    s = df_1m[df_1m.index.date == today]
    if s.empty:
        return None, None
    t = s.index.time
    wind = s[(t >= pm_start) & (t < pm_end)]
    if wind.empty:
        return None, None
    return float(wind["high"].max()), float(wind["low"].min())


def ema_state(closes, fast=EMA_FAST, slow=EMA_SLOW):
    """(fast_ema, slow_ema) at the series tail; ewm(adjust=False) like the harness."""
    if closes is None or closes.empty:
        return None, None
    return (
        float(closes.ewm(span=fast, adjust=False).mean().iloc[-1]),
        float(closes.ewm(span=slow, adjust=False).mean().iloc[-1]),
    )


def compute_bias(close, pdh, pdl, pmh, pml, fast_ema, slow_ema, tiers="both", use_ema=True):
    """Side from the tiered levels + optional EMA alignment.

    Mirrors build_signals() in the backtest harness exactly:
      strong bull = close > PDH, light bull = PMH < close <= PDH,
      strong bear = close < PDL, light bear = PDL <= close < PML.
    Returns ('CE' | 'PE' | None, tier_label).
    """
    tier = "neutral"
    if close is None or pdh is None or pdl is None:
        return None, tier
    strong_bull = close > pdh
    strong_bear = close < pdl
    if tiers == "strong":
        bull, bear = strong_bull, strong_bear
    else:
        light_bull = pmh is not None and pmh < close <= pdh
        light_bear = pml is not None and pdl <= close < pml
        bull, bear = strong_bull or light_bull, strong_bear or light_bear
    if use_ema:
        if fast_ema is None or slow_ema is None:
            return None, tier
        bull = bull and fast_ema > slow_ema
        bear = bear and fast_ema < slow_ema
    if bull:
        return "CE", "strong_bull" if strong_bull else "light_bull"
    if bear:
        return "PE", "strong_bear" if strong_bear else "light_bear"
    return None, tier


def compute_signal(levels, df_1m, mode=MODE, now=None):
    """The playbook on the latest CLOSED context.

    levels: dict with pdh/pdl/pmh/pml (pmh/pml may be None before 09:30).
    now   : when given, the bar still forming at `now` is dropped, so the bias
            only ever reads closed bars (the harnesses signal on bar close).
    mode == 'overnight': additionally restricted to bars opening before 15:00,
            i.e. the 14:45 bar is the signal bar for a 15:05 entry check.
    Returns (side, tier, detail) with detail for monitor logging.
    """
    if df_1m is None or df_1m.empty:
        return None, "neutral", {}
    d15 = resample_15m(df_1m)
    if d15.empty:
        return None, "neutral", {}
    if now is not None:
        cutoff = pd.Timestamp(now)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_localize(None)
        idx = d15.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        d15 = d15[idx + pd.Timedelta(minutes=15) <= cutoff]
    if mode == "overnight":
        d15 = d15[d15.index.time < dtime(15, 0)]
    if d15.empty:
        return None, "neutral", {}
    closes = d15["close"].astype(float)
    fast, slow = ema_state(closes)
    last = d15.iloc[-1]
    side, tier = compute_bias(
        float(last["close"]),
        levels.get("pdh"),
        levels.get("pdl"),
        levels.get("pmh"),
        levels.get("pml"),
        fast,
        slow,
        tiers=TIERS,
        use_ema=USE_EMA,
    )
    detail = {
        "pdh": levels.get("pdh"),
        "pdl": levels.get("pdl"),
        "pmh": levels.get("pmh"),
        "pml": levels.get("pml"),
        "close": float(last["close"]),
        "ema9": fast,
        "ema21": slow,
    }
    return side, tier, detail


# ------------------------------------------------------------ option helpers
def get_nearest_expiry(underlying, exchange):
    try:
        resp = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="options")
        if resp.get("status") == "success" and resp.get("data"):
            data = resp["data"]
            # optionsymbol wants the compact form: the expiry endpoint returns
            # "11-AUG-26" and the symbol endpoint answers 404 "No strikes
            # found" for it, but resolves "11AUG26" (verified live 2026-08-07).
            if isinstance(data, list):
                return str(data[0]).replace("-", "")
            if isinstance(data, dict):
                return str(data.get("expiry") or data.get("expiries") or "").replace("-", "")
    except Exception as e:
        log.warning("get_nearest_expiry: %s", e)
    return None


def is_expiry_today(expiry_str):
    """True when `expiry_str` (e.g. '31JUL26') is today."""
    if not expiry_str:
        return False
    try:
        m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", expiry_str)
        if not m:
            return False
        d, mth, yy = m.groups()
        months = {
            "JAN": 1,
            "FEB": 2,
            "MAR": 3,
            "APR": 4,
            "MAY": 5,
            "JUN": 6,
            "JUL": 7,
            "AUG": 8,
            "SEP": 9,
            "OCT": 10,
            "NOV": 11,
            "DEC": 12,
        }
        return date(2000 + int(yy), months[mth], int(d)) == date.today()
    except Exception:
        return False


def fetch_lot_size(underlying, opt_exchange):
    """Contract lot size, read from the optionsymbol response (it carries
    `lotsize` at the top level, e.g. 65 for NIFTY / 20 for SENSEX)."""
    try:
        expiry = get_nearest_expiry(underlying, opt_exchange)
        if not expiry:
            return None
        resp = client.optionsymbol(
            underlying=underlying,
            exchange=opt_exchange,
            expiry_date=expiry,
            offset=STRIKE_OFFSET,
            option_type="CE",
        )
        if resp.get("status") == "success":
            d = resp.get("data") or resp
            val = d.get("lotsize") or d.get("lot_size") or 0
            return int(val) or None
        log.warning("fetch_lot_size: %s", str(resp)[:160])
    except Exception as e:
        log.warning("fetch_lot_size: %s", e)
    return None


def get_option_symbol(underlying, exchange, expiry, offset, option_type):
    """Resolve the tradable option symbol. Contract verified live 2026-08-07:

        optionsymbol(underlying, exchange, expiry_date='11AUG26',
                     offset='ATM', option_type='CE')
        -> {"status":"success","symbol":"NIFTY11AUG2624550CE","lotsize":65,...}

    `offset` is REQUIRED and must be a STRING (ATM / ITM1-50 / OTM1-50) --
    passing a number returns "Not a valid string", and omitting it raises
    TypeError before the request is even sent. The symbol comes back at the
    top level, not under "data".
    """
    try:
        resp = client.optionsymbol(
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry,
            offset=str(offset),
            option_type=option_type,
        )
        if resp.get("status") == "success":
            d = resp.get("data") or resp
            return str(d.get("symbol") or d.get("tradingsymbol") or "") or None
        log.warning("get_option_symbol: %s", str(resp)[:160])
    except Exception as e:
        log.warning("get_option_symbol: %s", e)
    return None


def place_premium_sl(opt_symbol, opt_exchange, qty, entry_px):
    """Broker-side stop-limit SELL at entry_px * (1 - PREMIUM_SL_PCT%)."""
    trig = _tick(entry_px * (1 - PREMIUM_SL_PCT / 100.0))
    try:
        resp = client.placeorder(
            symbol=opt_symbol,
            exchange=opt_exchange,
            transaction_type="SELL",
            quantity=qty,
            product_type=PRODUCT,
            strategy=STRATEGY_NAME,
            price_type="SL-LIMIT",
            price=trig,
            trigger_price=trig,
        )
        oid = (
            (resp.get("data") or {}).get("orderid") or resp.get("orderid")
            if isinstance(resp, dict)
            else None
        )
        if not oid:
            log.warning("premium SL not placed: %s", resp)
            return None, trig
        return oid, trig
    except Exception as e:
        log.error("premium SL error: %s", e)
        return None, trig


def verified_exit_sell(underlying, symbol, opt_exchange, qty, sl_oid, reason):
    """Cancel the protective SL and SELL only what the broker ACTUALLY holds.

    Returns (outcome, sold_qty, fill_price); outcome in
    ('sold' | 'flat' | 'unknown' | 'rejected').
    """
    if DRY_RUN:
        log.info("SHADOW exit %s %s qty=%s (%s)", underlying, symbol, qty, reason)
        return "sold", qty, None
    if sl_oid:
        safe_cancel_order(sl_oid, "exit " + reason)
    held = live_position_qty(underlying, symbol)
    if held is None:
        held = qty
    if held <= 0:
        return "flat", 0, None
    try:
        resp = client.placeorder(
            symbol=symbol,
            exchange=opt_exchange,
            transaction_type="SELL",
            quantity=held,
            product_type=PRODUCT,
            strategy=STRATEGY_NAME,
            price_type="MARKET",
            price=0.0,
        )
        oid = (
            (resp.get("data") or {}).get("orderid") or resp.get("orderid")
            if isinstance(resp, dict)
            else None
        )
        if not oid:
            log.error("exit SELL rejected: %s", resp)
            return "rejected", 0, None
        fill = None
        for _ in range(5):
            time.sleep(1)
            fill = fetch_fill_price(oid, symbol)
            if fill is not None:
                break
        return ("sold", held, fill) if fill is not None else ("sold", held, None)
    except Exception as e:
        log.error("exit SELL raised: %s", e)
        return "rejected", 0, None


# ---------------------------------------------------------------- run state
_shutdown_requested = False
_active_trade = {}
_day_state = {}
_opt_exchange = None


def _graceful_shutdown(signum, frame):
    name = signum
    try:
        name = signal.Signals(signum).name
    except Exception:
        pass
    log.info("SHUTDOWN SIGNAL (%s) -- cleaning up...", name)
    symbol = (_active_trade or {}).get("symbol")
    # An overnight carry MUST survive the shutdown. The platform stops a
    # scheduled strategy with SIGTERM at schedule_stop
    # (terminate_process_cross_platform -> process.terminate()), which lands
    # here minutes after the 15:05 entry. Squaring off would close the trade
    # the same evening and delete the gap -- the only thing that backtests
    # positive. The position is protected while this process is down by the
    # broker-side premium SL placed at entry, and tomorrow's session adopts
    # it from the state file and exits at EXIT_TIME (the carry branch).
    if MODE == "overnight" and symbol:
        persist_state(_active_trade, _day_state)
        log.info("overnight carry: leaving %s open for the next session "
                 "(broker SL %s guards it)", symbol,
                 (_active_trade or {}).get("sl_oid"))
        sys.exit(0)
    if symbol and _opt_exchange:
        outcome, qty, fill = verified_exit_sell(
            UNDERLYING,
            symbol,
            _opt_exchange,
            (_active_trade or {}).get("qty", 0),
            (_active_trade or {}).get("sl_oid"),
            "shutdown",
        )
        log.info("shutdown square-off: %s qty=%s fill=%s", outcome, qty, fill)
        if outcome == "sold":
            release_symbol_lock(symbol, STRATEGY_NAME)
            release_direction_lock(UNDERLYING, STRATEGY_NAME, (_active_trade or {}).get("side"))
    write_status("INACTIVE")
    release_instance_lock(UNDERLYING, STRATEGY_NAME)
    sys.exit(0)


signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)


def _latest_spot(df_1m):
    if df_1m is None or df_1m.empty:
        return None
    return float(df_1m["close"].iloc[-1])


def order_quantity(premium):
    """Order quantity in units. Manual: one lot. Auto: risk-budgeted lots.

    The risk per unit is the premium stop distance (PREMIUM_SL_PCT of the paid
    premium), so sizing uses the REAL premium at entry, not a guess.
    """
    if LOT_MODE != "auto" or QUANTITY > 0 or not premium:
        return LOT_SIZE
    risk_per_unit = float(premium) * PREMIUM_SL_PCT / 100.0
    lots = compute_auto_lots(
        fetch_available_capital(), RISK_PCT_PER_TRADE, risk_per_unit, LOT_SIZE, MAX_LOTS
    )
    return lots * LOT_SIZE


def _enter_position(side, reason, levels, df_1m, intraday=False):
    """Place the ATM weekly buy with a protective premium SL. Returns trade dict or None."""
    global _active_trade, _day_state
    if not acquire_direction_lock(UNDERLYING, side, STRATEGY_NAME):
        log.warning("direction lock busy for %s", side)
        return None
    try:
        expiry = get_nearest_expiry(UNDERLYING, _opt_exchange)
        if not expiry:
            log.warning("no expiry")
            return None
        sym = get_option_symbol(UNDERLYING, _opt_exchange, expiry, STRIKE_OFFSET, side)
        if not sym:
            log.warning("no option symbol")
            release_direction_lock(UNDERLYING, STRATEGY_NAME, side)
            return None
        if not acquire_symbol_lock(sym, STRATEGY_NAME):
            log.warning("contract lock busy for %s", sym)
            release_direction_lock(UNDERLYING, STRATEGY_NAME, side)
            return None
        prem = fetch_option_ltp(sym, _opt_exchange)
        if prem is None:
            log.warning("no premium for %s", sym)
            return None
        qty = order_quantity(prem)
        entry_spot = levels.get("close") or _latest_spot(df_1m)
        sl_spot = entry_spot * (1 - SL_FRAC) if side == "CE" else entry_spot * (1 + SL_FRAC)
        tgt_spot = entry_spot * (1 + TGT_FRAC) if side == "CE" else entry_spot * (1 - TGT_FRAC)
        ok, why, detail = check_entry_geometry(entry_spot, sl_spot, tgt_spot, prem, qty)
        if not ok:
            log.info("entry refused (%s) %s", why, detail)
            return None
        today = date.today()
        if DRY_RUN:
            trade = {
                "symbol": sym,
                "side": side,
                "qty": qty,
                "entry_px": prem,
                "entry_spot": entry_spot,
                "entry_day": str(today),
                "sl_oid": None,
                "intraday": intraday,
            }
            _active_trade = trade
            mark_entry(_day_state, today)
            persist_state(_active_trade, _day_state)
            log.info(
                "SHADOW entry %s %s qty=%s prem=%.2f spot=%.1f levels PDH=%s PDL=%s PMH=%s PML=%s",
                side,
                sym,
                qty,
                prem,
                entry_spot,
                levels.get("pdh"),
                levels.get("pdl"),
                levels.get("pmh"),
                levels.get("pml"),
            )
            return trade
        resp = client.placeorder(
            symbol=sym,
            exchange=_opt_exchange,
            transaction_type="BUY",
            quantity=qty,
            product_type=PRODUCT,
            strategy=STRATEGY_NAME,
            price_type="MARKET",
            price=0.0,
        )
        oid = (
            (resp.get("data") or {}).get("orderid") or resp.get("orderid")
            if isinstance(resp, dict)
            else None
        )
        if not oid:
            log.error("entry rejected: %s", resp)
            release_symbol_lock(sym, STRATEGY_NAME)
            release_direction_lock(UNDERLYING, STRATEGY_NAME, side)
            return None
        # the order is live from here: count it against the day before anything
        # else can fail, so a crash cannot buy a second lot on the same session
        mark_entry(_day_state, today)
        fill = None
        for _ in range(5):
            time.sleep(1)
            fill = fetch_fill_price(oid, sym)
            if fill is not None:
                break
        prem_fill = fill or prem
        sl_oid, sl_trig = (
            place_premium_sl(sym, _opt_exchange, qty, prem_fill)
            if PREMIUM_SL_PCT > 0
            else (None, None)
        )
        trade = {
            "symbol": sym,
            "side": side,
            "qty": qty,
            "entry_px": prem_fill,
            "entry_spot": entry_spot,
            "entry_day": str(today),
            "sl_oid": sl_oid,
            "sl_trig": sl_trig,
            "intraday": intraday,
        }
        _active_trade = trade
        persist_state(_active_trade, _day_state)
        _e_dir = side or ("CE" if sym.upper().endswith("CE") else "PE")
        _e_sp = float(entry_spot or 0.0)
        _sl_sp = _e_sp * (1 - SL_FRAC) if _e_dir == "CE" else _e_sp * (1 + SL_FRAC)
        _tgt_sp = _e_sp * (1 + TGT_FRAC) if _e_dir == "CE" else _e_sp * (1 - TGT_FRAC)
        write_status("IN_TRADE", active_trades=[{
            "symbol": sym,
            "direction": _e_dir,
            "entry_price": _e_sp or None,
            "stop_loss": _sl_sp or None,
            "target": _tgt_sp or None,
            "current_price": _e_sp or None,
            "type": _e_dir,
        }], indicators={"phase": "CARRY", "regime": f"OVERNIGHT {UNDERLYING}"})
        log.info(
            "Phase: CARRY %s %s qty=%s fill=%.2f spot=%.1f SL@%.2f (%s)",
            side,
            sym,
            qty,
            prem_fill,
            entry_spot,
            sl_trig or 0,
            reason,
        )
        return trade
    except Exception as e:
        log.error("entry error: %s", e)
        return None


def _exit_position(reason):
    global _active_trade
    trade = _active_trade or {}
    sym = trade.get("symbol")
    if not sym:
        _active_trade = {}
        persist_state({}, _day_state)
        return
    outcome, qty, sell = verified_exit_sell(
        UNDERLYING, sym, _opt_exchange, trade.get("qty", 0), trade.get("sl_oid"), reason
    )
    if outcome == "sold":
        _active_trade = {}
        persist_state({}, _day_state)
        release_symbol_lock(sym, STRATEGY_NAME)
        release_direction_lock(UNDERLYING, STRATEGY_NAME, trade.get("side"))
        log.info("Phase: FLAT reason=%s qty=%s fill=%s", reason, qty, sell)
        _pdh_after = "DONE" if not day_budget_left(_day_state, date.today()) else "IDLE"
        write_status(_pdh_after, indicators={"phase": "FLAT", "regime": f"OVERNIGHT {UNDERLYING}"})
    else:
        log.warning("exit incomplete (%s) -> retry", outcome)


def _stop_state(trade, spot):
    """'SL' | 'TGT' | None for the carried trade at the latest spot."""
    if spot is None:
        return None
    entry = trade.get("entry_spot")
    if not entry:
        return None
    if trade.get("side") == "CE":
        if spot <= entry * (1 - SL_FRAC):
            return "SL"
        if spot >= entry * (1 + TGT_FRAC):
            return "TGT"
    else:
        if spot >= entry * (1 + SL_FRAC):
            return "SL"
        if spot <= entry * (1 - TGT_FRAC):
            return "TGT"
    return None


def run_strategy():
    global _active_trade, _day_state, _opt_exchange, QUANTITY, LOT_SIZE
    _opt_exchange = _option_exchange(UNDERLYING)
    if not acquire_instance_lock(UNDERLYING, STRATEGY_NAME):
        log.error("instance lock held by another process; exiting")
        sys.exit(1)
    _active_trade, _day_state = load_state()
    log.info(
        "%s starting: underlying=%s mode=%s use_ema=%s tiers=%s pm=%smin "
        "sl=%.2f%% tgt=%.2f%% exit=%s expiry-skip=%s",
        STRATEGY_NAME,
        UNDERLYING,
        MODE,
        USE_EMA,
        TIERS,
        PM_WINDOW,
        SL_FRAC * 100,
        TGT_FRAC * 100,
        EXIT_TIME,
        UNDERLYING in SKIP_EXPIRY_DAY_UNDERLYINGS,
    )
    log.info("Regime: %s %s", MODE.upper(), UNDERLYING)

    if not _active_trade:
        adopted = reconcile_orphan_position(UNDERLYING)
        if adopted:
            is_claimed, peer_detail = is_position_claimed_by_peer(adopted["symbol"], STRATEGY_NAME)
            if is_claimed:
                log.info(f"Orphan {adopted['symbol']} is claimed by peer ({peer_detail}) — skipping adoption")
                adopted = None
            else:
                log.warning("adopting orphan: %s", adopted)
                _active_trade = adopted
                persist_state(_active_trade, _day_state)
    if LOT_SIZE <= 0:
        LOT_SIZE = fetch_lot_size(UNDERLYING, _opt_exchange) or 0
    # The 09:10 schedule start lands before the exchange master answers, so a
    # cold start failed here and sys.exit(1) turned that into a platform
    # restart loop -- 7 restarts on 2026-08-07 before one happened to stick
    # after the market opened. Wait for the master rather than dying; SIGTERM
    # still breaks out immediately.
    #
    # 2026-08-24: a FIXED 600s window from process start was still wrong. It
    # expired at 09:20:09 and the flattrade master finished downloading at
    # 09:20:50 -- both instances died 41 SECONDS early and lost the whole
    # session. In overnight mode the size is not needed until ENTRY_TIME
    # (15:05), so anchor the deadline to the moment the size is actually
    # REQUIRED, never to process start.
    _deadline = datetime.combine(date.today(), ENTRY_TIME if MODE == "overnight"
                                 else EXIT_TIME)
    while LOT_SIZE <= 0 and not _shutdown_requested:
        if datetime.now() >= _deadline:
            break
        log.info("lot size not published yet; retrying in 10s (deadline %s)",
                 _deadline.strftime("%H:%M"))
        time.sleep(10)
        LOT_SIZE = fetch_lot_size(UNDERLYING, _opt_exchange) or 0
    if LOT_SIZE <= 0:
        if _shutdown_requested:
            log.info("shutdown while waiting for lot size")
            sys.exit(0)
        log.error("lot size unavailable by %s (the point it is needed); "
                  "set QUANTITY to override", _deadline.strftime("%H:%M"))
        sys.exit(1)
    log.info(
        "sizing: lot=%s mode=%s max_lots=%s risk=%.2f%% (auto sizing uses the entry premium)",
        LOT_SIZE,
        LOT_MODE,
        MAX_LOTS,
        RISK_PCT_PER_TRADE,
    )

    today = date.today()
    while not _shutdown_requested:
        now = datetime.now()
        if now.date() != today:
            today = now.date()  # new session; circuit breakers reset below
        if MODE == "overnight":
            _overnight_tick(now)
        else:
            _intraday_tick(now)
        time.sleep(5)


def _overnight_tick(now):
    global _active_trade
    t = now.time()
    carry = _active_trade
    # Publish sidecar for Live Monitor
    if carry:
        _c_dir = carry.get("side") or ("CE" if str(carry.get("symbol", "")).upper().endswith("CE") else "PE")
        _e_spot = float(carry.get("entry_spot") or 0.0)
        _sl_spot = _e_spot * (1 - SL_FRAC) if _c_dir == "CE" else _e_spot * (1 + SL_FRAC)
        _tgt_spot = _e_spot * (1 + TGT_FRAC) if _c_dir == "CE" else _e_spot * (1 - TGT_FRAC)
        write_status("IN_TRADE", active_trades=[{
            "symbol": carry.get("symbol"),
            "direction": _c_dir,
            "entry_price": _e_spot or None,
            "stop_loss": _sl_spot or None,
            "target": _tgt_spot or None,
            "current_price": None,
            "type": _c_dir,
        }], indicators={"phase": "CARRY", "regime": f"OVERNIGHT {UNDERLYING}"})
    else:
        _pdh_st = "DONE" if not day_budget_left(_day_state, now.date()) else "IDLE"
        write_status(_pdh_st, indicators={"phase": _pdh_st, "regime": f"OVERNIGHT {UNDERLYING}"})
    if carry:
        spot = _latest_spot(fetch_minute_history(UNDERLYING, _index_exchange(UNDERLYING)))
        entry_day = carry.get("entry_day")
        if entry_day and now.date() > date.fromisoformat(entry_day) and t >= EXIT_TIME:
            _exit_position("next-open exit")
            return
        hit = _stop_state(carry, spot)
        if hit:
            _exit_position(f"spot {hit}")
        return
    if t < ENTRY_TIME:
        return
    if not day_budget_left(_day_state, now.date()):
        return  # the session's single entry is already spent
    if UNDERLYING in SKIP_EXPIRY_DAY_UNDERLYINGS and _expiry_present(now):
        log.info("Phase: SKIP expiry-day")
        return
    df_1m = fetch_minute_history(UNDERLYING, _index_exchange(UNDERLYING))
    pdh, pdl = fetch_prior_levels(df_1m, now.date())
    if pdh is None:
        log.warning("no prior-day levels; stand down")
        return
    pm_end = (datetime.combine(now.date(), PM_START) + timedelta(minutes=PM_WINDOW)).time()
    pmh, pml = pm_range(df_1m, now.date(), PM_START, pm_end)
    levels = {"pdh": pdh, "pdl": pdl, "pmh": pmh, "pml": pml}
    side, tier, detail = compute_signal(levels, df_1m, mode="overnight", now=now)
    log.info("Level: PDH=%.1f PDL=%.1f PMH=%s PML=%s", pdh, pdl, pmh, pml)
    log.info(
        "Phase: IDLE bias=%s tier=%s ema9=%s ema21=%s close=%.1f",
        side or "none",
        tier,
        detail.get("ema9"),
        detail.get("ema21"),
        detail.get("close"),
    )
    if side:
        _enter_position(side, "overnight carry", levels, df_1m)


def _intraday_tick(now):
    global _active_trade
    t = now.time()
    if _active_trade:
        if t >= EOD_EXIT:
            _exit_position("EOD exit")
        else:
            spot = _latest_spot(fetch_minute_history(UNDERLYING, _index_exchange(UNDERLYING)))
            hit = _stop_state(_active_trade, spot)
            if hit:
                _exit_position(f"spot {hit}")
        return
    pm_end = (datetime.combine(now.date(), PM_START) + timedelta(minutes=PM_WINDOW)).time()
    if t < pm_end or t >= ENTRY_END:
        return
    if not day_budget_left(_day_state, now.date()):
        return  # one entry per session, matching both harnesses
    if UNDERLYING in SKIP_EXPIRY_DAY_UNDERLYINGS and _expiry_present(now):
        log.info("Phase: SKIP expiry-day")
        return
    df_1m = fetch_minute_history(UNDERLYING, _index_exchange(UNDERLYING))
    pdh, pdl = fetch_prior_levels(df_1m, now.date())
    if pdh is None:
        return
    pmh, pml = pm_range(df_1m, now.date(), PM_START, pm_end)
    levels = {"pdh": pdh, "pdl": pdl, "pmh": pmh, "pml": pml}
    side, tier, detail = compute_signal(levels, df_1m, mode="intraday", now=now)
    log.info(
        "Phase: IDLE intraday bias=%s tier=%s close=%.1f", side or "none", tier, detail.get("close")
    )
    if side:
        _enter_position(side, "intraday breakout", levels, df_1m, intraday=True)


def _expiry_present(now):
    try:
        exp = get_nearest_expiry(UNDERLYING, _opt_exchange)
        return is_expiry_today(exp)
    except Exception:
        return False


if __name__ == "__main__":
    run_strategy()
