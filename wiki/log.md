# Chronological Log

An append-only record of wiki updates, backtests, and VPS operations.

---

## [2026-08-12] research | OpenMTOps upstream review + OI feed verification
- Reviewed CApsUNlocked123/openmtops pov_engine.py: our POV port is faithful, every constant matches (PRE 50k, C2 30k, 5/5 gate, 1.5/3/5R targets). Upstream sources OI from the candle feed, same as us.
- Verified the optionchain 404 did NOT degrade OI scoring: optionchain was only ever called in fetch_lot_size. Cross-checked history() OI against the collector's independent quote path (NIFTY25AUG26FUT, 312 minutes): mean abs diff 2,128 = 0.017% of OI, early samples exact. Feed is trustworthy.
- Scoring improved that day (5,148 polls vs 4,247) and POV hit 5/5 three times -- first STRONG signals in 3 days -- all killed by the lot-size bug, not by data.
- narrative.py (730 lines, 7 templates): descriptive not predictive, no backtest, no edge claim. Half of it needs IV which history() does not return. Verdict: do not port the narration layer; the one cheap use is annotating existing trades with the OI x price quadrant as diagnostic context for the give-back study.
- SEPARATE BUG: greeks_collector has ZERO option rows across 08-10/11/12 -- only spot and futures. The greeks columns are empty because no option leg is sampled. It is not serving its stated purpose.

## [2026-08-12] bugfix | lot-size detection sent invalid quantities in analyzer
- Symptom: trades fired but never reached sandbox positions/trades. 51 rejections: "Quantity must be in multiples of lot size 65" (NIFTY) / "... 20" (SENSEX).
- Cause: fetch_lot_size() had ONE source, client.optionchain(), which returned 404 "No strikes found ... update master contract" all session on BOTH indices despite the master holding 462 CE rows for that expiry. Failure is INTERMITTENT (same call worked 2h later).
- On failure the code fell through to a hardcoded `QUANTITY = 75` -- NIFTY's lot size before the 2025-12-31 change to 65, never correct for SENSEX (20).
- Only SENSEX orders on 08-11 reached sandbox (4, all complete); every NIFTY order died at validation.
- Fix: second independent source -- optionsymbol() returns lotsize at the top level and is the same endpoint the strategies already call every cycle to resolve their leg. Removed the 75 guess; both strategies now stand down rather than size a trade they cannot size.
- Verified live: NIFTY 65, SENSEX 20 through both sources. 14 new tests, 61 total pass. Deployed judas 4f00cd76, pov ad27ecd3.
- NOTE: red_bar / ha_ema / regime_momentum carry the same `QUANTITY = 75` fallback but are de-registered. Fix before any re-registration.

## [2026-08-10] research | Variance Risk Premium -- selling vs buying
- Researched (agent-reach/web): documented statistically significant positive VRP in Indian index options; implied variance systematically exceeds realized. All five prior strategies BOUGHT options and paid it.
- Volrix test 1, short ATM straddle 30% SL: NIFTY PF 0.90 -Rs 22,921; SENSEX PF 1.00 -Rs 16,466. Win rate 38% = stop firing on noise, mis-specified.
- Volrix test 2, iron fly (defined risk, no stop): NIFTY PF 1.00 -Rs 1,261 Sharpe -0.09 maxDD 13.2%; SENSEX PF 1.00 -Rs 3,392 maxDD 8.4%. Drawdown halved, Sharpe near zero.
- Finding: VRP edge is real and almost exactly consumed by 4-leg friction. avgWin +1,548 vs avgLoss -1,550 on NIFTY.
- Six strategies now land between PF 0.70 and 1.00. Binding constraint is friction and capital, not signal.
- Selling needs ~Rs 1.5-2L margin/lot -- not accessible at live balance regardless.

## [2026-08-10] research | Stochastic Crossover (SKB) -- OpenAlgo + Volrix
- Analysed the SKB chart: Stochastic (14,3,3), buy on %K/%D cross up from <20, sell on cross down from >80, NIFTY 15m.
- OpenAlgo engine: chart defaults lose on every timeframe (15m PF 0.88, Sharpe -3.77).
- Swept 162 NIFTY configs; only 16 profitable. All top 12 used the `range` regime filter, validating the chart's own "works best in sideways markets" caveat.
- Best NIFTY config (30m z30 range rr3.0): PF 1.21 overall but IS +Rs 383,892 vs OOS -Rs 10,106, maxDD 189.9% of capital. Key parameter inverts on SENSEX (its champions used `none`); SENSEX champion loses -Rs 535,014 on NIFTY.
- Volrix (REAL option premiums, 6-month plan limit): NIFTY n=51 PF 0.90 -Rs 6,796 Sharpe -0.86; SENSEX n=54 PF 0.70 -Rs 21,714 Sharpe -2.16.
- Verdict: do not deploy. Two engines agree.

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
