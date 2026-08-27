#!/usr/bin/env python
"""Judas Swing (opening-range sweep reversal) - Delta Exchange perpetuals.

Trades the PERPETUAL, not options. The Volrix study that produced these
parameters traded the perp long and short with the stop at the sweep extreme
and the target at rr x raw_risk; buying an ATM option instead changes the
payoff (delta ~0.5 plus theta) and discards the price-based invalidation that
is the whole point of the setup. wiki/strategies/judas_swing.md documents what
that costs on the Indian book: the spot signal was profitable while the option
position bled premium to death.

Validated configuration (60m bars, 0.03% slippage, $1000 per leg):
    opening range  00:00-06:00 UTC        min sweep      0.30% beyond the range
    entry window   06:00-14:00 UTC        volume         reversal bar >= 1.2x 5-bar mean
    stop           sweep extreme          target         2.0 x raw_risk
    break-even     ratchet at 1.0R        time stop      540 min
    one trade per UTC day

Per-symbol results were sign-stable across both test windows (6/6): BTC, ETH
and SOL profitable in-sample and out-of-sample; XRP, DOGE and AVAX negative in
both. The whole-universe book is negative out-of-sample (PF 0.90), so the
symbol allowlist is part of the strategy, not a detail.

Exits are enforced twice: a resting stop order at the venue (survives a crash)
and in-process monitoring for target, break-even and the time stop.
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
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

STRATEGY_NAME = os.getenv('STRATEGY_NAME', 'Judas Swing (BTC perp 1h)')
# The instrument that is both analysed and traded. No option leg, no anchor
# indirection: the signal and the position are the same series.
SYMBOL = os.getenv('SYMBOL', 'BTCUSDFUT')
EXCHANGE = os.getenv('EXCHANGE', 'CRYPTO')
PRODUCT = os.getenv('PRODUCT', 'NRML')
INTERVAL = os.getenv('INTERVAL', '1h')
LOOKBACK_DAYS = int(os.getenv('LOOKBACK_DAYS', '7'))

default_id = "judas_crypto_btc" if "BTC" in SYMBOL else ("judas_crypto_eth" if "ETH" in SYMBOL else "judas_crypto_sol")
STRATEGY_ID = os.getenv('STRATEGY_ID', default_id)
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

TRADE_VALUE = float(config_override.get('quantity', os.getenv('TRADE_VALUE', '1000')))


def _contract_value_from_master(symbol):
    """contract_value as published by Delta.

    broker/deltaexchange/database/master_contract_db.py writes Delta's own
    /v2/products `contract_value` into symtoken, so the master contract IS the
    venue's figure -- no second hardcoded table to drift out of sync.
    """
    db_path = Path(__file__).resolve().parents[2] / "db" / "openalgo.db"
    if not db_path.exists():
        return None
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT contract_value FROM symtoken WHERE symbol = ? AND exchange = ?",
                (symbol, EXCHANGE),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] and float(row[0]) > 0:
            return float(row[0])
    except Exception as e:
        log.warning(f"contract_value lookup failed for {symbol}: {e}")
    return None


CONTRACT_VALUE = _contract_value_from_master(SYMBOL) or float(
    config_override.get('CONTRACT_VALUE', os.getenv('CONTRACT_VALUE', '0.001'))
)
MAX_CONTRACTS = int(config_override.get('max_lots_nifty', os.getenv('MAX_CONTRACTS', '100')))
LOT_MODE = str(config_override.get('lot_mode', os.getenv('LOT_MODE', 'manual'))).lower()
RISK_PCT_PER_TRADE = float(config_override.get('risk_pct_per_trade', os.getenv('RISK_PCT_PER_TRADE', '1.0')))
TICK_SIZE = float(os.getenv('TICK_SIZE', '0.5'))

# Session geometry, UTC. The engine and Delta both run on UTC; there is no IST
# session boundary on a 24/7 venue.
OR_END_H = int(os.getenv('OR_END_H', '6'))
ENTRY_END_H = int(os.getenv('ENTRY_END_H', '14'))

MIN_SWEEP_PCT = float(os.getenv('MIN_SWEEP_PCT', '0.30'))
MIN_SL_PCT = float(os.getenv('MIN_SL_PCT', '0.10'))
VOL_MULT = float(os.getenv('VOL_MULT', '1.2'))
RR = float(os.getenv('RR', '2.0'))
BE_ARM_R = float(os.getenv('BE_ARM_R', '1.0'))
MAX_HOLD_MINUTES = int(os.getenv('MAX_HOLD_MINUTES', '540'))
MAX_TRADES_PER_DAY = int(os.getenv('MAX_TRADES_PER_DAY', '1'))
COST_PCT = float(os.getenv('COST_PCT', '0.05'))
DAILY_LOSS_LIMIT = float(os.getenv('DAILY_LOSS_LIMIT', '500.0'))
LOSS_STREAK_LIMIT = int(os.getenv('LOSS_STREAK_LIMIT', '3'))
POLL_SECS = int(os.getenv('POLL_SECS', '15'))

LOCKS_DIR = Path("log") / "strategies" / "locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = Path("log") / "strategies" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"judas_perp_{SYMBOL}.json"
DONE_FILE = STATE_DIR / f"judas_perp_done_{SYMBOL}.json"
# One session's worth of holding. A leaked lock must expire or it blocks every
# future entry on this contract for good.
LOCK_TTL_MIN = float(os.getenv('LOCK_TTL_MIN', '720'))


def _round_tick(price, tick=TICK_SIZE):
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def _pid_alive(pid):
    """True if the process exists. Unknown -> assume alive, never steal."""
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
    """Stale once past its TTL, or as soon as the owner process is gone.

    The TTL is checked FIRST and is authoritative. Deferring to "the pid is
    alive" would let a wedged owner hold the contract forever, and pids get
    reused, so a live pid is not proof that this claim is still real. An
    unparseable timestamp counts as stale.
    """
    try:
        age_min = (datetime.now() - datetime.fromisoformat(str(ts_str))).total_seconds() / 60.0
    except (ValueError, TypeError):
        return True
    if age_min > LOCK_TTL_MIN:
        return True
    return not (pid and _pid_alive(pid))


def acquire_symbol_lock(symbol):
    lock_file = LOCKS_DIR / f"{symbol}.lock"
    payload = json.dumps({"strategy": STRATEGY_NAME, "pid": os.getpid(),
                          "time": datetime.now().isoformat()})
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
        return True
    except FileExistsError:
        pass
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
    log.warning(f"Stale lock on {symbol} (owner {data.get('strategy')}) - reclaiming")
    lock_file.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
        return True
    except FileExistsError:
        return False


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


def save_state(pos):
    try:
        STATE_FILE.write_text(json.dumps(pos, indent=2, default=str))
    except Exception as e:
        log.warning(f"Failed to save state: {e}")


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def persist_done(utc_day):
    """One trade per UTC day must survive a restart, unlike an in-memory counter."""
    try:
        DONE_FILE.write_text(json.dumps({"done_on": str(utc_day)}))
    except Exception as e:
        log.warning(f"Failed to persist done marker: {e}")


def load_done_day():
    if not DONE_FILE.exists():
        return None
    try:
        return json.loads(DONE_FILE.read_text()).get("done_on")
    except Exception:
        return None


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


def confirm_fill(order_id, timeout_sec=10):
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            resp = client.orderstatus(order_id=str(order_id), strategy=STRATEGY_NAME)
            if resp and resp.get("status") == "success" and "data" in resp:
                st, fill = _status_fields(resp["data"])
                if st == "COMPLETE":
                    return True, fill
                if st in ("REJECTED", "CANCELLED"):
                    log.error(f"Order {order_id} was {st}")
                    return False, 0.0
        except Exception as e:
            log.warning(f"Polling order {order_id}: {e}")
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


def safe_cancel(order_id, why=""):
    if not order_id:
        return True
    try:
        resp = client.cancelorder(order_id=str(order_id), strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success":
            log.info(f"Cancelled order {order_id} ({why})")
            return True
        log.warning(f"Cancel {order_id} returned non-success: {resp}")
    except Exception as e:
        log.warning(f"Exception cancelling {order_id}: {e}")
    return False


def live_position_qty(symbol):
    """Signed quantity held at the broker. None when it cannot be determined."""
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


def transaction_cost(turnover):
    return abs(turnover) * (COST_PCT / 100.0)


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


def usd_inr_rate():
    """Delta's own USD->INR reference rate (GET /v2/settings)."""
    try:
        from broker.deltaexchange.api.reference import get_usd_inr_rate
        return get_usd_inr_rate()
    except Exception as e:
        override = os.getenv('DELTA_USD_INR_RATE') or os.getenv('USDINR_RATE')
        if override:
            try:
                return float(override)
            except (TypeError, ValueError):
                pass
        log.warning(f"USD-INR unavailable from Delta ({e}); using 85.0")
        return 85.0


def contracts_for(price, risk_per_contract=None):
    if price <= 0 or CONTRACT_VALUE <= 0:
        return 1

    if LOT_MODE == "auto" and risk_per_contract is not None and risk_per_contract > 0:
        capital = fetch_available_capital()
        if capital is not None and capital > 0:
            fx = usd_inr_rate()
            capital_usd = capital / fx
            risk_budget = capital_usd * (RISK_PCT_PER_TRADE / 100.0)
            auto_qty = int(risk_budget / risk_per_contract)
            log.info(
                f"AUTO-LOT: capital INR {capital:,.2f} @ {fx} = ${capital_usd:,.2f} | "
                f"risk {RISK_PCT_PER_TRADE}% = ${risk_budget:,.2f} | "
                f"risk/contract ${risk_per_contract:.4f} -> {auto_qty} (cap {MAX_CONTRACTS})"
            )
            return max(1, min(auto_qty, MAX_CONTRACTS))
        log.warning(f"AUTO-LOT: capital unavailable, falling back to notional {TRADE_VALUE}")

    n = int(TRADE_VALUE / (price * CONTRACT_VALUE))
    return max(1, min(n, MAX_CONTRACTS))


def close_position(side_held, qty, reason):
    """Flatten by trading the opposite side of what is held."""
    action = "SELL" if side_held == "long" else "BUY"
    live_q = live_position_qty(SYMBOL)
    if live_q is not None and live_q == 0:
        log.info(f"close_position: {SYMBOL} already flat")
        return True, 0.0
    size = abs(live_q) if live_q else qty
    try:
        resp = client.placeorder(symbol=SYMBOL, exchange=EXCHANGE, action=action,
                                 pricetype="MARKET", product=PRODUCT, quantity=size,
                                 strategy=STRATEGY_NAME)
        if resp and resp.get("status") == "success":
            oid = resp.get("orderid")
            log.info(f"{action} {size} {SYMBOL} to close ({reason}), orderid={oid}")
            return True, fetch_fill_price(oid, 0.0)
        log.error(f"Failed to close {SYMBOL}: {resp}")
    except Exception as e:
        log.error(f"Exception closing {SYMBOL}: {e}")
    return False, 0.0


def compute_signal(df, utc_today):
    """Opening-range sweep reversal on completed bars. Always returns a status
    dict when the range is known, so the caller can log a heartbeat."""
    if not isinstance(df, pd.DataFrame) or len(df) < 8:
        return None
    d = df.sort_index()
    if len(d) > 1:
        d = d.iloc[:-1]          # drop the forming bar
    ts = pd.to_datetime(d.index)
    ts_utc = ts.tz_convert("UTC") if getattr(ts, "tz", None) is not None else ts
    mask = [t.date() == utc_today for t in ts_utc]
    today = d[mask]
    if len(today) < 2:
        return None

    times = [t.time() for t in ts_utc[mask]]
    highs = today["high"].tolist()
    lows = today["low"].tolist()
    closes = today["close"].tolist()
    vols = today["volume"].tolist()

    or_high = or_low = None
    or_idx = -1
    for i, t in enumerate(times):
        if t.hour < OR_END_H:
            or_high = highs[i] if or_high is None else max(or_high, highs[i])
            or_low = lows[i] if or_low is None else min(or_low, lows[i])
            or_idx = i
    if or_high is None or or_low is None:
        return None

    # Thresholds measured off their own side of the range.
    hi_trigger = or_high * (1.0 + MIN_SWEEP_PCT / 100.0)
    lo_trigger = or_low * (1.0 - MIN_SWEEP_PCT / 100.0)

    swept_high = swept_low = False
    ext_high = ext_low = None
    for i in range(or_idx + 1, len(today)):
        if times[i].hour > ENTRY_END_H:
            continue
        if highs[i] > hi_trigger:
            swept_high = True
            ext_high = highs[i] if ext_high is None else max(ext_high, highs[i])
        if lows[i] < lo_trigger:
            swept_low = True
            ext_low = lows[i] if ext_low is None else min(ext_low, lows[i])

    last = len(today) - 1
    close = float(closes[last])
    status = {"or_high": or_high, "or_low": or_low, "swept_high": swept_high,
              "swept_low": swept_low, "close": close, "signal": None,
              "bar_time": str(ts_utc[mask][last])}

    if last <= or_idx or times[last].hour < OR_END_H or times[last].hour > ENTRY_END_H:
        return status

    # Volume confirmation on the reversal bar, same gate as the tuned run.
    if last >= 5:
        avg_vol = sum(vols[last - 5:last]) / 5.0
        status["vol_ok"] = float(vols[last]) >= avg_vol * VOL_MULT
    else:
        status["vol_ok"] = False
    if not status["vol_ok"]:
        return status

    floor = close * (MIN_SL_PCT / 100.0)

    if swept_high and close < or_high and ext_high:
        raw_risk = ext_high - close
        if raw_risk > 0:
            status.update({"signal": "short", "entry": close, "raw_risk": raw_risk,
                           "stop": close + max(raw_risk, floor),
                           "target": close - raw_risk * RR})
            return status

    if swept_low and close > or_low and ext_low:
        raw_risk = close - ext_low
        if raw_risk > 0:
            status.update({"signal": "long", "entry": close, "raw_risk": raw_risk,
                           "stop": close - max(raw_risk, floor),
                           "target": close + raw_risk * RR})
            return status

    return status


def reconcile(pos):
    """Drop tracking when the broker is flat; book the stop fill if that is why."""
    if not pos:
        return pos, 0.0, 0
    live_q = live_position_qty(SYMBOL)
    if live_q is None or live_q != 0:
        return pos, 0.0, 0

    realized, losses = 0.0, 0
    stop_oid = pos.get("stop_orderid")
    if order_state(stop_oid) == "COMPLETE":
        exit_p = fetch_fill_price(stop_oid, 0.0)
        entry_p = float(pos.get("entry_fill", 0.0) or 0.0)
        qty = int(pos.get("qty", 0) or 0)
        if exit_p > 0 and entry_p > 0 and qty > 0:
            sign = 1.0 if pos.get("side") == "long" else -1.0
            gross = sign * (exit_p - entry_p) * qty * CONTRACT_VALUE
            realized = gross - transaction_cost((entry_p + exit_p) * qty * CONTRACT_VALUE)
            if realized < 0:
                losses = 1
            log.info(f"Stop filled @ {exit_p} (entry {entry_p}) -> ${realized:.4f}")
        else:
            log.warning(f"Stop {stop_oid} complete but fill price unavailable - pnl not booked")
    else:
        safe_cancel(stop_oid, "broker flat")
    release_symbol_lock(SYMBOL)
    log.info("Position closed externally or by stop - tracking cleared")
    return {}, realized, losses


def main():
    log.info(f"Starting {STRATEGY_NAME} | {SYMBOL} ({EXCHANGE}) {INTERVAL} | product={PRODUCT}")
    log.info(f"OR 00:00-{OR_END_H:02d}:00 UTC | entry <= {ENTRY_END_H:02d}:00 UTC | "
             f"sweep>={MIN_SWEEP_PCT}% | vol>={VOL_MULT}x | RR={RR} | BE={BE_ARM_R}R | "
             f"hold<={MAX_HOLD_MINUTES}m | {MAX_TRADES_PER_DAY}/day")

    pos = load_state()
    daily_loss = 0.0
    losses_streak = 0
    utc_day = datetime.now(timezone.utc).date()

    def on_signal(signum, frame):
        log.info("Termination signal received - saving state and exiting")
        save_state(pos)
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.date() != utc_day:
                utc_day = now.date()
                daily_loss, losses_streak = 0.0, 0
                log.info(f"New UTC day {utc_day} - counters reset")

            pos, realized, losses = reconcile(pos)
            if realized or losses:
                if realized < 0:
                    daily_loss += abs(realized)
                losses_streak = losses_streak + losses if losses else 0
                save_state(pos)

            df = client.history(symbol=SYMBOL, exchange=EXCHANGE, interval=INTERVAL,
                               start_date=(now.date() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
                               end_date=now.date().strftime("%Y-%m-%d"))
            sig = compute_signal(df, now.date())

            # Unconditional heartbeat: silence must never be ambiguous between
            # "no setup" and "process wedged". The old build only logged when a
            # signal dict existed, so it went dark for hours after 00:00 UTC.
            if sig is None:
                log.info(f"Heartbeat | {now:%H:%M}Z | building today's range "
                         f"(<2 completed {INTERVAL} bars) | pos={'yes' if pos else 'flat'}")
            else:
                swept = "SWEPT-HIGH" if sig["swept_high"] else ("SWEPT-LOW" if sig["swept_low"] else "NO-SWEEP")
                log.info(f"Heartbeat | {now:%H:%M}Z | close={sig['close']:.1f} | "
                         f"OR {sig['or_low']:.1f}-{sig['or_high']:.1f} | {swept} | "
                         f"vol_ok={sig.get('vol_ok')} | signal={sig['signal'] or 'none'} | "
                         f"pos={'yes' if pos else 'flat'}")

            # ---- manage an open position -------------------------------------
            if pos:
                q = client.quotes(symbol=SYMBOL, exchange=EXCHANGE)
                if not q or q.get("status") != "success":
                    time.sleep(POLL_SECS)
                    continue
                ltp = float(q["data"]["ltp"])
                side = pos["side"]
                entry = float(pos["entry_fill"])
                risk = float(pos["raw_risk"])
                qty = int(pos["qty"])
                sign = 1.0 if side == "long" else -1.0
                fav_r = sign * (ltp - entry) / risk if risk > 0 else 0.0

                if BE_ARM_R > 0 and not pos.get("be_armed") and fav_r >= BE_ARM_R:
                    # Move the resting stop to entry. Same instrument as the
                    # signal, so this is a real break-even, not a proxy.
                    safe_cancel(pos.get("stop_orderid"), "be ratchet")
                    trg = _round_tick(entry)
                    lmt = _round_tick(entry * (0.999 if side == "long" else 1.001))
                    r = client.placeorder(symbol=SYMBOL, exchange=EXCHANGE,
                                          action="SELL" if side == "long" else "BUY",
                                          pricetype="SL", product=PRODUCT, quantity=qty,
                                          price=lmt, trigger_price=trg, strategy=STRATEGY_NAME)
                    if r and r.get("status") == "success":
                        pos["stop_orderid"] = r.get("orderid")
                        pos["be_armed"] = True
                        save_state(pos)
                        log.info(f"BREAK-EVEN ARMED at {fav_r:.2f}R - stop moved to {trg}")

                hit_target = (ltp >= float(pos["target"])) if side == "long" else (ltp <= float(pos["target"]))
                held_min = None
                try:
                    held_min = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60.0
                except (ValueError, TypeError, KeyError):
                    held_min = None

                reason = None
                if hit_target:
                    reason = "TARGET"
                elif held_min is not None and held_min >= MAX_HOLD_MINUTES:
                    reason = "TIME_STOP"

                if reason:
                    log.info(f"{reason} on {SYMBOL} at {ltp} ({fav_r:+.2f}R) - closing")
                    safe_cancel(pos.get("stop_orderid"), reason)
                    ok, fill = close_position(side, qty, reason)
                    if ok:
                        exit_p = fill or ltp
                        gross = sign * (exit_p - entry) * qty * CONTRACT_VALUE
                        pnl = gross - transaction_cost((entry + exit_p) * qty * CONTRACT_VALUE)
                        log.info(f"Booked {reason}: ${pnl:.4f} (entry {entry} exit {exit_p} x{qty})")
                        if pnl < 0:
                            daily_loss += abs(pnl)
                            losses_streak += 1
                        else:
                            losses_streak = 0
                        release_symbol_lock(SYMBOL)
                        pos = {}
                        save_state(pos)
                time.sleep(POLL_SECS)
                continue

            # ---- gates before a new entry ------------------------------------
            if load_done_day() == str(utc_day):
                time.sleep(POLL_SECS)
                continue
            if daily_loss >= DAILY_LOSS_LIMIT:
                log.warning(f"Daily loss ${daily_loss:.2f} >= ${DAILY_LOSS_LIMIT:.2f} - standing down")
                time.sleep(60)
                continue
            if losses_streak >= LOSS_STREAK_LIMIT:
                log.warning(f"Loss streak {losses_streak} >= {LOSS_STREAK_LIMIT} - standing down")
                time.sleep(60)
                continue
            if not sig or not sig.get("signal"):
                time.sleep(POLL_SECS)
                continue

            # ---- enter -------------------------------------------------------
            if not acquire_symbol_lock(SYMBOL):
                time.sleep(POLL_SECS)
                continue

            side = sig["signal"]
            action = "BUY" if side == "long" else "SELL"
            risk_per_contract = float(sig["raw_risk"]) * CONTRACT_VALUE
            qty = contracts_for(sig["entry"], risk_per_contract=risk_per_contract)
            log.info(f"JUDAS {side.upper()} on {SYMBOL}: sweep reversal, entry {sig['entry']:.1f} "
                     f"stop {sig['stop']:.1f} target {sig['target']:.1f} risk {sig['raw_risk']:.1f} qty {qty}")

            entry_resp = client.placeorder(symbol=SYMBOL, exchange=EXCHANGE, action=action,
                                           pricetype="MARKET", product=PRODUCT, quantity=qty,
                                           strategy=STRATEGY_NAME)
            if not entry_resp or entry_resp.get("status") != "success":
                log.error(f"Entry rejected: {entry_resp}")
                release_symbol_lock(SYMBOL)
                time.sleep(POLL_SECS)
                continue

            filled, fill = confirm_fill(entry_resp.get("orderid"))
            entry_fill = fill if (filled and fill > 0) else sig["entry"]

            # Resting stop at the sweep extreme: the backtest's actual stop, and
            # it stays armed if this process dies.
            stop_trg = _round_tick(sig["stop"])
            stop_lmt = _round_tick(stop_trg * (0.998 if side == "long" else 1.002))
            stop_resp = client.placeorder(symbol=SYMBOL, exchange=EXCHANGE,
                                          action="SELL" if side == "long" else "BUY",
                                          pricetype="SL", product=PRODUCT, quantity=qty,
                                          price=stop_lmt, trigger_price=stop_trg,
                                          strategy=STRATEGY_NAME)
            stop_oid = stop_resp.get("orderid") if (stop_resp and stop_resp.get("status") == "success") else None
            if not stop_oid:
                log.error(f"STOP NOT ARMED on {SYMBOL} ({stop_resp}) - in-process exits only")
            else:
                log.info(f"Resting stop armed: trigger {stop_trg} limit {stop_lmt} oid {stop_oid}")

            pos = {"side": side, "qty": qty, "entry_fill": entry_fill,
                   "raw_risk": float(sig["raw_risk"]), "stop": stop_trg,
                   "target": float(sig["target"]), "stop_orderid": stop_oid,
                   "entry_time": datetime.now(timezone.utc).isoformat(),
                   "be_armed": False}
            save_state(pos)
            persist_done(utc_day)   # one trade per UTC day, restart-proof
            time.sleep(POLL_SECS)

        except Exception as e:
            log.error(f"Unhandled exception in scan loop: {e}", exc_info=True)
            time.sleep(POLL_SECS)


if __name__ == '__main__':
    main()
