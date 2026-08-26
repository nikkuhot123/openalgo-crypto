#!/usr/bin/env python
"""POV Wall-Squeeze - Delta Exchange crypto options, original signal restored.

This is the original POV Wall-Squeeze gate, not a proxy. Delta does publish
open interest; it just is not in the price candle. OI is a separate series on
the same endpoint under an "OI:" symbol prefix, and broker/deltaexchange/api/
data.py now fetches and merges it, so df["oi"] carries real contract OI
(verified 2026-08-26: 86/86 hourly bars populated on BTC28AUG2678000CE).

Signal, exactly as the Indian book defines it - a pre-gate plus five scored
conditions on closed option candles:

    pre-gate  sum of positive oi_change over the last PRE_LOOKBACK bars must
              clear PRE_OI_MIN. Without recent OI build-up there is no wall to
              squeeze, so the leg is skipped before anything else is scored.
    c1  volume      > VOL_MULT x mean(volume of the prior 5 bars)
    c2  |oi_change| < OI_THRESHOLD          (the wall is holding, not unwinding)
    c3  range       > RANGE_MULT x prior bar range
    c4  lower wick  < WICK_MAX x range      (no rejection from below)
    c5  close       > open                  (closed green)

    score 5 -> STRONG, 4 -> WATCH, else WAIT. Entry needs POV_MIN_SCORE.

THRESHOLD SCALE - the one deliberate departure. The original ships absolute
thresholds (NIFTY 50000/30000, SENSEX 1600/550) and its own comments state
those do not port across books, which is why it also carries a relative mode
for MIDCPNIFTY (7% of current OI). Delta reports BTC option OI in BTC units:
observed range 3.15-32.06, i.e. four orders of magnitude below NIFTY contract
counts. Absolute numbers are therefore meaningless here and the relative mode
is the faithful transfer: both thresholds are percentages of the leg's current
OI, so they scale with the strike's own liquidity.

Exits are the original ones: stop-limit at the signal bar's low, first target
at 1.5R, decay floor, and a max-hold time stop.
"""
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from openalgo import api

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

api_key = os.getenv('OPENALGO_API_KEY')
host = os.getenv('HOST_SERVER', 'http://127.0.0.1:5001')
ws_url = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8766')

if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=api_key, host=host, ws_url=ws_url)

STRATEGY_NAME = os.getenv('STRATEGY_NAME', 'POV Wall-Squeeze (BTC)')
UNDERLYING = os.getenv('UNDERLYING', 'BTC')
UNDERLYING_QUOTE_SYMBOL = os.getenv('UNDERLYING_QUOTE_SYMBOL', 'BTCUSDFUT')
EXCHANGE = os.getenv('EXCHANGE', 'CRYPTO')
PRODUCT = os.getenv('PRODUCT', 'NRML')
STRATEGY_ID = os.getenv('STRATEGY_ID', 'pov_crypto_btc')
config_override = {}
try:
    cfg_file = Path(__file__).resolve().parents[1] / "strategy_configs.json"
    if cfg_file.exists():
        with open(cfg_file) as fh:
            strategy_configs = json.load(fh)
            if STRATEGY_ID in strategy_configs:
                config_override = strategy_configs[STRATEGY_ID]
except Exception as e:
    log.warning(f"Could not load strategy_configs.json: {e}")

QUANTITY = int(config_override.get('quantity', os.getenv('QUANTITY', '1')))
MAX_LOTS = int(config_override.get('max_lots_nifty', os.getenv('MAX_LOTS', '1')))
LOT_MODE = str(config_override.get('lot_mode', os.getenv('LOT_MODE', 'manual'))).lower()
RISK_PCT_PER_TRADE = float(config_override.get('risk_pct_per_trade', os.getenv('RISK_PCT_PER_TRADE', '1.0')))
ENTRY_QTY = max(1, QUANTITY * MAX_LOTS)
TICK_SIZE = float(os.getenv('TICK_SIZE', '0.1'))
INTERVAL = os.getenv('INTERVAL', '1h')
LOOKBACK_DAYS = int(os.getenv('LOOKBACK_DAYS', '7'))
STRIKE_GAP = float(os.getenv('STRIKE_GAP', '200.0'))

# ── original POV signal constants ─────────────────────────────────────────────
PRE_LOOKBACK = int(os.getenv('PRE_LOOKBACK', '4'))
# Relative thresholds, as fractions of the leg's current OI. See the module
# docstring for why the original's absolute values cannot be used on Delta.
PRE_OI_MIN_PCT = float(os.getenv('PRE_OI_MIN_PCT', '1.0'))    # build-up over PRE_LOOKBACK bars
OI_PCT = float(os.getenv('OI_PCT', '7.0'))                    # c2 ceiling, original MIDCP value
VOL_MULT = float(os.getenv('VOL_MULT', '3.0'))
RANGE_MULT = float(os.getenv('RANGE_MULT', '2.0'))
WICK_MAX = float(os.getenv('WICK_MAX', '0.15'))
POV_MIN_SCORE = int(os.getenv('POV_MIN_SCORE', '5'))          # 5 = STRONG only, as shipped
POV_MAX_TRADES_PER_DAY = int(os.getenv('POV_MAX_TRADES_PER_DAY', '4'))
COOLDOWN_MINUTES = int(os.getenv('POV_COOLDOWN_MINUTES', '30'))
R_TARGET = float(os.getenv('R_TARGET', '1.5'))
SL_LIMIT_BUFFER_PCT = float(os.getenv('SL_LIMIT_BUFFER_PCT', '2.0'))
MAX_HOLD_MINUTES = int(os.getenv('MAX_HOLD_MINUTES', '540'))
DECAY_EXIT_PCT = float(os.getenv('DECAY_EXIT_PCT', '0.60'))
OPT_COST_PCT = float(os.getenv('OPT_COST_PCT', '0.05'))
DAILY_LOSS_LIMIT = float(os.getenv('DAILY_LOSS_LIMIT', '500.0'))
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
POLL_SECS = int(os.getenv('POLL_SECS', '15'))

LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"pov_crypto_{UNDERLYING.upper()}.json"
LOCK_TTL_MIN = float(os.getenv('LOCK_TTL_MIN', '720'))

_state = {}          # per-symbol dedup state for _dedup_action


def _round_tick(price, tick=TICK_SIZE):
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def _strategy_slug(name):
    return re.sub(r'[^A-Za-z0-9_]+', '_', name).strip('_').upper()


def _pid_alive(pid):
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
    """TTL is authoritative: a live pid does not prove the claim is still real."""
    try:
        age_min = (datetime.now() - datetime.fromisoformat(str(ts_str))).total_seconds() / 60.0
    except (ValueError, TypeError):
        return True
    if age_min > LOCK_TTL_MIN:
        return True
    return not (pid and _pid_alive(pid))


def _claim(lock_file, payload):
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
        return True
    except FileExistsError:
        return False


def acquire_symbol_lock(symbol):
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    payload = json.dumps({"strategy": STRATEGY_NAME, "pid": os.getpid(),
                          "time": datetime.now().isoformat()})
    if _claim(lock_file, payload):
        return True
    try:
        data = json.loads(lock_file.read_text())
    except Exception:
        log.warning(f"Unreadable lock on {symbol} - standing aside")
        return False
    if data.get("strategy") == STRATEGY_NAME and data.get("pid") == os.getpid():
        return True
    if not _lock_is_stale(data.get("time"), data.get("pid")):
        log.info(f"LOCKED: {symbol} held by {data.get('strategy')} - standing aside")
        return False
    log.warning(f"Stale lock on {symbol} - reclaiming")
    lock_file.unlink(missing_ok=True)
    return _claim(lock_file, payload)


def release_symbol_lock(symbol):
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    try:
        if lock_file.exists():
            data = json.loads(lock_file.read_text())
            if data.get("strategy") == STRATEGY_NAME:
                lock_file.unlink(missing_ok=True)
                log.info(f"Released lock for {symbol}")
    except Exception as e:
        log.warning(f"Error releasing lock {lock_file}: {e}")


def direction_lock_file(direction):
    return LOCKS_DIR / f"dir_{_strategy_slug(STRATEGY_NAME)}_{UNDERLYING.upper()}_{direction}.lock"


def acquire_direction_lock(direction, symbol):
    """One live leg per side, as the original does - never two CEs at once."""
    lf = direction_lock_file(direction)
    payload = json.dumps({"strategy": STRATEGY_NAME, "pid": os.getpid(),
                          "symbol": symbol, "time": datetime.now().isoformat()})
    if _claim(lf, payload):
        return True
    try:
        data = json.loads(lf.read_text())
    except Exception:
        return False
    if data.get("symbol") == symbol and data.get("pid") == os.getpid():
        return True
    if _lock_is_stale(data.get("time"), data.get("pid")):
        lf.unlink(missing_ok=True)
        return _claim(lf, payload)
    return False


def release_direction_lock(direction):
    lf = direction_lock_file(direction)
    try:
        if lf.exists():
            data = json.loads(lf.read_text())
            if data.get("strategy") == STRATEGY_NAME:
                lf.unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"Error releasing direction lock: {e}")


def sync_direction_locks(positions):
    for direction in ("CE", "PE"):
        held = next((s for s in positions if s.endswith(direction)), None)
        if held:
            acquire_direction_lock(direction, held)
        else:
            release_direction_lock(direction)


def save_state(positions):
    try:
        STATE_FILE.write_text(json.dumps(positions, indent=2, default=str))
    except Exception as e:
        log.warning(f"Failed to save state: {e}")


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _status_fields(data):
    status = (data.get("order_status") or data.get("orderstatus") or "").upper()
    fill = float(data.get("average_price") or data.get("averageprice") or data.get("price") or 0.0)
    return status, fill


def fetch_fill_price(order_id, fallback=0.0):
    if not order_id:
        return fallback
    try:
        resp = client.orderstatus(order_id=str(order_id), strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success" and "data" in resp:
            _, fill = _status_fields(resp["data"])
            if fill > 0:
                return fill
    except Exception as e:
        log.warning(f"Error fetching fill price for {order_id}: {e}")
    return fallback


def confirm_entry_fill(order_id, symbol, timeout_sec=10):
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            resp = client.orderstatus(order_id=str(order_id), strategy=STRATEGY_NAME)
            if resp and resp.get("status") == "success" and "data" in resp:
                st, fill = _status_fields(resp["data"])
                if st == "COMPLETE":
                    return True, fill
                if st in ("REJECTED", "CANCELLED"):
                    log.error(f"Entry order {order_id} was {st} on {symbol}")
                    return False, 0.0
        except Exception as e:
            log.warning(f"Polling entry order {order_id}: {e}")
        time.sleep(1)
    return False, 0.0


def order_state(order_id):
    if not order_id:
        return None
    try:
        resp = client.orderstatus(order_id=str(order_id), strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success" and "data" in resp:
            return _status_fields(resp["data"])[0] or None
    except Exception as e:
        log.warning(f"Error checking order state for {order_id}: {e}")
    return None


def safe_cancel_order(order_id, symbol=""):
    if not order_id:
        return True
    try:
        resp = client.cancelorder(order_id=str(order_id), strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success":
            log.info(f"Cancelled order {order_id} ({symbol})")
            return True
        log.warning(f"Cancel {order_id} returned non-success: {resp}")
    except Exception as e:
        log.warning(f"Exception cancelling {order_id}: {e}")
    return False


def live_position_qty(symbol):
    try:
        resp = client.positionbook()
        if not resp or resp.get("status") != "success" or "data" not in resp:
            return None
        for p in resp["data"]:
            if p.get("symbol") == symbol:
                return int(p.get("quantity", 0))
        return 0
    except Exception as e:
        log.warning(f"Error fetching position for {symbol}: {e}")
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


def fetch_contract_value_from_db(symbol, exchange="CRYPTO"):
    """Query openalgo.db to get the contract_value (multiplier) for a symbol."""
    db_path = Path(__file__).resolve().parents[2] / "db" / "openalgo.db"
    if not db_path.exists():
        return None
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT contract_value FROM symtoken WHERE symbol = ? AND exchange = ?", (symbol, exchange))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        log.warning(f"Error querying contract_value for {symbol}: {e}")
    return None


def compute_auto_lots(capital, risk_pct, max_loss_per_unit, contract_value, hard_cap_lots):
    """Compute contract count from risk budget. max_loss_per_unit is in USD premium points."""
    if max_loss_per_unit <= 0 or contract_value <= 0:
        return 1
    # Sandbox capital is in INR, but options and perps are priced in USD.
    # Convert capital from INR to USD before applying the risk budget percentage.
    USDINR = float(os.getenv('USDINR_RATE', '84.0'))
    capital_usd = capital / USDINR
    risk_budget = capital_usd * (risk_pct / 100.0)
    max_loss_per_contract = max_loss_per_unit * contract_value
    if max_loss_per_contract <= 0:
        return 1
    auto_lots = int(risk_budget / max_loss_per_contract)
    log.info(f"AUTO-LOT: capital INR {capital:.2f} (USD ${capital_usd:.2f}) | risk {risk_pct}% | risk_budget ${risk_budget:.2f} | cash_loss/contract ${max_loss_per_contract:.4f} → {auto_lots} contracts")
    return max(1, min(auto_lots, hard_cap_lots))


def statutory_cost(turnover):
    return abs(turnover) * (OPT_COST_PCT / 100.0)


def verified_exit_sell(symbol, qty, reason="EXIT"):
    live_q = live_position_qty(symbol)
    if live_q is not None and live_q <= 0:
        log.info(f"verified_exit_sell: {symbol} already flat")
        return True, 0.0
    sell_qty = qty if (live_q is None or live_q <= 0) else min(qty, live_q)
    try:
        resp = client.placeorder(symbol=symbol, exchange=EXCHANGE, action="SELL",
                                 pricetype="MARKET", product=PRODUCT, quantity=sell_qty,
                                 strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success":
            oid = resp.get("orderid")
            log.info(f"MARKET SELL {symbol} qty={sell_qty} ({reason}) orderid={oid}")
            return True, fetch_fill_price(oid, 0.0)
        log.error(f"Failed to sell {symbol}: {resp}")
    except Exception as e:
        log.error(f"Exception selling {symbol}: {e}")
    return False, 0.0


def flatten_unexpected_short(symbol, qty):
    size = abs(int(qty))
    if size <= 0:
        return False
    log.error(f"UNEXPECTED SHORT on {symbol} (qty={qty}) - buying back {size}")
    try:
        resp = client.placeorder(symbol=symbol, exchange=EXCHANGE, action="BUY",
                                 pricetype="MARKET", product=PRODUCT, quantity=size,
                                 strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success":
            log.info(f"Flattened short on {symbol} (orderid={resp.get('orderid')})")
            return True
    except Exception as e:
        log.error(f"Exception flattening short on {symbol}: {e}")
    return False


def sync_positions_with_book(positions):
    """Book stop fills, drop flat legs, flatten only this strategy's own shorts."""
    realized, losses = 0.0, 0
    try:
        resp = client.positionbook()
        if not resp or resp.get("status") != "success" or "data" not in resp:
            return positions, realized, losses

        live_map = {}
        for p in resp["data"]:
            sym = p.get("symbol", "")
            if sym and UNDERLYING.upper() in sym.upper():
                live_map[sym] = int(p.get("quantity", 0))

        # Only ever act on legs this strategy opened. "BTC" also matches
        # BTCUSDFUT, the perp another strategy trades; buying that back would
        # close someone else's deliberate short (it did, on 2026-08-26).
        for sym, live_q in live_map.items():
            if live_q >= 0:
                continue
            if sym in positions or sym.endswith(("CE", "PE")):
                flatten_unexpected_short(sym, live_q)
            else:
                log.info(f"Short {live_q} on {sym} is not a POV option leg - leaving it alone")

        to_remove = []
        for sym, pdata in list(positions.items()):
            if live_map.get(sym, 0) > 0:
                continue
            sl_oid = pdata.get("sl_orderid")
            entry_p = float(pdata.get("entry_price", 0.0) or 0.0)
            qty = int(pdata.get("qty", ENTRY_QTY) or ENTRY_QTY)
            if order_state(sl_oid) == "COMPLETE":
                exit_p = fetch_fill_price(sl_oid, 0.0)
                if exit_p > 0 and entry_p > 0:
                    pnl = (exit_p - entry_p) * qty - statutory_cost((entry_p + exit_p) * qty)
                    realized += pnl
                    if pnl < 0:
                        losses += 1
                    log.info(f"Stop filled on {sym} @ {exit_p} (entry {entry_p}) -> ${pnl:.4f}")
                else:
                    log.warning(f"Stop {sl_oid} on {sym} complete but no fill price - pnl not booked")
            else:
                safe_cancel_order(sl_oid, sym)
            to_remove.append(sym)

        for sym in to_remove:
            positions.pop(sym, None)
            release_symbol_lock(sym)
            log.info(f"Cleared flat position {sym}")
        if to_remove:
            save_state(positions)
            sync_direction_locks(positions)
        return positions, realized, losses
    except Exception as e:
        log.warning(f"Error in sync_positions_with_book: {e}")
        return positions, realized, losses


def _dedup_action(symbol, action, score, entry, sl, t1):
    """Original dedup: a repeat of the same action only re-fires after cooldown."""
    now = datetime.now()
    prev = _state.get(symbol, {})
    changed = action != prev.get("action")
    cooled = False
    if not changed and action in ("STRONG", "WATCH"):
        pt = prev.get("time")
        if pt:
            cooled = (now - pt).total_seconds() / 60.0 >= COOLDOWN_MINUTES
    is_new = changed or cooled
    if is_new:
        _state[symbol] = {"action": action, "time": now}
    return {"action": action, "score": score, "is_new": is_new,
            "entry": entry, "sl": sl, "t1": t1}


def evaluate_pov(symbol, df):
    """The original wall-squeeze gate, scored on closed candles with real OI."""
    if not isinstance(df, pd.DataFrame) or len(df) < 8:
        return {"action": "WAIT", "score": 0, "is_new": False, "reason": "insufficient bars"}

    d = df.copy()
    if "oi" not in d.columns:
        return {"action": "WAIT", "score": 0, "is_new": False, "reason": "no oi column"}
    d["oi_change"] = d["oi"].diff().fillna(0)
    bars = d.tail(10).to_dict(orient="records")
    cur, prev = bars[-1], bars[-2]

    # A zero/absent OI level makes both OI tests meaningless: the pre-gate
    # threshold collapses to 0 (always passes) while c2's ceiling collapses to 0
    # (never passes). Refuse to score rather than emit a corrupted result.
    cur_oi = float(cur.get("oi", 0) or 0)
    if cur_oi <= 0:
        return {"action": "WAIT", "score": 0, "is_new": False, "reason": "oi level unavailable"}

    # Pre-gate: recent positive OI build-up into the trigger bar.
    build = sum(max(0.0, float(b.get("oi_change", 0) or 0)) for b in bars[-PRE_LOOKBACK:])
    pre_min = cur_oi * (PRE_OI_MIN_PCT / 100.0)
    if build < pre_min:
        return _dedup_action(symbol, "WAIT", 0, None, None, None)

    vols = [float(b.get("volume", 0) or 0) for b in bars[-6:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    c1 = float(cur.get("volume", 0) or 0) > avg_vol * VOL_MULT

    c2 = abs(float(cur.get("oi_change", 0) or 0)) < cur_oi * (OI_PCT / 100.0)

    cur_rng = float(cur.get("high", 0)) - float(cur.get("low", 0))
    prev_rng = float(prev.get("high", 0)) - float(prev.get("low", 0))
    c3 = (cur_rng > prev_rng * RANGE_MULT) if prev_rng > 0 else False

    op, cl, lo = float(cur.get("open", 0)), float(cur.get("close", 0)), float(cur.get("low", 0))
    c4 = (((min(op, cl) - lo) / cur_rng) < WICK_MAX) if cur_rng > 0 else False

    c5 = cl > op

    score = int(c1) + int(c2) + int(c3) + int(c4) + int(c5)
    action = "STRONG" if score == 5 else ("WATCH" if score == 4 else "WAIT")

    entry = sl = t1 = None
    if score >= 4:
        entry = _round_tick(cl)
        sl = _round_tick(lo)
        risk = max(entry - sl, TICK_SIZE * 2)
        t1 = _round_tick(entry + risk * R_TARGET)
    return _dedup_action(symbol, action, score, entry, sl, t1)


def get_nearest_expiry():
    """Nearest expiry that is actually quoting - Delta settles at 12:00 UTC and
    the settled contract lingers in the master list with zero quotes."""
    try:
        resp = client.expiry(symbol=UNDERLYING, exchange=EXCHANGE, instrumenttype="options")
        if resp and resp.get("status") == "success" and "data" in resp:
            expiries = resp["data"]
            for raw in expiries:
                clean = raw.replace("-", "")
                so = client.optionsymbol(underlying=UNDERLYING, exchange=EXCHANGE,
                                         expiry_date=clean, offset="ATM", option_type="CE")
                if so and so.get("status") == "success":
                    sym = so.get("symbol") or (so.get("data", {}) or {}).get("symbol")
                    if sym:
                        q = client.quotes(symbol=sym, exchange=EXCHANGE)
                        if q and q.get("status") == "success":
                            dd = q.get("data", {})
                            if float(dd.get("ltp", 0) or 0) > 0 or float(dd.get("bid", 0) or 0) > 0:
                                return clean
            if expiries:
                return expiries[0].replace("-", "")
    except Exception as e:
        log.warning(f"Error fetching expiry: {e}")
    return None


def get_option_symbol(expiry, offset, option_type):
    try:
        resp = client.optionsymbol(underlying=UNDERLYING, exchange=EXCHANGE,
                                   expiry_date=expiry, offset=offset, option_type=option_type)
        if resp and resp.get("status") == "success":
            return resp.get("symbol") or (resp.get("data", {}) or {}).get("symbol")
    except Exception as e:
        log.warning(f"Error resolving option symbol: {e}")
    return None


def fetch_option_ltp(symbol):
    try:
        resp = client.quotes(symbol=symbol, exchange=EXCHANGE)
        if resp and resp.get("status") == "success" and "data" in resp:
            return float(resp["data"].get("ltp", 0.0) or 0.0)
    except Exception as e:
        log.warning(f"Error fetching quote for {symbol}: {e}")
    return 0.0


def main():
    log.info(f"Starting {STRATEGY_NAME} | {UNDERLYING} ({EXCHANGE}) {INTERVAL} | product={PRODUCT}")
    log.info(f"Signal: pre-gate {PRE_OI_MIN_PCT}% OI build over {PRE_LOOKBACK} bars | "
             f"vol>{VOL_MULT}x | |dOI|<{OI_PCT}% | range>{RANGE_MULT}x | wick<{WICK_MAX} | "
             f"green | min score {POV_MIN_SCORE} | target {R_TARGET}R | hold<={MAX_HOLD_MINUTES}m")

    positions = load_state()
    sync_direction_locks(positions)
    daily_loss, losses_streak, trades_today = 0.0, 0, 0
    current_day = date.today()

    def on_signal(signum, frame):
        log.info("Termination signal received - saving state and exiting")
        save_state(positions)
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while True:
        try:
            if date.today() != current_day:
                current_day = date.today()
                daily_loss, losses_streak, trades_today = 0.0, 0, 0
                log.info(f"New day {current_day} - counters reset")

            positions, realized, losses = sync_positions_with_book(positions)
            if realized or losses:
                if realized < 0:
                    daily_loss += abs(realized)
                losses_streak = losses_streak + losses if losses else 0
                log.info(f"Booked from stops: ${realized:.4f} | daily_loss=${daily_loss:.4f} "
                         f"| streak={losses_streak}")

            if daily_loss >= DAILY_LOSS_LIMIT:
                log.warning(f"Daily loss ${daily_loss:.2f} >= ${DAILY_LOSS_LIMIT:.2f} - standing down")
                time.sleep(60)
                continue
            if losses_streak >= LOSS_STREAK_LIMIT:
                log.warning(f"Loss streak {losses_streak} - standing down")
                time.sleep(60)
                continue

            expiry = get_nearest_expiry()
            if not expiry:
                log.warning("No quoting expiry found - retrying")
                time.sleep(POLL_SECS)
                continue

            anchor = client.quotes(symbol=UNDERLYING_QUOTE_SYMBOL, exchange=EXCHANGE)
            if not anchor or anchor.get("status") != "success":
                time.sleep(POLL_SECS)
                continue
            spot = float(anchor["data"]["ltp"])
            log.info(f"Anchor {UNDERLYING_QUOTE_SYMBOL} {spot:.1f} | ATM ~{_round_tick(spot, STRIKE_GAP)} "
                     f"| expiry {expiry} | open legs {len(positions)}")

            end_d = date.today().strftime("%Y-%m-%d")
            start_d = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

            for option_type, offset in [("CE", "OTM2"), ("CE", "OTM1"), ("CE", "ATM"),
                                        ("PE", "ATM"), ("PE", "OTM1"), ("PE", "OTM2")]:
                symbol = get_option_symbol(expiry, offset, option_type)
                if not symbol:
                    continue
                ltp = fetch_option_ltp(symbol)
                if ltp <= 0:
                    continue

                df = client.history(symbol=symbol, exchange=EXCHANGE, interval=INTERVAL,
                                    start_date=start_d, end_date=end_d)
                if not isinstance(df, pd.DataFrame) or df.empty:
                    continue
                df = df.sort_index().reset_index(drop=True)

                res = evaluate_pov(symbol, df)
                oi_now = float(df["oi"].iloc[-1]) if "oi" in df.columns and len(df) else 0.0
                log.info(f"Scan {symbol} ({option_type} {offset}) | LTP {ltp} | OI {oi_now:.3f} | "
                         f"{res['action']} {res['score']}/5")

                pos = positions.get(symbol)
                if pos:
                    entry_p = float(pos.get("entry_price", 0.0) or 0.0)
                    target_p = float(pos.get("target_price", 0.0) or 0.0)
                    sl_oid = pos.get("sl_orderid")
                    qty = int(pos.get("qty", ENTRY_QTY) or ENTRY_QTY)

                    reason = None
                    if target_p and ltp >= target_p:
                        reason = "TARGET"
                    elif entry_p > 0 and ltp < entry_p * DECAY_EXIT_PCT:
                        reason = "DECAY"
                    else:
                        try:
                            held = (datetime.now() - datetime.fromisoformat(str(pos.get("entry_time")))).total_seconds() / 60.0
                        except (ValueError, TypeError):
                            held = None
                        if held is not None and held >= MAX_HOLD_MINUTES:
                            reason = "MAX_HOLD"

                    if reason:
                        log.info(f"{reason} on {symbol} at {ltp} - closing")
                        safe_cancel_order(sl_oid, symbol)
                        ok, fill = verified_exit_sell(symbol, qty, reason=reason)
                        if ok:
                            exit_p = fill or ltp
                            pnl = (exit_p - entry_p) * qty - statutory_cost((entry_p + exit_p) * qty)
                            log.info(f"Booked {reason} on {symbol}: ${pnl:.4f}")
                            if pnl < 0:
                                daily_loss += abs(pnl)
                                losses_streak += 1
                            else:
                                losses_streak = 0
                            positions.pop(symbol, None)
                            release_symbol_lock(symbol)
                            save_state(positions)
                            sync_direction_locks(positions)
                    continue

                if not res.get("is_new") or res["score"] < POV_MIN_SCORE:
                    continue
                if trades_today >= POV_MAX_TRADES_PER_DAY:
                    log.info(f"Daily trade cap {trades_today}/{POV_MAX_TRADES_PER_DAY} - no entry")
                    continue
                if not acquire_direction_lock(option_type, symbol):
                    log.info(f"Direction {option_type} already held - skipping {symbol}")
                    continue
                if not acquire_symbol_lock(symbol):
                    release_direction_lock(option_type)
                    continue

                # Compute entry quantity based on LOT_MODE
                entry_qty = ENTRY_QTY
                if LOT_MODE == "auto" and res["entry"] is not None and res["sl"] is not None:
                    capital = fetch_available_capital()
                    if capital is not None and capital > 0:
                        cv = fetch_contract_value_from_db(symbol)
                        if cv is None:
                            und = UNDERLYING.upper()
                            cv = 0.001 if und == "BTC" else (0.01 if und == "ETH" else (0.1 if und == "SOL" else 1.0))
                        
                        max_loss_per_unit = max(res["entry"] - res["sl"], TICK_SIZE * 2)
                        lots = compute_auto_lots(capital, RISK_PCT_PER_TRADE, max_loss_per_unit, cv, MAX_LOTS)
                        entry_qty = lots
                        log.info(f"AUTO-LOT: capital ${capital:.2f} | risk {RISK_PCT_PER_TRADE}% | contract_value {cv} | loss/unit ${max_loss_per_unit:.2f} → {lots} contracts")
                    else:
                        log.warning("AUTO-LOT: capital unavailable, falling back to manual quantity")

                log.info(f"POV {res['action']} {res['score']}/5 on {symbol} - BUY {entry_qty} "
                         f"(entry {res['entry']} sl {res['sl']} t1 {res['t1']})")
                eo = client.placeorder(symbol=symbol, exchange=EXCHANGE, action="BUY",
                                       pricetype="MARKET", product=PRODUCT, quantity=entry_qty,
                                       strategy=STRATEGY_NAME)
                if not eo or eo.get("status") != "success":
                    log.error(f"Entry rejected for {symbol}: {eo}")
                    release_symbol_lock(symbol)
                    release_direction_lock(option_type)
                    continue

                filled, fill_p = confirm_entry_fill(eo.get("orderid"), symbol)
                if not filled or fill_p <= 0:
                    fill_p = res["entry"] or ltp

                sl_trg = _round_tick(res["sl"])
                sl_lmt = _round_tick(sl_trg * (1 - SL_LIMIT_BUFFER_PCT / 100.0))
                so = client.placeorder(symbol=symbol, exchange=EXCHANGE, action="SELL",
                                       pricetype="SL", product=PRODUCT, quantity=entry_qty,
                                       price=sl_lmt, trigger_price=sl_trg, strategy=STRATEGY_NAME)
                sl_oid = so.get("orderid") if (so and so.get("status") == "success") else None
                if not sl_oid:
                    log.error(f"STOP NOT ARMED on {symbol} ({so}) - in-process exits only")
                else:
                    log.info(f"Stop armed on {symbol}: trigger {sl_trg} limit {sl_lmt} oid {sl_oid}")

                positions[symbol] = {
                    "entry_price": fill_p, "sl_price": sl_trg, "sl_limit_price": sl_lmt,
                    "target_price": res["t1"], "qty": entry_qty, "sl_orderid": sl_oid,
                    "entry_time": datetime.now().isoformat(), "direction": option_type,
                }
                trades_today += 1
                save_state(positions)
                sync_direction_locks(positions)

            time.sleep(POLL_SECS)

        except Exception as e:
            log.error(f"Unhandled exception in scan loop: {e}", exc_info=True)
            time.sleep(POLL_SECS)


if __name__ == '__main__':
    main()
