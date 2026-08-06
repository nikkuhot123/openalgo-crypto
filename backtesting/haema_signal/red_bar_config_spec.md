# Red Bar / X-Candle — Final Configuration Spec & Evidence

Date: 2026-08-06 · Author: research harness (backtesting/haema_signal)
Verdict: **NOT DEPLOYABLE with real money.** The delta-model edge does not
survive real option premiums. Read the premium evidence before considering any
live use.

---

## 1. Verdict (one paragraph)

The Red Bar / X-Candle strategy was given the full optimization pass requested:
regime gates, walk-forward parameter grid, SENSEX robustness test, CAS exit
timing analysis, and (new) a real-premium re-pricing audited against LIVE
greeks. On the spot-delta model (pts x 0.358 x 65 minus statutory 0.12% x2 +
0.41% spread — no brokerage), the IS-chosen gates + parameters yield PF 1.38
in 2023-24 and PF 1.32 in OOS 2025-26 — 4/4 positive years. **But** re-pricing
the same trades at REAL option premiums from `harvest_state.db` (1-minute
NIFTY option bars, 2026-01-28..05-27) shows the edge is erased in the
near-DTE (0-9 days) bucket — the weekly contracts the strategy actually
trades — from +7,813 (delta) to **+1,622 (real), PF 1.08**. The corrected
harness (end-of-bucket fills, audited 2026-08-06 against live Black-76 greeks)
shows the tax is real: ~287 Rs/trade mean gap vs the ~116 Rs/lot physically
expected from live theta (-13.4/day) + statutory + spread. CE carries the
real result (PF 3.93); PE bleeds (PF 0.69); SL/target exits are the bleed
(PF 0.49). A Markov regime gate (the framework you pointed to) was tested on
the same data and does NOT transfer: Bull PF 1.75 IS -> 0.99 OOS, Bear 0.81
-> 1.52. **Do not deploy.** The signal's edge does not out-earn genuine
option costs on the instruments it trades.

## 2. What WAS validated (for the record)

### 2.1 Lookahead audit — the rv5 gate was a leak
- Original diagnosis claimed a 5-day realized-volatility gate (rv5 >= 0.087)
  strongly separated winning/losing years.
- Audit 2026-08-06: `rv5` used TODAY's close, unknowable at entry time.
  Honest `rv5_prev` (5-day vol ending YESTERDAY) is flat: IS PF 1.01-1.05,
  no threshold stands out. 118/694 trades flip gate status. **Discarded.**
- Lesson re-confirmed: no gate computed with today's close.

### 2.2 IS-chosen honest gates (survive OOS)
Chosen on IS 2023-24 only, reported on OOS 2025-26 once:

| Gate | Rule | IS PF | OOS PF |
|---|---|---|---|
| skip Tuesday | dayofweek != 1 | 1.15 | 1.20 |
| skip strong-uptrend | mom5_prev < 0.0137 (IS 75th pct of 5-day momentum ending yesterday) | 1.35 | 1.28 |
| combined | both | 1.35 (222 tr) | 1.28 (197 tr) |

Combined per year: 2023 +5,694 PF 1.19 | 2024 +23,082 PF 1.44 | 2025 +8,783
PF 1.16 | 2026 +15,782 PF 1.49. Total +53,342 over 419 trades at 1 lot, net of
statutory + spread.

### 2.3 Parameter grid (walk-forward, honest gates, 180 combos)
- Grid: EXIT_TIME {14:15..15:10} x MAX_HOLD {60,90,120,180} x RR {2,3,4} x
  MAX_SL_PCT {0.40,0.60,0.80}. Selected on IS net, OOS reported once.
- All 180 combos have positive OOS net; 67/180 have IS PF >= 1.30 — a plateau,
  not a spike.
- CAS-safe pick (EXIT_TIME 15:10 = last pre-freeze bar): **hold 90, RR 3.0,
  max_sl 0.80** → IS PF 1.38 (+31,898) / OOS PF 1.32 (+27,725). Current live
  max_sl is 0.60 (IS 1.35 / OOS 1.28) — 0.80 is a small, real improvement.
- Exit timing: 15:10 vs 14:45 differs by ~1k IS (noise); 15:00 and 15:10 are
  the same 30m bar (bar anchored 09:15). CAS constraint satisfied at 15:10.

### 2.4 SENSEX — structurally negative, rejected
- 30m: signal negative BEFORE costs (CE -13.3 pts/trade vs 7.7 pts breakeven).
  Honest gates make it worse (PF 0.74). 45m/60m marginally positive on 37-48
  trades (PF 1.24/1.10) but that is one 3.5-month window, 2026 only, no IS/OOS
  split possible — indistinguishable from noise. Not deployable; NIFTY only.

### 2.5 CAS exit timing
- Spot freezes 15:15, auction print ~15:28-29; broker history API returns
  nothing after 15:29. EXIT_TIME must be <= 15:10 (bar closes 15:10 are the
  last pre-freeze reference). Verified: 15:00 vs 15:10 are the same resampled
  bar, so no information is lost by exiting at 15:10.
- Expanding to 15:40 (F&O close) would require options-LTP exit logic, which
  this spot-based backtest cannot model, plus severe 0DTE theta — rejected.

## 3. The killing evidence — real premium re-pricing

Script: `redbar_premium.py`. Data: `harvest_state.db` `options_bars`
(1-minute, underlying=NIFTY, 2026-01-28..2026-05-27, ~3M readable rows;
options_bars is corrupt past rowid ~7.13M, only these dates survived).
Method: same trades, same timestamps, same direction; premium = real 1-min
ATM close at entry and at exit (EOD at 15:10 wall-clock, CAS-correct;
max-hold at bar close; SL/target at the END of the exit 30m bucket — the
level has crossed by then, unlike the bucket's first minute which precedes
the fill). Costs: statutory 0.12% x2 + 0.41% spread on REAL premium
(no brokerage — confirmed). Audited 2026-08-06 against LIVE greeks from the
VPS (`optiongreeks`, NIFTY 11-Aug weekly, 5.13 DTE): theta -13.4/day,
IV ~11.8%, delta 0.52 ATM, live spread 0.24-0.29%.

| Bucket | T | delta net | REAL net | REAL PF | delta PF |
|---|---|---|---|---|---|
| ALL (mixed DTE) | 33 | +10,713 | +1,245 | 1.04 | 1.70 |
| CE | 14 | +8,499 | +10,447 | 3.93 | 3.57 |
| PE | 19 | +2,214 | -9,202 | 0.69 | 1.18 |
| EOD/max-hold | 16 | +11,173 | +14,510 | 3.10 | 5.13 |
| SL/target | 17 | -460 | -13,265 | 0.49 | 0.96 |
| **DTE 0-9 (near weekly — what live trades)** | **19** | **+7,813** | **+1,622** | **1.08** | ~1.2 |
| DTE 10-20 | 7 | +5,404 | +7,707 | 11.81 | — |
| DTE 21-98 (far dated — NOT traded live) | 7 | -2,504 | -8,084 | 0.38 | — |

Read carefully:
- The harvest only kept expiries >= 2026-04-07, so Jan-Mar trades were priced
  against far-dated contracts (DTE 21-98). Those rows are NOT the live
  instrument; the DTE 0-9 bucket (19 trades) is the honest one: delta +7,813
  vs real +1,622, PF 1.08 — flat, edge erased.
- Earlier harness run priced SL/target at the exit bucket's FIRST minute
  (before the level crossed) — that made both wins and losses look smaller
  (CE target exits showed premium DOWN on wins; a PE SL showed zero move on a
  76-pt adverse day). End-of-bucket fills fix this; live-greeks physics
  (~116 Rs/lot per 90-min hold: 55 theta + 22 statutory + ~39 spread)
  brackets the measured mean gap (287 Rs/trade incl. gamma/skew).
- PE's real result (0.69) is the death blow: delta model's 0.358 says PE is
  fine, real premiums say it bleeds. CE's convexity pays (3.93 real beats
  3.57 delta) but the aggregate is flat.
- 18/51 window trades were unpriced (no harvest bars for those dates — Jan/Feb
  coverage gap); their delta net was +1,947, i.e., the priced 33 are
  representative and slightly conservative.

### 3b. Markov regime gate (the framework you pointed to) — REJECTED
Applied the installed markov-hedge-fund-method skill (20-day rolling-return
labels, +/-2% threshold) to NIFTY daily closes 2023-04..2026-05, labels
shifted one day (no lookahead). NIFTY is genuinely regime-sticky (persistence
diagonal 85.6% Bear / 84.8% Sideways / 85.8% Bull; stationary mix 17% / 48%
/ 35%). But regime-conditioned Red Bar PF INVERTS out of sample:

| Regime | IS 2023-24 | OOS 2025-26 |
|---|---|---|
| Bear | 0.81 (n=32, -2,816) | 1.52 (n=63, +15,339) |
| Sideways | 1.29 (n=100, +11,967) | 1.23 (n=96, +9,326) |
| Bull | 1.75 (n=90, +19,624) | 0.99 (n=38, -99) |

Bull is the IS driver and flat OOS; Bear inverts to the best OOS bucket. Same
small-sample inversion as every other regime filter this session. The only
transferable gate remains mom5_prev < 0.0137 + skip-Tue (IS 1.35 / OOS 1.28).

## 4. If a re-test is ever wanted (configuration to use)

Env config for `red_bar_x_candle_strategy.py`, NIFTY only:

| Env | Value | Note |
|---|---|---|
| UNDERLYING | NIFTY | SENSEX structurally negative |
| EXIT_TIME | 15:10 | CAS: last pre-freeze bar |
| MAX_HOLD_MINUTES | 90 | theta protection; hold=180 gains are delta-model-only |
| RR | 3.0 | grid plateau center |
| MAX_SL_PCT | 0.80 | beats 0.60 both IS and OOS |
| SKIP_TUESDAY | true | IS-chosen, OOS-confirmed |
| MOM5_PREV_MAX | 0.0137 | skip strong-uptrend days (5-day mom ending yesterday) |

Both gates are computed from daily closes ENDING YESTERDAY — implementable
with the existing `fetch_daily_context` daily history call. No other parameter
(timing, RR, hold, SL cap) adds edge beyond noise.

Deploy gate: only after the Greeks Collector shows real 0DTE theta on
comparable holds is small enough that PF 0.97 becomes PF >= 1.15. Evidence so
far says the opposite.

## 5. Artifacts

- `redbar_features.py` — feature-augmented trade log (leak-aware: shifts daily
  features by one session)
- `redbar_grid.py` / `redbar_grid_results.csv` — 180-combo walk-forward grid
  with honest gates
- `redbar_premium.py` / `redbar_premium_trades.csv` — real-premium re-pricing
- `redbar_trades_features_{NIFTY,SENSEX}.csv` — per-trade features
- `redbar_backtest.py` — faithful 1-lot harness (now records entry/exit ts)
- `redbar_trail_backtest.py` — 2-lot study (rejected, kept for the record)
