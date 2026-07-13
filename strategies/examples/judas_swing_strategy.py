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
EXIT_TIME = dtime(15, 15)  # Auto-squareoff time
RR = float(os.getenv('RR', '2.0'))  # reward:risk target multiple (both indices: 2.0)
# Circuit breaker config
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
DAILY_LOSS_LIMIT_RS = float(os.getenv('DAILY_LOSS_LIMIT_RS', '10000'))

# Symbol lock dir (shared across all strategies on this host)
LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)

def acquire_symbol_lock(symbol, strategy_name):
    """Try to claim a lock on a symbol. Returns True if acquired (or already ours)."""
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    if lock_file.exists():
        try:
            owner = lock_file.read_text().split("|", 1)[0]
            return owner == strategy_name
        except Exception:
            return False
    try:
        lock_file.write_text(f"{strategy_name}|{datetime.now().isoformat()}")
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

    # sweep above -> false bullish break -> reversal down -> buy PE
    if swept_high and c_close < or_high:
        sl_spot = sweep_extreme_high
        risk = sl_spot - c_close
        if risk > 0:
            status.update({"signal": "PE", "entry_spot": c_close,
                           "sl_spot": sl_spot, "target_spot": c_close - RR * risk})
            return status
    # sweep below -> false bearish break -> reversal up -> buy CE
    if swept_low and c_close > or_low:
        sl_spot = sweep_extreme_low
        risk = c_close - sl_spot
        if risk > 0:
            status.update({"signal": "CE", "entry_spot": c_close,
                           "sl_spot": sl_spot, "target_spot": c_close + RR * risk})
            return status
    return status

# Shutdown state shared between signal handler and run loop
_shutdown_requested = False
_active_trade = {}
_opt_exchange = None

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

            if broker_qty is None:
                # UNKNOWN (positionbook non-success, e.g. app restarting -> 502).
                # Do NOT assume flat; keep lock + state so restart adoption re-arms it.
                log.error(f"Shutdown: cannot verify {symbol} position — leaving untouched for restart adoption")
                log.info("Shutdown complete. Exiting.")
                sys.exit(0)
            if broker_qty <= 0:
                log.info(f"Shutdown: broker reports {symbol} qty={broker_qty} — already flat, no SELL")
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
                    release_symbol_lock(symbol, STRATEGY_NAME)
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
        trade_date = date.today()  # seed so the new-day reset does NOT wipe the adopted IN_TRADE state
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

                exit_triggered = False
                exit_reason = ""

                if current_time >= EXIT_TIME:
                    exit_triggered = True
                    exit_reason = "EOD Squareoff (15:15)"
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
                    pre_exit_opt_ltp = fetch_option_ltp(symbol, opt_exchange, underlying_ltp=underlying_ltp)

                    order_resp = client.placeorder(
                        strategy=STRATEGY_NAME,
                        symbol=symbol,
                        action="SELL",
                        exchange=opt_exchange,
                        price_type="MARKET",
                        product=PRODUCT,
                        quantity=qty
                    )
                    log.info(f"Exit Order Response: {order_resp}")

                    # Compute trade P&L (option BUY entry → SELL exit)
                    entry_opt_price = active_trade.get("entry_opt_price")
                    if entry_opt_price is not None and pre_exit_opt_ltp is not None:
                        trade_pnl = (pre_exit_opt_ltp - entry_opt_price) * qty
                        if trade_pnl < 0:
                            consecutive_losses += 1
                            daily_loss_rs += abs(trade_pnl)
                            log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak: {consecutive_losses} | Daily losses: ₹{daily_loss_rs:.0f}")
                        else:
                            consecutive_losses = 0
                            log.info(f"Trade P&L: ₹{trade_pnl:+.2f} | Loss streak reset")

                    release_symbol_lock(symbol, STRATEGY_NAME)

                    # Judas is one-trade-per-day — after any exit, done for the day
                    state = "DONE"
                    active_trade = {}
                    _active_trade = {}
                    persist_trade({})
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
                        "target_spot": target_spot,
                        "qty": entry_qty,
                        "entry_opt_price": entry_opt_price,
                    }
                    _active_trade = active_trade
                    persist_trade(active_trade)
                    last_entry_candle_fp = sig["candle_fp"]
                    log.info(f"Entered Trade! Spot Entry: {entry_spot:.2f} | SL: {sl_spot:.2f} | Target: {target_spot:.2f} | Opt entry: {entry_opt_price}")
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
