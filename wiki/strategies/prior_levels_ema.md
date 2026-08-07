# Prior Levels EMA Strategy (PDH/PDL)

The **Prior Levels EMA** strategy is an overnight carry system that trades breakout setups near the session close and carries the position to the next morning's open.

---

## 1. Configuration

- **Underlying**: NIFTY (NFO) and SENSEX (BFO).
- **Schedule**: Starts 09:10, exits/holds from 15:05, exits next morning (09:20 SENSEX / 09:30 NIFTY).
- **Size**: 1 lot.
- **Product**: `NRML` (overnight).
- **Stops**: Live premium stop placed at broker at 25% below entry premium.
- **SIGTERM**: Caught and handled; strategies do not square off at 15:20 stop time, leaving the position open at the broker for the overnight carry.

---

## 2. Bug Fixes (2026-08-07)

The strategy was deployed but failed to execute any trades, including the first scheduled overnight carry on 2026-08-07, due to three critical bugs:

### Bug A: The Quote Crash (AttributeError)
- **Problem**: `fetch_option_ltp` called `client.quote(...)` and `fetch_fill_price` called `client.orderhistory(...)`. The SDK does not have these singular methods (it uses `quotes()` and `orderstatus()`). Every call raised `AttributeError` and returned `None`.
- **Consequence**: The strategy could never price a leg, refusing all entries with `no premium for <symbol>`.
- **Fix**: Swapped to `client.quotes()` and `client.orderstatus()`, matching the other strategies. Wired `test_strategy_sdk_surface.py` to prevent future method name typos.

### Bug B: The Pre-Market Loop (sys.exit)
- **Problem**: Starting at 09:10 (before the exchange master publishes the lot size) caused `fetch_lot_size` to return 0. The code did `sys.exit(1)`, turning the start into a **7-restart crash loop** before the market opened.
- **Fix**: Added a wait loop (`LOT_SIZE_WAIT_SECS`, default 600s) that retries every 10s while remaining SIGTERM-responsive.

### Bug C: Unit Test Fakes
- **Problem**: The unit tests in `test_prior_levels_ema_runloop.py` passed because the test's fake broker defined `def quote(...)` and `def orderhistory(...)`, reproducing and validating the typo.
- **Fix**: Renamed the test fakes to match the real SDK.

---

## 3. Status

- **Live Status**: Registered, active, and fully fixed (commit `6f9fb0aef`, md5 `fc241b99`).
- **First Run**: Tomorrow morning's 09:10 start will be its first clean session. Watch for a 15:05 entry and overnight carry.
