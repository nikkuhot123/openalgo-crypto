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

STRATEGY_NAME = "CAS Window Logger"
START_TIME = os.getenv("CAS_LOG_START", "15:25")
STOP_TIME = os.getenv("CAS_LOG_STOP", "15:45")
POLL_SECONDS = float(os.getenv("CAS_LOG_POLL", "2"))

OUT_DIR = Path("log") / "strategies" / "cas_window"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = ["ts", "symbol", "exchange", "kind", "ltp", "bid", "ask", "spread",
          "volume", "oi", "open", "high", "low", "prev_close"]

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


def build_watchlist():
    """Spot indices (freeze at 15:15) + their futures (trade to 15:40)."""
    wl = [
        ("NIFTY", "NSE_INDEX", "spot"),
        ("SENSEX", "BSE_INDEX", "spot"),
        ("INDIAVIX", "NSE_INDEX", "spot"),
    ]
    for und, fut_exch in [("NIFTY", "NFO"), ("SENSEX", "BFO")]:
        sym = resolve_front_future(und, fut_exch)
        if sym:
            wl.append((sym, fut_exch, "future"))
            log.info(f"Watching future: {sym} on {fut_exch}")
        else:
            log.warning(f"No future resolved for {und} — spot only")
    return wl


def poll_row(symbol, exchange, kind):
    """One quote -> one CSV row, or None if the quote failed."""
    try:
        r = client.quotes(symbol=symbol, exchange=exchange)
        if r.get("status") != "success":
            return None
        d = r.get("data", {}) or {}
        bid = float(d.get("bid", 0) or 0)
        ask = float(d.get("ask", 0) or 0)
        return {
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
        }
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
                for symbol, exchange, kind in watchlist:
                    row = poll_row(symbol, exchange, kind)
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
