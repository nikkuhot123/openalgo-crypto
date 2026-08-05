#!/usr/bin/env python
"""
CAS Window Logger — records the 15:25-15:45 blackout the historical feed hides.
=============================================================================
Why this exists
---------------
SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/2765/2026 (2026-01-16), live from
2026-08-03, ends continuous cash trading in F&O-underlying stocks at 15:15 and
runs a Closing Auction Session 15:15-15:35. Para 4.2.3 keeps the equity
derivatives segment open to 15:40.

The broker's historical API does not return that window. Measured 2026-08-04 on
NIFTY25AUG26FUT: 1m stops at 15:29, 3m at 15:27, 5m/10m at 15:25, 15m/30m/1h at
15:15 - zero bars after 15:29 at any granularity. Yet the daily bar close
disagrees with the last 1m close (-13.70 on Aug 4), so trading IS happening
there and we simply cannot see it.

`nifty_overnight_drift_strategy` enters at 15:26 - inside CAS Session 3, with
14 minutes of futures trading still ahead of it. Whether that entry should move
to ~15:38 cannot be decided from historical bars that do not exist. So poll the
live quote endpoint and build the record ourselves.

Writes one CSV row per symbol per poll to:
    log/strategies/cas_window/cas_ticks_YYYY-MM-DD.csv

Run it as a scheduled OpenAlgo strategy (start 15:20, stop 15:50) or standalone.
It places no orders and holds no positions.
"""
import csv
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from openalgo import api

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

api_key = os.getenv("OPENALGO_API_KEY")
host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
if not api_key:
    log.error("OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=api_key, host=host)

STRATEGY_NAME = os.getenv("CAS_LOG_NAME", "CAS Window Logger")
START_TIME = os.getenv("CAS_LOG_START", "15:25")
STOP_TIME = os.getenv("CAS_LOG_STOP", "15:45")
POLL_SECONDS = float(os.getenv("CAS_LOG_POLL", "2"))

# Also sample ATM option Greeks. Off by default so the CAS-window run stays
# lean; a second full-session instance turns it on.
#
# Why: theta cannot be inferred reliably from price series. Two independent
# estimators (regressing option 1m returns on spot, and measuring drift over
# flat-spot windows) both produced POSITIVE theta on multiple sessions, which
# is impossible for a long option - implied-vol movement swamps decay at these
# sample sizes. The broker exposes optiongreeks(), which settles it directly,
# but it returns empty outside market hours. So collect it live.
LOG_GREEKS = os.getenv("CAS_LOG_GREEKS", "false").lower() in ("1", "true", "yes")
OUT_SUBDIR = os.getenv("CAS_LOG_SUBDIR", "cas_window")

OUT_DIR = Path("log") / "strategies" / OUT_SUBDIR
OUT_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = ["ts", "symbol", "exchange", "kind", "ltp", "bid", "ask", "spread",
          "volume", "oi", "open", "high", "low", "prev_close",
          "delta", "theta", "gamma", "vega", "iv", "dte"]

_shutdown = False


def _hhmm(s):
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


T_START, T_STOP = _hhmm(START_TIME), _hhmm(STOP_TIME)


def _graceful(signum, _frame):
    global _shutdown
    _shutdown = True
    log.info(f"Signal {signal.Signals(signum).name} received — flushing and exiting.")


signal.signal(signal.SIGINT, _graceful)
signal.signal(signal.SIGTERM, _graceful)


def resolve_front_future(underlying, exchange):
    """Resolve the front-month future symbol, skipping today's expiry."""
    try:
        r = client.expiry(symbol=underlying, exchange=exchange, instrumenttype="futures")
        if r.get("status") != "success":
            return None
        today = date.today()
        for e in r.get("data", []):
            try:
                d = datetime.strptime(str(e).upper(), "%d-%b-%y").date()
            except ValueError:
                continue
            if d > today:  # never the settling contract
                return f"{underlying}{d.day:02d}{d.strftime('%b').upper()}{d.strftime('%y')}FUT"
    except Exception as e:
        log.warning(f"Could not resolve future for {underlying}: {e}")
    return None


def resolve_atm_options(underlying, idx_exchange, opt_exchange):
    """Nearest-expiry ATM CE and PE, with their expiry date for DTE."""
    out = []
    try:
        e = client.expiry(symbol=underlying, exchange=opt_exchange, instrumenttype="options")
        if e.get("status") != "success" or not e.get("data"):
            return out
        exp_str = e["data"][0]
        exp_date = datetime.strptime(str(exp_str).upper(), "%d-%b-%y").date()
        for ot in ("CE", "PE"):
            r = client.optionsymbol(
                underlying=underlying, exchange=idx_exchange,
                expiry_date=exp_str.replace("-", ""), offset="ATM", option_type=ot,
            )
            sym = r.get("symbol") if isinstance(r, dict) else None
            if sym:
                out.append((sym, opt_exchange, f"opt{ot}", underlying, idx_exchange, exp_date))
    except Exception as ex:
        log.warning(f"Could not resolve ATM options for {underlying}: {ex}")
    return out


def build_watchlist():
    """Spot indices (freeze at 15:15) + futures (trade to 15:40) [+ ATM options]."""
    wl = [
        ("NIFTY", "NSE_INDEX", "spot", None, None, None),
        ("SENSEX", "BSE_INDEX", "spot", None, None, None),
        ("INDIAVIX", "NSE_INDEX", "spot", None, None, None),
    ]
    for und, fut_exch in [("NIFTY", "NFO"), ("SENSEX", "BFO")]:
        sym = resolve_front_future(und, fut_exch)
        if sym:
            wl.append((sym, fut_exch, "future", None, None, None))
            log.info(f"Watching future: {sym} on {fut_exch}")
        else:
            log.warning(f"No future resolved for {und} — spot only")

    if LOG_GREEKS:
        for und, idx_ex, opt_ex in [("NIFTY", "NSE_INDEX", "NFO"),
                                    ("SENSEX", "BSE_INDEX", "BFO")]:
            for leg in resolve_atm_options(und, idx_ex, opt_ex):
                wl.append(leg)
                log.info(f"Watching ATM option: {leg[0]} (expiry {leg[5]})")
    return wl


def fetch_greeks(symbol, exchange, underlying, idx_exchange):
    """Broker-computed Greeks, or empty dict. Never raises."""
    try:
        r = client.optiongreeks(symbol=symbol, exchange=exchange,
                                underlying_symbol=underlying,
                                underlying_exchange=idx_exchange)
        if not isinstance(r, dict) or r.get("status") != "success":
            return {}
        d = r.get("data", {}) or {}
        g = d.get("greeks", d) or {}
        return {
            "delta": g.get("delta"),
            "theta": g.get("theta"),
            "gamma": g.get("gamma"),
            "vega": g.get("vega"),
            "iv": g.get("iv") or g.get("implied_volatility"),
        }
    except Exception as e:
        log.debug(f"greeks failed {symbol}: {e}")
        return {}


def poll_row(symbol, exchange, kind, underlying=None, idx_exchange=None, expiry=None):
    """One quote (+ Greeks for option legs) -> one CSV row, or None."""
    try:
        r = client.quotes(symbol=symbol, exchange=exchange)
        if r.get("status") != "success":
            return None
        d = r.get("data", {}) or {}
        bid = float(d.get("bid", 0) or 0)
        ask = float(d.get("ask", 0) or 0)
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "symbol": symbol,
            "exchange": exchange,
            "kind": kind,
            "ltp": d.get("ltp"),
            "bid": bid or None,
            "ask": ask or None,
            "spread": round(ask - bid, 4) if (bid > 0 and ask > 0) else None,
            "volume": d.get("volume"),
            "oi": d.get("oi"),
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "prev_close": d.get("prev_close"),
            "delta": None, "theta": None, "gamma": None, "vega": None,
            "iv": None, "dte": None,
        }
        if kind.startswith("opt"):
            row.update(fetch_greeks(symbol, exchange, underlying, idx_exchange))
            if expiry:
                row["dte"] = (expiry - date.today()).days
        return row
    except Exception as e:
        log.debug(f"quote failed {symbol}: {e}")
        return None


def run():
    log.info("=" * 68)
    log.info(f"{STRATEGY_NAME} | window {START_TIME}-{STOP_TIME} | poll {POLL_SECONDS}s")
    log.info("Read-only: places no orders, holds no positions.")
    log.info("=" * 68)

    watchlist = build_watchlist()
    if not watchlist:
        log.error("Empty watchlist — nothing to log.")
        return

    while not _shutdown:
        today = date.today()
        now = datetime.now()
        t = now.time()

        if t < T_START:
            wait = (datetime.combine(today, T_START) - now).total_seconds()
            log.info(f"Before window. Sleeping {int(wait)}s until {START_TIME}...")
            time.sleep(min(wait + 1, 60))
            continue

        if t > T_STOP:
            # Past the window: idle until tomorrow rather than spinning.
            nxt = datetime.combine(today + timedelta(days=1), T_START)
            log.info(f"Window closed. Next session {nxt:%Y-%m-%d %H:%M}. Sleeping.")
            while not _shutdown and datetime.now() < nxt:
                time.sleep(60)
            continue

        # ── inside the window: append rows ──
        out = OUT_DIR / f"cas_ticks_{today:%Y-%m-%d}.csv"
        new_file = not out.exists()
        rows_this_session = 0
        log.info(f"Window OPEN — logging {len(watchlist)} symbols to {out}")

        with out.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new_file:
                w.writeheader()

            while not _shutdown and datetime.now().time() <= T_STOP and date.today() == today:
                cycle = datetime.now()
                for symbol, exchange, kind, und, idx_ex, exp in watchlist:
                    row = poll_row(symbol, exchange, kind, und, idx_ex, exp)
                    if row:
                        w.writerow(row)
                        rows_this_session += 1
                fh.flush()  # survive a mid-window kill

                if rows_this_session and rows_this_session % (len(watchlist) * 30) == 0:
                    log.info(f"  {cycle:%H:%M:%S} | {rows_this_session} rows written")

                elapsed = (datetime.now() - cycle).total_seconds()
                time.sleep(max(0.0, POLL_SECONDS - elapsed))

        log.info(f"Window closed. {rows_this_session} rows -> {out}")

    log.info("Shutdown complete. Exiting.")


if __name__ == "__main__":
    run()
