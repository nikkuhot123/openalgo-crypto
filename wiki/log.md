# Chronological Log

An append-only record of wiki updates, backtests, and VPS operations.

---

## [2026-08-19] bugfix | Judas monitored a ghost position for 18 minutes

- LIVE. A manual square-off closed `NIFTY25AUG2624050CE` at 11:58 (+Rs 520).
  Judas was still logging `Monitoring Trade` at 12:16.
- Root cause: `live_position_qty()` appeared **nowhere** except inside
  `if exit_triggered`. Judas is otherwise a pure spot watcher, so a close it
  did not initiate was invisible. POV had `sync_positions_with_book`; Judas had
  no equivalent.
- Consequences: symbol lock held, session blocked, and the +Rs 520 never
  reached the books or the circuit breakers.
- Not a money risk: every SELL path (exit + shutdown) already verifies the
  broker holds the position. Verified live -- SIGTERM logged
  `broker reports ... qty=0 - already flat, no SELL` and placed no order.
- Fix 1: `detect_external_close()` on positive evidence only -- entry
  rejected/cancelled -> never a position; broker flat + entry complete ->
  externally closed; flat + entry undetermined -> 3 consecutive misses;
  positionbook unverifiable -> no decision (the 2026-08-14 lesson).
  Throttled to RECON_SECS=30 (in-trade poll is 5s).
- Fix 2: `find_external_exit_price()` reads the closing SELL from the broker's
  tradebook, then orderbook. Tracking closes even when unpriced -- a permanent
  ghost is worse than an unpriced trade.
- Fix 3 (latent, made reachable by Fix 1): `state = "DONE"` was memory-only, so
  a mid-session restart came up IDLE and could open a SECOND trade on a day
  Judas had already traded. Added `persist_done()`/`load_done_date()`; boot
  reads the marker before `persist_trade({})` can erase it.
- Verified against the real ghost on the live broker: exit 167.75 reconstructs
  gross Rs +520.00 exactly (matching the broker), net Rs +468.29 after cost.
- End-to-end on the VPS: booted the real strategy with today's marker ->
  `Already traded today (2026-08-19) - standing down`, zero orders placed.
- 31 new tests, 171 pass. Deployed judas 418e4461 (md5 verified both dirs).

## [2026-08-19] gate | Renko PRO on REAL option premiums -- rejected

- Ran the exit-tuned config on Volrix real ATM premiums, 2026-02-20..2026-08-19,
  15m, Rs 2L, slippage 0.25% (measured live) + transaction costs.
- NIFTY weekly: n=160 win 29.4% **PF 0.80 net -Rs 48,533** maxDD -88,502.
  Raw (no slip/cost) already -Rs 38,600 -- the gross book loses.
- NIFTY skip DTE-0: -Rs 31,037, maxDD -62,305, **PF still 0.80**. Expiry-day
  theta is ~1/3 of the damage, not the cause.
- BANKNIFTY monthly: PF 0.80, -Rs 44,870.
- SENSEX weekly: **PF 1.10, +Rs 18,483**, Sharpe 0.74 -- the only positive, but
  net/maxDD = 0.36 (25% DD to earn 9%); at 0.5% slip it is 0.21. Not deployable.
- Delta model was badly optimistic: index points said NIFTY +Rs 65k over 3y with
  entry beating the strong null at z=2.53. Example of the gap:
  NIFTY02MAR2624600PE bought 50.20, exited 9.20 (-82%) on expiry day.
- **Index question answered, and my section-6b ranking corrected**: BANKNIFTY,
  FINNIFTY and MIDCPNIFTY have NO weekly options (chain on 2026-03-04 shows
  BANKNIFTY nearest = monthly 2026-03-30). MIDCPNIFTY, ranked "best" on index
  points, cannot run this strategy at all. Only NIFTY and SENSEX can; of those
  SENSEX wins, agreeing with the scale-free hurdle ranking (5.55x vs 2.73x).
- Two of my own port bugs recorded: (1) I misread zero trades -- the Volrix
  trades payload is double-nested; (2) managing exits on 1-min spot in
  minTrigger (more precise than the 15m offline engine) produced ZERO exits,
  carrying positions to run end and blocking entries -- 1 entry per 10 sessions.
  Fixed by managing on the 15m bar in onCandleClose, which is also faithful.
- Six price-pattern methods now tested, six rejected. POV (OI/positioning, not
  price geometry) remains the only positive-expectancy live strategy.

## [2026-08-19] correction | Renko PRO -- the entry was fine, the exit was not

- Reverses the same-day "no edge" call. Two defects, both flagged by the user:
  the port hardcoded the Pine's shipped exits (stop = prev candle, T2 = Renko --
  the Pine's own worst target at 5.8% fill), and the null randomised entry
  TIMING only while inheriting the strategy's day, direction and EMA side.
- Symptom I had misread: 34 of 996 T2s filled yet carried 97% of net points.
  That is an unreachable target, not a bad entry.
- Swept the EXIT surface with entries frozen at engine defaults: 4 SL types x
  7 target modes x T1 x 4 trail modes x max-trades/day x cooldown = 12,096 runs.
- Trade count priced in per user request: selection on net RUPEES after friction
  (Rs 43.72/round trip = 1.87 pts). Points ranking gives 58.6% of configs
  positive; rupees gives 30.9%. Friction flips a quarter of the grid.
- Winner 15m: SL prev candle, T1 2.5R books 50%, T2 resolves to 3.0R, no trail,
  max 2 trades/day. Top 12 cluster on the same geometry.
- G1 OOS +Rs 17,236 PASS | G2 transfer 4/4 PASS | G3b STRONG null (random day +
  direction + timing) real +Rs 65,371 vs null mean -Rs 13,631, z=+2.53, 1/200
  beat it, PASS | G4 friction PASS | G5 top-5% FAIL.
- G5 is the wrong test for a 39.7%-win 2.5R book (skew +1.11): trimming the top
  5% of any right-skewed payoff looks fatal. Fair replacements: bootstrap 95.9%
  of 5,000 resamples profitable with 5th pct +Rs 2,959; 9/13 quarters positive;
  equal-trimmed real -2.90 vs null -6.05 pts/trade, z=+1.80 (better, not
  significant).
- Revised verdict: thin real borderline edge. Forward-test 1 lot, do not scale.
  Worst quarter (2026Q2, -Rs 12,269) is also the most recent.
- REQUIRED before deployment: real option premiums on Volrix. Everything here is
  delta-translated index points, which is exactly where Red Bar and Stochastic died.

## [2026-08-19] research | Renko PRO parameter sweep -- pre-registered protocol

- Swept 1,728 configs (576 params x 5/15/30m) on NIFTY in-sample only, with the
  selection rule and five validation gates fixed in the script beforehand.
- **99.0% of configs were profitable in-sample** (1,708/1,726). Noise gives ~50%,
  so IS ranking carries almost no information for this strategy family.
- Winner (15m, brick 1.00%, T1 2.5R) switched OFF both signature gates --
  confluence and the trend cloud.
- Passed G1 OOS (+755 pts), G2 transfer (bare 2/4), G4 friction (+Rs 76,705),
  G5 top-5. **Failed G3, the random-entry null**: nulls average +3,040 pts vs
  +5,167 real, z(pts) +1.78 / z(Sharpe) +1.10, and 7/200 seeds beat it outright.
- Added `entry_override` to the faithful port so the null reuses the IDENTICAL
  exit engine; regression-checked that 30m/15m/5m reproduce 435/746/1407 trades
  and identical net points.
- Money source: EOD +18,072 and SL -17,913 nearly cancel; 34 T2 hits (3.4%)
  carry 97% of net. Excluding T2: +0.17 pts/trade against a 1.88-pt friction
  breakeven = -Rs 38,355.
- G5 was mis-specified (top-5 = 0.5% of 996 trades vs 1.1% of the earlier 435).
  Post-hoc G6 (top 5% vs friction hurdle) fails at -3.58 pts/trade; recorded as
  post-hoc since G3 had already decided it.
- Verdict: no edge. Fifth price-pattern method to die the same way.

## [2026-08-16] bugfix | RECONCILE cancelled stops on two LIVE positions (14-Aug)
- 14-Aug ran LIVE. All three prior fixes held: lot sizes correct (65/20, zero rejections vs 51 on 12-Aug), greeks collector 1,356 option rows (was 0), TAPE quadrant tagging live on 7 entries.
- Recorded P&L +Rs 1,661 (Judas 24300CE +2,507 target; POV SENSEX 78000CE +352 target; POV 24450CE -714 max-hold; POV 24400CE -483 SL).
- BUT: POV opened 3 SENSEX legs at 12:49 and RECONCILE pruned all three, cancelling their stops. Only one was correct:
    77800CE entry REJECTED          -> prune correct
    78100CE entry COMPLETE @ 333.05 -> LIVE position, stop cancelled
    77900CE entry COMPLETE @ 433.95 -> LIVE position, stop cancelled (stop 420.1, leg trading 426-436 at prune time)
- Both ran unprotected to broker MIS auto-squareoff. P&L never reached the books or the circuit breakers. Exits unrecoverable (broker serves current session only; those thin strikes have no candle history).
- Second occurrence of this failure mode; July lost 75-80% on three legs the same way via a different trigger.
- FIX: sync_positions_with_book now requires POSITIVE evidence. entry rejected/cancelled -> prune. SL complete -> prune. entry complete + SL live -> DISCREPANCY, keep position AND keep stop armed, log error. Undetermined -> RECON_MISS_LIMIT (3) consecutive misses before touching a stop. entry_orderid now stored on the position so the check is possible at all.
- Judas and PDH/PDL checked: neither cancels a stop on a passive book miss. POV only.
- 9 new tests reproducing the exact 14-Aug scenario; 132 pass. Deployed pov eb149dbf.
- FOLLOW-UP FIX (same day): POV treated order ACCEPTANCE as a fill. 77800CE was accepted then rejected by the exchange; fetch_fill_price() returns None for both "rejected" and "unreadable", so the caller fell back to the pre-trade quote, armed a stop and logged "Trade entered ... Opt entry: 499.45" for a position that never existed. New confirm_entry_fill() returns complete/dead/unknown: dead aborts the entry with no stop and no position; unknown is deliberately treated as LIVE (an untracked real fill is worse than a phantom, and RECONCILE now settles it). 8 more tests, 140 pass. Deployed pov c073b360.

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
