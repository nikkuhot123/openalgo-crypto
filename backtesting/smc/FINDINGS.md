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
