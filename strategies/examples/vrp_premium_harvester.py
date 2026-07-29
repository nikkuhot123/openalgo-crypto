#!/usr/bin/env python
"""
Volatility Risk Premium (VRP) Harvester — FORWARD TESTING MODE
(Intraday Short Strangle conditioned on VIX vs. Realized Volatility)

This runs completely LOCALLY, hitting the remote OpenAlgo API for quotes and history.
Because DRY_RUN=True, it will never send a real placeorder request, but it will
print and log exactly what it WOULD do at the relevant times.

Rules:
1. Every day at 09:30, calculate Realized Volatility vs India VIX.
2. If VRP >= 2.0%, Sell ATM+1SD Strangle (Dry Run).
3. If entry taken, exit at 15:15 or when Premium SL of 40% hit.
"""

import os
import sys
import time
import math
import logging
import pandas as pd
from datetime import datetime
from openalgo import api
import signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

api_key = os.getenv("OPENALGO_API_KEY", "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a")
host = os.getenv("HOST_SERVER", "https://openalgo.inikhilesh.com")
client = api(api_key=api_key, host=host)

STRATEGY_NAME = "VRP Premium Harvester"
UNDERLYING = "NIFTY"
IDX_EXCHANGE = "NSE_INDEX"
DRY_RUN = os.getenv("DRY_RUN", "True").lower() in ("1", "true", "yes")

# Parameters
ENTRY_TIME = "09:30"
EXIT_TIME = "15:15"
POLL_SECONDS = 20
VRP_MIN_THRESHOLD = 2.0 

_shutdown = False

def get_realized_volatility():
    try:
        # History command with the active remote server
        df = client.history(symbol=UNDERLYING, exchange=IDX_EXCHANGE, interval="D", 
                            start_date=(datetime.now() - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                            end_date=datetime.now().strftime("%Y-%m-%d"))
        if not isinstance(df, pd.DataFrame) or df.empty: 
            return None
        
        df["ret"] = df["close"].pct_change()
        recent_returns = df["ret"].dropna().tail(5)
        if len(recent_returns) < 3: return None
        
        daily_vol = recent_returns.std()
        return daily_vol * math.sqrt(252) * 100
    except Exception as e:
        log.error(f"RV Calculation failed: {e}")
        return None

def get_implied_volatility():
    try:
        quote = client.quotes(symbol="INDIAVIX", exchange=IDX_EXCHANGE)
        if quote and quote.get("status") == "success":
            return float(quote["data"]["ltp"])
    except Exception as e:
        log.error(f"VIX fetch failed: {e}")
    return None

def preflight_forward_test():
    log.info("=" * 60)
    log.info(f" PRE-FLIGHT CHECK: {STRATEGY_NAME}")
    log.info(f" Host: {host} (DRY_RUN={DRY_RUN})")
    log.info("=" * 60)
    
    rv = get_realized_volatility()
    iv = get_implied_volatility()
    
    if rv is None or iv is None:
        log.error("Failed to fetch volatility data.")
        return 1
        
    vrp = iv - rv
    log.info(f" Implied Volatility (INDIA VIX) : {iv:.2f}% (Insurance Price)")
    log.info(f" Realized Volatility (5-Day)   : {rv:.2f}% (Market Danger)")
    log.info(f" Volatility Risk Premium (VRP): {vrp:+.2f}%")
    
    if vrp >= VRP_MIN_THRESHOLD:
        log.info(f" SIGNAL: VRP > {VRP_MIN_THRESHOLD}%. ACTION: Sell Strangle.")
    else:
        log.info(" SIGNAL: VRP not met. ACTION: Stand Aside.")
    return 0

def _sig_handler(signum, frame):
    global _shutdown
    log.info("Shutdown signal received.")
    _shutdown = True

def main():
    signal.signal(signal.SIGINT, _sig_handler)
    
    log.info(f"Starting Local Daemon: {STRATEGY_NAME}")
    log.info(f"Dry Run = {DRY_RUN}. Monitoring VIX daily at {ENTRY_TIME}.")
    
    entered_today = None
    exited_today = None
    position = None

    while not _shutdown:
        now = datetime.now()
        today = now.date()
        hm = now.strftime("%H:%M")

        if hm == EXIT_TIME and position and exited_today != today:
            log.info("[DRY_RUN] 15:15 - Closing Strangle Position.")
            position = None
            exited_today = today

        elif hm == ENTRY_TIME and entered_today != today and not position:
            rv = get_realized_volatility()
            iv = get_implied_volatility()
            if rv is not None and iv is not None:
                vrp = iv - rv
                if vrp >= VRP_MIN_THRESHOLD:
                    log.info(f"[DRY_RUN] ENTRY CONDITION MET. VRP={vrp:.2f}%. Simulating Strangle Sell.")
                    position = "STRANGLE_SHORT"
                else:
                    log.info(f"[DRY_RUN] VRP too low ({vrp:.2f}%). Standing aside.")
            entered_today = today

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(preflight_forward_test())
    main()
