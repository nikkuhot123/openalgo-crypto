# Chronological Log

An append-only record of wiki updates, backtests, and VPS operations.

---

## [2026-08-07] operations | retired four strategies
- Stopped `openalgo` service and removed four registrations from `strategy_configs.json` (11 -> 7 remaining).
- **Red Bar X-Candle**: Retired. Walk-forward thin edge (1.20) collapsed on unfitted window (1.05); live trade today lost Rs 1,777 at EOD.
- **Overnight Drift**: Retired. Sizing model requires Rs 52 Lakhs for 1 lot at 0.36 exposure; live balance cannot support it.
- **HA-EMA 34 Channel** (NIFTY/SENSEX): Retired. Backtest over 264 sessions showed negative expectancy (-5.6 index pts avg per trade, Sharpe -3.82, net Rs -68,928), making option profit structurally impossible.

## [2026-08-07] operations | contained 16G runaway service log
- Found `/opt/openalgo/log/openalgo.log` at 16 GB, filling `/` to 86%.
- Cause: systemd unit redirecting stdout/stderr with no logrotate in place.
- Action: archived tail, truncated file (freeing 16G, disk -> 51%), and installed `openalgo-logcap` hourly systemd timer for size-gated copytruncate.
- Tested: confirmed truncate works on test file.

## [2026-08-07] research | Renko PRO Index Backtest (RAW)
- Backtested NIFTY/SENSEX/BANKNIFTY/FINNIFTY under RAW flat-sizing (fixed lot sizes, no stop-based risk sizing) and option friction.
- Finding: NIFTY 30m is positive (+Rs 315,534 / +157.8%, Sharpe 0.94) but has a maximum drawdown of Rs 314,250 (157.1% of capital) which wipes out the account during the run. All other pairs lose (SENSEX 30m: -Rs 51,702, BANKNIFTY 30m: -Rs 123,230, FINNIFTY 30m: -Rs 315,187). Edge is an artifact of concentration (top 5 of 435 trades are 51% of points) and does not survive cross-symbol validation.

## [2026-08-07] research | Renko Stock Intraday Backtest (RAW)
- Backtested the Renko PRO strategy on 5 liquid stocks (RELIANCE, SBIN, HDFCBANK, ICICIBANK, TCS) using 60 days of 15m/30m data.
- Modelled flat Rs 1,00,000 position size per trade (no stop-based risk sizing) and cash friction (0.035% turnover).
- Finding: Consistently negative results (15m: -Rs 23,500 / -11.7% net, PF 0.66, maxDD 12.5%, Sharpe -3.16; 30m: -Rs 6,670 / -3.3% net, PF 0.83, maxDD 6.7%, Sharpe -1.01). Removing stop-based risk sizing confirms the core signal itself lacks a directional edge on stocks.


## [2026-08-07] research | Judas strike selection
- Replayed 4 live Judas trades across 7 strikes (OTM3 to ITM3).
- Finding: ITM decreases theta% bleed but scales up friction Rs due to premium size. ATM is worst, but no strike rescues the leak. Exit is the lever.

## [2026-08-07] bugfix | PDH/PDL quote crash
- Fixed two dead calls in `prior_levels_ema_strategy.py` (`client.quote` and `client.orderhistory`) that blocked all live entries.
- Fixed pre-market 09:10 crash loop by adding retry wait loop for exchange master.
- Added AST check `test_strategy_sdk_surface.py` to prevent future dead calls.

## [2026-08-07] instrumentation | Judas & POV premium paths
- Added throttled `PATH` logging (default 30s) to Judas and POV.
- Pre-registered gate for Judas exit change: wait for n>=15 trades before acting.
