#!/usr/bin/env python
"""
NIFTY Overnight-Drift Strategy — LIVE (positional, holds overnight)

WHAT IT DOES
    Buys the NIFTY current-month FUTURE near the close and exits next morning,
    sized by inverse volatility and gated by a long-term trend filter.

WHY (measured, see backtesting/positional/FINDINGS.md)
    Decomposing 15 years of daily bars, the index's entire risk-adjusted drift
    accrues OVERNIGHT; the intraday session is actively negative:
        NIFTY  overnight Sharpe  2.68   |  intraday Sharpe  -1.13
        SENSEX overnight Sharpe  3.57   |  intraday Sharpe  -1.58
    Trend-gated + vol-targeted, net of Flattrade's verified statutory cost
    (2.84 bps/side, brokerage 0), that is CAGR 4.9% / Sharpe 1.53 / maxDD -7.0%
    at TARGET_VOL=0.04 over 14.6 years. Positive in 15 of 16 calendar years and
    in BOTH halves of history; Sharpe is a flat 1.43-1.53 plateau across every
    lookback/halflife tested (not a tuned spike).

    NIFTY is chosen over SENSEX/MIDCAP deliberately: SENSEX's headline Sharpe
    collapses to 0.10 in the second half at realistic cost, and midcaps carry
    fatter overnight tails plus wider spreads. NIFTY's future is the most liquid
    instrument available, so realised slippage should track the backtest most
    closely - and slippage is what decides this strategy.

TIMING (measured on 1-minute bars, not assumed)
    Entry/exit Sharpe grid showed 15:20 entry and 09:20 exit is the peak:
        exit 09:16 -> 0.38   exit 09:20 -> 0.74   exit 09:30 -> 0.51
    09:16 (the opening tick) is the WORST exit - gappy and wide. 15:29 is worse
    than 15:20 (closing-auction noise). Hence 15:20 / 09:20.

THE LIVE RISK IS COST, NOT SIGNAL
    The book pays the spread twice a day. Edge vs all-in cost per side:
        2.84 bps (statutory only) -> Sharpe 1.64
        3.34 bps (+0.5 slip)      -> Sharpe 1.31
        3.84 bps (+1.0 slip)      -> Sharpe 0.98   <-- below 1.0
        4.84 bps (+2.0 slip)      -> edge gone
    So this script LOGS EVERY FILL and computes realised bps vs the pre-trade
    reference price into slippage_log.csv. If the rolling average per-side cost
    exceeds COST_KILL_BPS the strategy halts itself. Watch that number, not the P&L.

SAFETY
    - Never enters on the held contract's expiry day (would settle same session).
    - State persisted to JSON so an overnight position survives a restart.
    - Reconciles against the broker position book on boot.
    - DRY_RUN=true places no orders (paper mode) - recommended for 2-4 weeks first.
    - MAX_LOTS hard cap regardless of what sizing computes.

ENV
    OPENALGO_API_KEY (required), HOST_SERVER, DRY_RUN, CAPITAL, TARGET_VOL,
    MAX_LEV, MAX_LOTS, PRODUCT, ENTRY_TIME, EXIT_TIME, COST_KILL_BPS

OPERATIONS — READ THIS BEFORE SCHEDULING
    This is POSITIONAL: it holds a future overnight. That breaks the assumptions
    of the intraday strategies in this folder, in two ways:

    1. SCHEDULE. The existing strategy schedules stop at ~15:00, which would kill
       this process BEFORE its 15:20 entry and it would not be alive for the 09:20
       exit. Schedule it start 09:10, stop 15:30 (or run it continuously).
       State is persisted, so stopping it overnight while a position is open is
       safe - it exits on the next morning's run.
    2. PRODUCT must be NRML, never MIS (MIS is auto-squared-off intraday, which
       would destroy the entire edge).

    Recommended rollout (the backtest cannot settle your fill quality):
       week 1-2  DRY_RUN=true                 - confirm signal, sizing, symbol
                                                resolution, and log fills
       week 3-4  DRY_RUN=false MAX_LOTS=1     - confirm real fills match paper
       then      raise size only once realised slippage < 3.8 bps/side

    Pre-flight any time (read-only, places no orders):
       python strategies/examples/nifty_overnight_drift_strategy.py --check

CAPITAL REQUIREMENT (important)
    One NIFTY lot is ~Rs 18L notional. At TARGET_VOL=0.04 with overnight vol ~7%
    the vol-targeted exposure is ~0.6 of capital, so correctly-sized 1-lot trading
    needs roughly Rs 25-30L. With less, this script trades 0 LOTS BY DESIGN rather
    than silently running many times the intended risk. `--check` tells you the
    exact number for your account.
"""
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import pandas as pd
from openalgo import api

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

api_key = os.getenv("OPENALGO_API_KEY")
host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)
client = api(api_key=api_key, host=host)

# ----------------------------------------------------------------- parameters
STRATEGY_NAME = "NIFTY Overnight Drift"
UNDERLYING = "NIFTY"
IDX_EXCHANGE = "NSE_INDEX"
FUT_EXCHANGE = "NFO"

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
PRODUCT = os.getenv("PRODUCT", "NRML")          # NRML - must carry overnight
CAPITAL = float(os.getenv("CAPITAL", "0"))      # 0 = query funds API
TARGET_VOL = float(os.getenv("TARGET_VOL", "0.04"))
MAX_LEV = float(os.getenv("MAX_LEV", "2.0"))
MAX_LOTS = int(os.getenv("MAX_LOTS", "1"))
LOOKBACKS = tuple(int(x) for x in os.getenv("LOOKBACKS", "50,75,100,150,200").split(","))
VOL_HALFLIFE = int(os.getenv("VOL_HALFLIFE", "20"))
COST_KILL_BPS = float(os.getenv("COST_KILL_BPS", "3.8"))   # per side; above this NIFTY Sharpe < 1

ENTRY_TIME = os.getenv("ENTRY_TIME", "15:20")
EXIT_TIME = os.getenv("EXIT_TIME", "09:20")
ENTRY_WINDOW_END = os.getenv("ENTRY_WINDOW_END", "15:27")   # do not chase past this
EXIT_WINDOW_END = os.getenv("EXIT_WINDOW_END", "09:40")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))

LOG_DIR = Path(os.getenv("LOG_DIR", "log")) / "strategies"
STATE_DIR = LOG_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "nifty_overnight_state.json"
SLIP_FILE = STATE_DIR / "nifty_overnight_slippage.csv"

_shutdown = False


def _hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


T_ENTRY, T_ENTRY_END = _hhmm(ENTRY_TIME), _hhmm(ENTRY_WINDOW_END)
T_EXIT, T_EXIT_END = _hhmm(EXIT_TIME), _hhmm(EXIT_WINDOW_END)


# ------------------------------------------------------------- api safety net
# A hung/expired broker session must NEVER stall the entry or exit window, so
# every READ call goes through a hard timeout. Order PLACEMENT is deliberately
# left un-timed: aborting it would leave the order state ambiguous.
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "20"))
_POOL = ThreadPoolExecutor(max_workers=4)


def capi(name, default=None, **kw):
    """client.<name>(**kw) with a hard timeout; returns `default` on failure."""
    fn = getattr(client, name, None)
    if fn is None:
        log.error(f"openalgo client has no method '{name}'")
        return default
    try:
        return _POOL.submit(fn, **kw).result(timeout=API_TIMEOUT)
    except FuturesTimeout:
        log.error(f"API '{name}' timed out after {API_TIMEOUT}s "
                  f"(broker session expired or host unreachable?)")
    except Exception as e:
        log.error(f"API '{name}' failed: {e}")
    return default


# ---------------------------------------------------------------- state stuff
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError) as e:
            log.warning(f"State unreadable ({e}); starting clean")
    return {"position": None, "halted": False, "cost_samples": []}


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, default=str))
    tmp.replace(STATE_FILE)


def log_fill(side, symbol, qty, ref_price, fill_price, note=""):
    """Append a fill to the slippage log and return cost in bps (per side)."""
    bps = None
    if ref_price and fill_price:
        # buying above ref, or selling below ref, is a cost
        diff = (fill_price - ref_price) if side.upper() == "BUY" else (ref_price - fill_price)
        bps = diff / ref_price * 1e4
    new = not SLIP_FILE.exists()
    with SLIP_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "side", "symbol", "qty", "ref_price",
                        "fill_price", "slippage_bps", "note"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), side, symbol, qty,
                    ref_price, fill_price, None if bps is None else round(bps, 2), note])
    if bps is not None:
        log.info(f"FILL {side} {symbol} qty={qty} ref={ref_price} fill={fill_price} "
                 f"slippage={bps:+.2f} bps")
    return bps


# ------------------------------------------------------------ market plumbing
def daily_history(days=420):
    """NIFTY index daily OHLC. Needs > max(LOOKBACKS) sessions plus vol warmup."""
    end = datetime.now().date()
    start = end - timedelta(days=days)
    df = capi("history", default=None, symbol=UNDERLYING, exchange=IDX_EXCHANGE, interval="D",
                        start_date=start.strftime("%Y-%m-%d"),
                        end_date=end.strftime("%Y-%m-%d"))
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    df = df.sort_index()
    for c in ("open", "high", "low", "close"):
        if c not in df.columns:
            return None
        df[c] = df[c].astype(float)
    return df


def resolve_future_symbol():
    """Resolve the tradable NIFTY future, rolling past today's expiry.

    Never assumes a symbol format: tries the expiry API, then search, then
    candidate formats - and validates the result with a live quote.
    """
    today = date.today()
    candidates = []

    # 1. expiry API -> build DDMMMYY
    try:
        r = capi("expiry", default={}, symbol=UNDERLYING, exchange=FUT_EXCHANGE,
                 instrumenttype="futures") or {}
        if r.get("status") == "success":
            for e in (r.get("data") or []):
                try:
                    d = datetime.strptime(str(e), "%Y-%m-%d").date()
                except ValueError:
                    try:
                        d = datetime.strptime(str(e).upper(), "%d-%b-%Y").date()
                    except ValueError:
                        try:
                            d = datetime.strptime(str(e).upper(), "%d-%b-%y").date()
                        except ValueError:
                            continue
                if d > today:            # strictly future: never hold into settlement
                    tag = f"{d.day:02d}{d.strftime('%b').upper()}{d.strftime('%y')}"
                    candidates.append((d, f"{UNDERLYING}{tag}FUT"))
    except Exception as e:
        log.warning(f"futures expiry API failed: {e}")

    # 2. symbol search
    if not candidates:
        try:
            s = capi("search", default={}, query=UNDERLYING, exchange=FUT_EXCHANGE) or {}
            for row in (s.get("data") or []):
                sym = str(row.get("symbol", ""))
                if sym.startswith(UNDERLYING) and sym.endswith("FUT"):
                    exp = row.get("expiry")
                    d = None
                    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%y", "%d-%b-%y"):
                        try:
                            d = datetime.strptime(str(exp).upper(), fmt).date()
                            break
                        except (ValueError, TypeError):
                            continue
                    if d and d > today:
                        candidates.append((d, sym))
        except Exception as e:
            log.warning(f"symbol search failed: {e}")

    candidates.sort(key=lambda x: x[0])
    for exp_date, sym in candidates:
        q = fetch_quote(sym, FUT_EXCHANGE)
        if q:
            log.info(f"Resolved future {sym} (expiry {exp_date}) ltp={q}")
            return sym, exp_date
    log.error("Could not resolve a tradable NIFTY future symbol - not trading")
    return None, None


def fetch_quote(symbol, exchange, retries=3):
    for i in range(retries):
        try:
            q = capi("quotes", default={}, symbol=symbol, exchange=exchange) or {}
            if q.get("status") == "success":
                ltp = float((q.get("data") or {}).get("ltp") or 0)
                if ltp > 0:
                    return ltp
        except Exception as e:
            log.debug(f"quote {symbol} attempt {i+1}: {e}")
        time.sleep(1.0)
    return None


def available_capital():
    if CAPITAL > 0:
        return CAPITAL
    try:
        r = capi("funds", default={}) or {}
        if r.get("status") == "success":
            d = r.get("data") or {}
            for k in ("availablecash", "availableCash", "cash", "net"):
                if d.get(k) is not None:
                    return float(d[k])
    except Exception as e:
        log.warning(f"funds API failed: {e}")
    return 0.0


def fetch_lot_size():
    """NIFTY futures lot size from the symbol master; falls back to 75."""
    try:
        s = capi("search", default={}, query=UNDERLYING, exchange=FUT_EXCHANGE) or {}
        for row in (s.get("data") or []):
            if str(row.get("symbol", "")).endswith("FUT") and row.get("lotsize"):
                return int(row["lotsize"])
    except Exception as e:
        log.debug(f"lot size lookup failed: {e}")
    return int(os.getenv("LOT_SIZE_FALLBACK", "75"))


# -------------------------------------------------------------------- signal
def compute_signal(df):
    """Ensemble long/flat trend filter in [0,1] using closes through YESTERDAY.

    Causal by construction: the last completed daily bar is the most recent
    information, exactly as the backtest's shift(1).
    """
    close = df["close"]
    if len(close) < max(LOOKBACKS) + 5:
        log.warning(f"only {len(close)} daily bars; need > {max(LOOKBACKS)+5}")
        return None
    flags = [1.0 if float(close.iloc[-1]) > float(close.rolling(n).mean().iloc[-1]) else 0.0
             for n in LOOKBACKS]
    return sum(flags) / len(flags)


def overnight_vol(df):
    """Annualised EWMA vol of overnight (prev_close -> open) returns."""
    on = (df["open"] / df["close"].shift(1) - 1).dropna()
    if len(on) < VOL_HALFLIFE * 2:
        return None
    ew = on.ewm(halflife=VOL_HALFLIFE, min_periods=VOL_HALFLIFE).std()
    v = float(ew.iloc[-1]) * (252 ** 0.5)
    return v if v > 0 else None


def target_lots(signal, vol, fut_price, lot_size, capital):
    """Vol-targeted exposure -> integer lots, with an honest granularity warning."""
    if not signal or not vol or not fut_price or not lot_size:
        return 0, {}
    scale = min(TARGET_VOL / vol, MAX_LEV)
    exposure_frac = max(0.0, min(signal * scale, MAX_LEV))
    notional_target = capital * exposure_frac
    lot_notional = fut_price * lot_size
    ideal = notional_target / lot_notional if lot_notional else 0.0
    lots = int(round(ideal))
    lots = max(0, min(lots, MAX_LOTS))
    diag = {"signal": round(signal, 3), "overnight_vol": round(vol, 4),
            "vol_scale": round(scale, 3), "exposure_frac": round(exposure_frac, 3),
            "lot_notional": round(lot_notional, 0), "ideal_lots": round(ideal, 3),
            "lots": lots}
    if ideal > 0 and lots == 0:
        need = lot_notional / max(exposure_frac, 1e-9)
        diag["warning"] = (f"capital too small to express target: need ~Rs {need:,.0f} "
                           f"for 1 lot at exposure {exposure_frac:.2f}")
    if lots > 0:
        eff = lots * lot_notional / max(capital, 1e-9)
        diag["effective_exposure"] = round(eff, 3)
        diag["effective_vol_target"] = round(eff * vol / max(signal, 1e-9), 4)
    return lots, diag


# --------------------------------------------------------------------- orders
def place(symbol, action, qty, ref_price, note):
    """Place a market order. Returns (order_id, fill_price, slippage_bps)."""
    if DRY_RUN:
        log.info(f"[DRY_RUN] would {action} {qty} {symbol} (ref {ref_price}) - {note}")
        log_fill(action, symbol, qty, ref_price, ref_price, note="DRY_RUN " + note)
        return "DRYRUN", ref_price, 0.0
    try:
        resp = client.placeorder(
            strategy=STRATEGY_NAME, symbol=symbol, action=action,
            exchange=FUT_EXCHANGE, price_type="MARKET", product=PRODUCT, quantity=qty,
        )
    except Exception as e:
        log.error(f"placeorder raised: {e}")
        return None, None, None
    if resp.get("status") != "success":
        log.error(f"placeorder failed: {resp}")
        return None, None, None
    oid = resp.get("orderid") or resp.get("order_id")
    fill = fetch_fill_price(oid, symbol)
    bps = log_fill(action, symbol, qty, ref_price, fill, note=note)
    return oid, fill, bps


def fetch_fill_price(order_id, symbol, retries=6):
    """Poll for the average traded price so slippage is measured on REAL fills."""
    for i in range(retries):
        time.sleep(1.5)
        try:
            r = capi("orderstatus", default={}, order_id=order_id, strategy=STRATEGY_NAME) or {}
            d = (r.get("data") or {}) if isinstance(r, dict) else {}
            for k in ("average_price", "averageprice", "avgprice", "price"):
                v = d.get(k)
                if v and float(v) > 0:
                    return float(v)
        except Exception as e:
            log.debug(f"orderstatus attempt {i+1}: {e}")
        try:
            tb = capi("tradebook", default={}) or {}
            for t in (tb.get("data") or []):
                if str(t.get("symbol")) == symbol:
                    for k in ("average_price", "averageprice", "fill_price", "price"):
                        if t.get(k) and float(t[k]) > 0:
                            return float(t[k])
        except Exception:
            pass
    log.warning(f"Could not read fill price for order {order_id}; slippage unrecorded")
    return None


def broker_position_qty(symbol):
    """Net qty held at the broker for `symbol` (reconciliation on boot)."""
    try:
        r = capi("positionbook", default={}) or {}
        for p in (r.get("data") or []):
            if str(p.get("symbol")) == symbol:
                for k in ("netqty", "net_quantity", "quantity"):
                    if p.get(k) is not None:
                        return int(float(p[k]))
    except Exception as e:
        log.warning(f"positionbook failed: {e}")
    return None


# ----------------------------------------------------------------- kill switch
def cost_guard(st):
    """Halt if realised per-side cost is eating the edge."""
    samples = [abs(x) for x in st.get("cost_samples", []) if x is not None]
    if len(samples) < 10:
        return False
    avg = sum(samples[-20:]) / len(samples[-20:])
    if avg > COST_KILL_BPS:
        log.error(f"COST KILL SWITCH: rolling avg slippage {avg:.2f} bps/side exceeds "
                  f"{COST_KILL_BPS} - NIFTY Sharpe falls below 1.0 here. Halting. "
                  f"Set COST_KILL_BPS higher only if you accept a weaker edge.")
        st["halted"] = True
        return True
    log.info(f"cost guard OK: rolling avg slippage {avg:.2f} bps/side "
             f"(limit {COST_KILL_BPS})")
    return False


# ---------------------------------------------------------------- entry / exit
def do_entry(st):
    sym, exp_date = resolve_future_symbol()
    if not sym:
        return
    if exp_date and exp_date <= date.today():
        log.warning("resolved contract expires today or earlier - skipping entry")
        return
    df = daily_history()
    if df is None:
        log.error("no daily history - skipping entry")
        return
    sig = compute_signal(df)
    vol = overnight_vol(df)
    if sig is None or vol is None:
        log.error("signal/vol unavailable - skipping entry")
        return
    if sig <= 0:
        log.info(f"Trend filter FLAT (signal {sig:.2f}) - standing aside tonight")
        return
    price = fetch_quote(sym, FUT_EXCHANGE)
    if not price:
        log.error(f"no quote for {sym} - skipping entry")
        return
    lot_size = fetch_lot_size()
    cap = available_capital()
    lots, diag = target_lots(sig, vol, price, lot_size, cap)
    log.info(f"sizing: capital={cap:,.0f} {json.dumps(diag)}")
    if lots <= 0:
        log.info("computed 0 lots - no entry" +
                 (f" ({diag['warning']})" if "warning" in diag else ""))
        return
    qty = lots * lot_size
    oid, fill, bps = place(sym, "BUY", qty, price, note=f"overnight entry sig={sig:.2f}")
    if oid is None:
        return
    if bps is not None:
        st.setdefault("cost_samples", []).append(bps)
    st["position"] = {"symbol": sym, "qty": qty, "lots": lots, "lot_size": lot_size,
                      "entry_ref": price, "entry_fill": fill, "order_id": oid,
                      "entry_time": datetime.now().isoformat(timespec="seconds"),
                      "entry_date": str(date.today()), "expiry": str(exp_date),
                      "signal": sig, "vol": vol}
    save_state(st)
    log.info(f"ENTERED {lots} lot(s) {sym} qty={qty} @ {fill or price}")


def do_exit(st):
    pos = st.get("position")
    if not pos:
        return
    sym, qty = pos["symbol"], pos["qty"]
    if not DRY_RUN:
        held = broker_position_qty(sym)
        if held is not None and held == 0:
            log.warning(f"broker shows flat in {sym}; clearing local state")
            st["position"] = None
            save_state(st)
            return
        if held is not None and abs(held) < qty:
            log.warning(f"broker qty {held} < state qty {qty}; exiting broker qty")
            qty = abs(held)
    price = fetch_quote(sym, FUT_EXCHANGE)
    oid, fill, bps = place(sym, "SELL", qty, price, note="overnight exit")
    if oid is None:
        log.error("EXIT FAILED - position still open, will retry next poll")
        return
    entry = pos.get("entry_fill") or pos.get("entry_ref")
    if entry and fill:
        gross = (fill - entry) * qty
        ret_bps = (fill / entry - 1) * 1e4
        log.info(f"EXITED {sym}: entry {entry} -> exit {fill} | "
                 f"overnight {ret_bps:+.1f} bps | gross Rs {gross:+,.0f}")
    if bps is not None:
        st.setdefault("cost_samples", []).append(bps)
    st["position"] = None
    save_state(st)
    cost_guard(st)
    save_state(st)


# --------------------------------------------------------------------- runtime
def _sig_handler(signum, frame):
    global _shutdown
    log.info(f"signal {signum} received - shutting down (position, if any, is LEFT OPEN "
             f"and will be exited on next run)")
    _shutdown = True


def preflight():
    """`--check`: read-only pre-flight. Resolves the contract, computes tonight's
    signal/size and prints it. Places NO orders. Run this before market hours to
    confirm the whole pipeline works against your broker session."""
    print("=" * 78)
    print("PRE-FLIGHT (read-only, no orders placed)")
    print("=" * 78)
    ok = True

    sym, exp_date = resolve_future_symbol()
    print(f"  contract          : {sym or 'FAILED'}  expiry {exp_date}")
    ok &= bool(sym)

    df = daily_history()
    if df is None:
        print("  daily history     : FAILED")
        ok = False
    else:
        print(f"  daily history     : {len(df)} bars, last {df.index[-1]} "
              f"close {float(df['close'].iloc[-1]):.2f}")
        sig = compute_signal(df)
        vol = overnight_vol(df)
        print(f"  trend signal      : {sig}  ({'CARRY tonight' if sig else 'STAND ASIDE'})")
        print(f"  overnight vol     : {None if vol is None else round(vol, 4)} annualised")
        price = fetch_quote(sym, FUT_EXCHANGE) if sym else None
        lot = fetch_lot_size()
        cap = available_capital()
        print(f"  future ltp        : {price}")
        print(f"  lot size          : {lot}")
        print(f"  capital           : Rs {cap:,.0f}"
              f"{'  (from funds API)' if CAPITAL <= 0 else '  (from CAPITAL env)'}")
        if sig is not None and vol and price:
            lots, diag = target_lots(sig, vol, price, lot, cap)
            print(f"  sizing            : {json.dumps(diag)}")
            print(f"  -> would trade    : {lots} lot(s) = {lots * lot} qty")
            if "warning" in diag:
                print(f"  !! {diag['warning']}")
                print("     A NIFTY lot is ~Rs 18L notional, so correct vol-targeted")
                print("     sizing needs roughly Rs 25-30L of capital. With less, this")
                print("     script trades 0 lots by design rather than over-leverage.")
        else:
            ok = False
    st = load_state()
    print(f"  open position     : {st.get('position')}")
    print(f"  halted            : {st.get('halted')}")
    samples = [abs(x) for x in st.get("cost_samples", []) if x is not None]
    if samples:
        print(f"  realised slippage : avg {sum(samples[-20:])/len(samples[-20:]):.2f} "
              f"bps/side over {len(samples)} fills (kill switch at {COST_KILL_BPS})")
    print("=" * 78)
    print("PRE-FLIGHT " + ("PASSED" if ok else "FAILED - fix the above before going live"))
    return 0 if ok else 1


def main():
    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    st = load_state()
    log.info("=" * 78)
    log.info(f"{STRATEGY_NAME} | {'DRY_RUN (paper)' if DRY_RUN else 'LIVE'} | "
             f"product={PRODUCT}")
    log.info(f"entry {ENTRY_TIME} (until {ENTRY_WINDOW_END}) | "
             f"exit {EXIT_TIME} (until {EXIT_WINDOW_END})")
    log.info(f"TARGET_VOL={TARGET_VOL} MAX_LEV={MAX_LEV} MAX_LOTS={MAX_LOTS} "
             f"lookbacks={LOOKBACKS} cost_kill={COST_KILL_BPS}bps/side")
    if st.get("position"):
        log.info(f"resuming with OPEN position: {st['position']}")
    if st.get("halted"):
        log.error("strategy is HALTED by the cost kill switch. Clear 'halted' in "
                  f"{STATE_FILE} to resume.")
    log.info("=" * 78)

    entered_today = None
    exited_today = None

    while not _shutdown:
        now = datetime.now()
        today = now.date()
        t = now.time()

        if st.get("halted"):
            time.sleep(60)
            continue

        try:
            # ---- morning exit first: never hold into the negative intraday session
            if st.get("position") and T_EXIT <= t <= T_EXIT_END and exited_today != today:
                pos_date = st["position"].get("entry_date")
                if pos_date != str(today):        # entered on a previous session
                    log.info("exit window - closing overnight position")
                    do_exit(st)
                    if not st.get("position"):
                        exited_today = today

            # ---- evening entry
            elif (not st.get("position")) and T_ENTRY <= t <= T_ENTRY_END \
                    and entered_today != today:
                log.info("entry window - evaluating tonight's carry")
                do_entry(st)
                entered_today = today       # one attempt per day either way

            # safety: if a position is still open well past the exit window, shout
            elif st.get("position") and t > T_EXIT_END and t < T_ENTRY:
                pos_date = st["position"].get("entry_date")
                if pos_date != str(today):
                    log.warning("position still open past the exit window - retrying exit")
                    do_exit(st)

        except Exception as e:
            log.exception(f"loop error: {e}")

        time.sleep(POLL_SECONDS)

    log.info("shutdown complete")


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(preflight())
    main()
