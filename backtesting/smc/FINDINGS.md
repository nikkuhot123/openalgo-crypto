# SMC / ICT Sweep-Reversal — measured result (Volrix, NIFTY + SENSEX)

Built from 5 SKB Trading Lab infographics: Order Blocks & Smart Money Zones,
BSL/SSL liquidity, the Unicorn Model, and two on volume analysis.

## Verdict: does NOT meet the targets. Not deployed.

Targets were: return >= 4%, Sharpe >= 1.0, max DD <= 6%, trades >= 15.

Out-of-sample (VAL 2026-05-01..2026-07-27), portfolio of NIFTY + SENSEX,
1% option slippage, capital Rs 1.5L, 1 lot:

| metric | value | target | met |
|---|--:|--:|:--:|
| trades | 17 | >= 15 | YES |
| return on capital | **-2.14%** | >= 4% | NO |
| Sharpe | **-2.32** | >= 1.0 | NO |
| max drawdown | 3.19% | <= 6% | YES |
| win rate | 29.4% | — | — |
| profit factor | 0.7 | — | — |

The two targets that matter (return, Sharpe) fail. DD and trade count are only
met because the strategy trades small and rarely.

## Data limits actually measured (both matter)

| source | usable history | prices what |
|---|---|---|
| Volrix, free plan | **6 months only** (on/after 2026-01-25) | real option strikes/premium |
| Volrix `available_duration` *claims* | 2019-02-11 .. 2026-07-27 (1841 days) | — gated by plan |
| OpenAlgo broker feed | ~400 calendar days | index only, no option pricing |

Asking OpenAlgo for >6 months of 1m returns EMPTY rather than clamping, and a
whole-range request costs ~100s/probe. Probe far-back depth with a 5-day window.

## Two real bugs found (both would silently produce nothing)

1. **`.max()` / `.min()` on a Volrix data slice raises.** The docs say
   `self.dt_spot['high']` is a `numpy.ndarray`; the engine hands back a plain
   sequence, so `hi[i-k:i+k+1].max()` throws `AttributeError`. Volrix
   **catches exceptions inside hooks**, so the run completes normally with zero
   trades and no error anywhere. Use the builtin `max()`/`min()`/`sum()`.
   This one bug invalidated 6 earlier probe runs.
2. **CHOCH compared against the wrong level.** I used `min(lows)`/`max(highs)`
   over the whole 40-bar window: that demands a break of the *deepest* low in
   the window. A change of character breaks the *most recent* swing. Fixed to
   the latest pivot.

Diagnosis method that found #1: an analysis-mode funnel logging points BEFORE
and AFTER the suspect block. 128 points before, 0 after => the block throws.

## Funnel measurements (NIFTY, 64 train days, 5m)

    pivot days logged        64
    same-candle sweeps      937
    multi-candle sweeps    1478
    bars visible/day        151      (spot, futures and 15m HTF all resolve)
    futures volume/bar   611930      (spot has NO volume; futures does)

## What the tuning actually showed (TRAIN 2026-01-26..2026-04-30, NIFTY)

RR floor is monotone in profit factor — a real selection effect, not a peak:

| MIN_RR | trades | return | PF |
|--:|--:|--:|--:|
| none | 37 | -8.64% | 0.4 |
| 1.0 | 11 | -1.11% | 0.8 |
| 1.2 | 9 | +1.54% | 1.6 |
| 1.5 | 5 | +1.10% | 1.8 |

Every confluence gate raised per-trade quality and destroyed the sample:

| gate | trades | return | PF |
|---|--:|--:|--:|
| none | 9 | +1.54% | 1.6 |
| + volume (futures) | 4 | +3.11% | 4.0 |
| + 15m HTF bias | 2 | +1.49% | — |
| + Unicorn OB/FVG overlap | 1 | +1.13% | — |

Hypotheses that measurement REJECTED:
- **Multi-candle sweep window** (my idea, to add setups): PF 1.6 -> 0.6. Using
  the window extreme as the stop widens risk, worsening RR and enlarging losses.
  Reverted; the same-candle rejection keeps the stop tight.
- **MAX_ENTRIES 2 -> 3**: identical results. The cap was never binding.
- **Loosening to reach 15 trades** (wider window, SETUP_LIFE 15, DISP_ATR 0.5):
  14 trades but PF 1.6 -> 0.9. The marginal setups are noise.

## Why the train numbers were never trustworthy

9 trades over 3 months. Sharpe 3.63 on 9 trades is not a measurement. The
volume gate looked best of all on train (PF 4.0 on 4 trades) and went
**0-for-5 out of sample**. That is the small-sample illusion in one line.

Full OOS breakdown:

| run | trades | return | win | PF |
|---|--:|--:|--:|--:|
| NIFTY volume-gated | 2 | -0.79% | 0% | 0.0 |
| NIFTY bare RR1.2 | 7 | -2.66% | 14.3% | 0.2 |
| SENSEX volume-gated | 3 | -2.04% | 0% | 0.0 |
| SENSEX bare RR1.2 | 10 | +0.52% | 40% | 1.1 |

Only SENSEX-bare survived, and 10 trades at PF 1.1 is indistinguishable from
breakeven noise.

## Honest read

The concept is not disproven — it is **unmeasurable on 6 months of data**. The
gates that the infographics call the edge (volume confirmation, HTF alignment,
Unicorn overlap) are each so selective that a 6-month window yields 1-4 trades.
You cannot validate a 1-4 trade strategy, and you must not deploy one.

Two things would change the answer, in order of value:
1. **More history.** The Volrix Max plan unlocks 2019->present (1841 trading
   days vs 64). At ~0.14 trades/day the full range gives ~250 trades per
   instrument — enough to actually test the confluence gates.
2. **Lower timeframe for setup supply** (3m instead of 5m) — untested here;
   it raises setup count without loosening any stop or gate.

Sizing is NOT the blocker: at RR1.2 on train the return/DD ratio was 1.18, so
lots alone could reach 4% return inside a 6% DD. The blocker is that the edge
does not survive out of sample.

## Reports

- NIFTY bare VAL     https://app.volrix.ai/report/35256f2a-2950-4481-8203-0a4db36fb912
- NIFTY volume VAL   https://app.volrix.ai/report/86a9cb5c-fd89-474e-a584-7a4bb58eebd1
- SENSEX bare VAL    https://app.volrix.ai/report/bba98ae5-6cef-4eab-a6fb-cceeafdddf94
- SENSEX volume VAL  https://app.volrix.ai/report/6d61351d-f30f-4118-a52b-2ebefdfd99f4


---

# Part 2 — Inverting the mechanism: sell premium instead of buying it

## The reasoning

Every option-BUYING model tested (HA-EMA, Judas, the SMC debit model above) lost.
The cause is mechanical, not tuning: a bought option must clear premium decay plus
~1% slippage before direction pays, which is why the SMC debit model needed
RR>=1.2 + Unicorn gates so strict they left 1-9 trades. Selling inverts every one
of those terms - theta works for you, the level only has to HOLD rather than
break, so the signal needs no large move and no starving gates.

## The one genuinely strong finding of this whole session

Same instrument, same side of the market, same theta tailwind. The only
difference is WHERE the trade is placed:

| TRAIN (NIFTY, 2026-01-26..04-30) | trades | return | Sharpe | maxDD | PF |
|---|--:|--:|--:|--:|--:|
| S1: sell ATM straddle blindly at 09:20 | 128 | **-31.49%** | -3.16 | 34.0% | 0.8 |
| S3: sell only at swept-and-rejected liquidity | 111 | **+12.22%** | 1.30 | 13.2% | 1.2 |

A ~44 percentage-point gap attributable purely to the ICT liquidity selection.
The infographic concepts DO carry information. It is just not enough to be
profitable - see below.

## Structural findings that held up

- **Skip expiry day (DTE 0).** DD 13.2% -> 9.4%, Sharpe 1.30 -> 1.42, return
  intact. Sellers are short gamma; expiry day is where the tail lives. Same
  conclusion the HA-EMA work reached independently.
- **A protective wing makes it worse.** Sell OTM1 / buy OTM4 credit spread:
  -14.13%, PF 0.9. The wing premium costs more than the tail it insures on
  weekly options.
- **Tighter premium stop makes it worse.** SL 40% -> 30%: DD 9.4% -> 14.3%.
  More stop-outs on noise, and each stop is realised against slippage.
- Late entry (from 11:00) and an early-only window (to 12:00) both degraded.
  A "strong rejection close" filter also degraded (DD 9.4% -> 13.8%).

## Why I am NOT handing over the good-looking config

Tuning `VOL_MAX` produced this curve on TRAIN:

| VOL_MAX | trades | return | Sharpe |
|---|--:|--:|--:|
| off | 85 | +3.91% | 0.52 |
| 1.30 | 84 | -1.22% | -0.16 |
| **1.60** | 83 | **+11.38%** | **1.42** |
| 2.00 | 85 | +0.18% | 0.02 |

A **spike, not a plateau**. Trade counts differ by 1-2 out of 85 while return
swings 12 percentage points - so one or two outlier trades produce the entire
result. `VOL_MAX=1.60` would have shown +11.38% / Sharpe 1.42 / DD 9.4% and
cleared three of four targets. It is an artifact and it is not deployable.

The honest unfitted base is the `off` row: +3.91%, Sharpe 0.52, DD 9.17%.

## Out-of-sample, and then the full window

Base config = sweep-fade credit, skip DTE-0, no volume gate, 09:30-14:30.

| window | symbol | trades | return | Sharpe | maxDD | win | PF |
|---|---|--:|--:|--:|--:|--:|--:|
| TRAIN | NIFTY | 85 | +3.91% | 0.52 | 9.2% | 32.9% | 1.1 |
| VAL | NIFTY | 83 | **-14.16%** | -2.78 | 21.6% | 28.9% | 0.7 |
| VAL | SENSEX | 83 | **-19.88%** | -4.14 | 22.9% | 26.5% | 0.7 |
| FULL 6m | NIFTY | 168 | -10.24% | -0.80 | 24.2% | 31.0% | 0.9 |
| FULL 6m | SENSEX | 172 | -23.89% | -1.90 | 38.2% | 30.2% | 0.8 |
| FULL 6m | portfolio (Rs 3.0L) | **340** | **-17.07%** | -1.54 | 30.3% | 30.6% | 0.9 |

**This one is a real measurement, not a small-sample shrug.** 340 trades over the
full available window is a meaningful sample, and it says profit factor 0.9 -
losing. Unlike the debit model (1-9 trades, unmeasurable), the credit model
generates enough trades to be judged, and the judgement is negative.

## Final scoreboard vs targets

| target | best honest result | met |
|---|---|:--:|
| return >= 4% | -17.07% (full, portfolio) | NO |
| Sharpe >= 1.0 | -1.54 | NO |
| max DD <= 6% | 30.3% | NO |
| trades >= 15 | 340 | YES |

Sizing cannot fix this: the return/DD ratio must be >= 4/6 = 0.667 for any lot
size to satisfy both return and DD. The honest base ratio is 0.43 on train and
negative everywhere else.

## What I actually believe after all of this

1. **The liquidity-selection concept has measurable content** (+44pp vs blind
   selling). That is the real result and it is worth keeping.
2. **It is not sufficient for profit in this 6-month window.** Both instruments,
   both directions of expression (debit and credit), all fail out of sample.
3. **VAL is entirely post-2026-04-30** - the same regime break every earlier
   strategy family died in. Either that regime is hostile to all these
   mechanisms, or nothing here ever had an edge and the earlier window
   flattered it. Six months cannot separate those two explanations.
4. Highest-value next step remains **more history** (Volrix Max: 1,841 trading
   days vs 125). At ~1.4 trades/day the full range gives ~2,500 trades, enough
   to test a regime/volatility filter - which is the only untested lever with
   real headroom, since a seller's losses concentrate in trend/high-vol days.

Do not deploy any of this.


---

# Part 3 — The definitive test: 92,876 events, 4 instruments, 4 years

Part 1 said the concept was "unmeasurable on 6 months". That excuse is now gone.
`harvest_state.db` supplied 777 trading days x 4 indices of 5m bars (2023-04-05..
2026-05-27), validated at 98.9% exact close-parity against the live broker feed.

## First: slippage was never the cause

Volrix applies slippage post-run, so the existing backtests were re-priced at the
measured cost (median 0 bps drift) instead of the assumed 1%:

| run | @1.0% | @0.2% | @0.0% |
|---|--:|--:|--:|
| S3 base NIFTY TRAIN | +3.91% | +13.31% (Sharpe 1.79) | +15.65% |
| S3 base NIFTY VAL | -14.16% | -7.47% | **-5.79%** |
| S3 base SENSEX VAL | -19.88% | -12.65% | **-10.84%** |

**It still loses out of sample at ZERO transaction cost.** Cost changes the
magnitude, never the sign. My earlier "slippage dominates" framing was wrong for
this strategy: the signal breaks down on its own.

## Second: the infographic's core claim is TRUE

"LIQUIDITY SWEEP (breaks a pool then reverses) vs REAL BREAKOUT (breaks and
continues)" - tested as P(the swept extreme is never exceeded again before 15:15),
comparing the two groups so both took out the same kind of pool:

| group | events | P(hold) | median MFE |
|---|--:|--:|--:|
| sweep | 35,874 | **40.9%** | 55.3 pts |
| real breakout | 57,002 | 32.7% | 49.9 pts |

**Edge +8.1pp, z = 25.2.** Positive in all 4 instruments (NIFTY +8.7, MIDCPNIFTY
+8.8, BANKNIFTY +7.8, FINNIFTY +7.1) and all 4 years (+5.8, +8.4, +9.4, +8.3).
The distinction the material rests on is real and stable. That is a genuine
validation of the source material.

It also explains the credit model's PF exactly: 40.9% hold at the 1.25:1 payoff
of a 50%-target/40%-stop gives 0.409*1.25/0.591 = **0.87**, versus the 0.9
measured. The edge was real; the payoff structure was wrong.

## Third: but it does NOT translate into tradable expectancy

Median MFE of 55 pts against a ~19-pt stop implied ~2.3:1 was available, so the
actual bar path was walked for every event: enter at the sweep close, stop just
beyond the swept extreme, target at k x risk. Stop wins ties; unresolved by 15:15
exits at the close.

| target k | sweep E[R] | win% | breakout E[R] |
|---|--:|--:|--:|
| 1.0 | -0.019 | 49.0% | +0.006 |
| 1.5 | -0.025 | 41.1% | +0.005 |
| 2.0 | -0.023 | 37.5% | -0.001 |
| 2.5 | -0.013 | 36.0% | -0.005 |
| 3.0 | -0.007 | 35.2% | -0.002 |

Best case k=3.0: **E[R] = -0.0068R, se 0.0075, t = -0.9**, n=35,874. Zero.
Per trade: **-0.22 index points**. Sign flips by year (2023 -0.045, 2025 +0.037)
and by instrument, so no stable subset exists either.

**Statistical power:** se = 0.0075R detects any edge above ~0.023R at 3 sigma
(~0.7 points on 31-pt median risk). The observed value is -0.007R. This
**rules out** a tradable edge rather than failing to find one.

## Why P(hold) can be significant while E[R] is zero

P(hold) is a survival statistic measured to EOD. Expectancy depends on the PATH.
Even when a fade ultimately holds, price frequently trades against the entry
first and takes out a stop placed just beyond the extreme; when it does not hold,
you lose a full 1R. The +8.1pp survival advantage is exactly cancelled by
unfavourable path and timing. A classifier can be highly significant and still
have no tradable content - this dataset is a clean example.

## What this closes

Every result in this repo is now consistent under one explanation:

| strategy | outcome | consistent with E[R] ~ 0? |
|---|---|---|
| HA-EMA 34 channel (debit) | -10.2 pts/trade | yes: ~0 signal edge, then option cost |
| Judas swing (debit) | net negative | yes |
| SMC debit (RR gates) | 1-9 trades, OOS negative | yes |
| SMC credit (sweep-fade) | PF 0.9, predicted 0.87 | yes, quantitatively |
| Blind short straddle | -31% | yes: no selection at all |

There is no directional edge in this signal family on the index. Positive results
on 64-day windows were noise, and the 6-month data cap was never the real limit.

## Recommendation

Stop pursuing ICT/SMC directional variants. This is now a measured null with
enough power to be trusted, not an inconclusive result. Reaching return >= 4% /
Sharpe >= 1.0 / DD <= 6% requires a different SOURCE of edge, not another
parameterisation of this one.

The one mechanism that is genuinely different and now cheap to test: cross-
sectional relative value between the four indices (e.g. NIFTY vs BANKNIFTY
dislocation), since the cache holds 777 days of all four aligned on the same 5m
grid. That is a different bet - convergence rather than continuation - and it has
not been tested. Multi-day horizons are also untouched; everything here exits by
15:15.
