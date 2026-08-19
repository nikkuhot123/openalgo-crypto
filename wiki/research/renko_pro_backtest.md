# Renko PRO Backtest

A study of the TradingView indicator `Doctor_Diven_Smart_Renko_Engine_Pro_Combined.pine` ported to Python and backtested over 3.3 years of NIFTY history.

---

## 1. The Strategy

The strategy is a path-dependent, intrabar execution model:
- **Renko Base**: Sequential Renko bricks calculated as close * 0.66% for index assets.
- **Structural Confluence**: Red bar triggers (close < open) are qualified only if they touch a CPR, gap, institutional, afternoon, or X-candle level within 8 index points.
- **Target Mode**: T1 books 50% at 1.5R. T2 exits at the Renko ceiling/floor structure.
- **Filters**: Entry is blocked inside the X-candle buffer band (44%-56%) and the CPR zone.

---

## 2. NIFTY Results (RAW Flat-Sizing)

Backtested over NIFTY 5-minute bars from **2023-04-05 to 2026-05-27** (57,005 bars), reported on a **Rs 2,00,000 Notional** capital:

| Timeframe | n | Win Rate | Profit Factor | Net P&L (Rs) | Max DD (Rs) | Sharpe (Trade) | Size |
|---|---|---|---|---|---|---|---|
| 5m | 1407 | 41.4% | 1.09 | **-Rs 264,678** | Rs 417,905 (209.0%) | -0.56 | 14 lots |
| 15m | 746 | 43.0% | 1.15 | **+Rs 185,163** | Rs 374,082 (187.0%) | +0.42 | 14 lots |
| 30m | 435 | 48.0% | 1.25 | **+Rs 315,534** | Rs 314,250 (157.1%) | +0.94 | 14 lots |

*Note: 15m out-of-sample (OOS) was negative (-Rs 97,343, Sharpe -0.30), showing the edge decayed. 30m OOS was positive (+Rs 230,557, Sharpe 0.91).*

---

## 3. SENSEX Results (RAW Flat-Sizing)

Backtested over SENSEX 5-minute bars from **2026-01-30 to 2026-05-27** (5,532 bars), reported on a **Rs 2,00,000 Notional** capital:

| Timeframe | n | Win Rate | Profit Factor | Net P&L (Rs) | Max DD (Rs) | Sharpe (Trade) | Size |
|---|---|---|---|---|---|---|---|
| 5m | 90 | 43.3% | 1.51 | **+Rs 200,837** | Rs 86,106 (43.1%) | +1.18 | 14 lots |
| 15m | 47 | 42.6% | 1.05 | **-Rs 9,766** | Rs 192,027 (96.0%) | -0.06 | 14 lots |
| 30m | 31 | 48.4% | 0.89 | **-Rs 51,702** | Rs 91,222 (45.6%) | -0.46 | 14 lots |

*Note: SENSEX 5m OOS was negative (-Rs 26,536, Sharpe -0.28), showing the edge was concentrated in the first half.*

---

## 4. Findings & Verdict

Although NIFTY 30m shows a net profit (+Rs 315,534), it is rejected for live deployment due to four fatal flaws:

1. **The Drawdown Wall**: At NIFTY 30m, the maximum drawdown was **Rs 314,250 (157.1% of capital)**. A single drawdown cycle would have completely wiped out the account 1.5 times over.
2. **Cross-Symbol Failure**: Running the same 30m config on other indices generated consistent losses:
   - **SENSEX 30m**: −Rs 51,702 (maxDD 45.6%, Sharpe −0.46)
   - **BANKNIFTY 30m**: −Rs 123,230 (maxDD 226.3%, Sharpe −0.32)
   - **FINNIFTY 30m**: −Rs 315,187 (maxDD 261.5%, Sharpe −0.88)
3. **Severe Concentration**: The top 5 trades out of 435 accounted for **51% of all points** (+917 of +1,782). Without those 5 outliers, the average return drops to **+2.01 points** — scraping the 1.9-point breakeven hurdle.
4. **Directional Bias**: Long trades averaged only **+1.1 points** (losing money). The entire net profit came from the short side (avg +7.6 points).

**Verdict: No edge.** The index-point edge is a statistical artifact driven by a tiny handful of outlier trades on one side of the book, and it does not survive the option friction hurdle.

*Script: `backtesting/renko_engine/renko_engine_backtest.py`*

---

## 5. Parameter Sweep Under A Pre-Registered Protocol (2026-08-19)

Sections 2-4 tested the Pine's **shipped defaults**. The remaining question was
tuning: does *any* parameter set carry a real edge? Because sweeping until
something looks good is how the previous four price-pattern studies in this repo
manufactured headlines that died on contact, the protocol was fixed in the
script **before any result was inspected** (`backtesting/renko_engine/renko_sweep.py`).

**Selection.** 576 configs x 3 timeframes = **1,728 runs**, NIFTY only, on the
**first 60% of sessions**. Grid: brick 0.33/0.50/0.66/1.00%, tolerance 4/8/16 pts,
room 1.0/1.5/2.0/3.0R, T1 1.0/1.5/2.5R, EMA filter on/off, confluence required
on/off. Ranked by in-sample net points, minimum 60 IS trades. Exactly one winner
carried forward.

### The first result is the sweep itself

**1,708 of 1,726 qualifying configs (99.0%) were profitable in-sample.** Pure
noise produces ~50%. In-sample profitability therefore carries almost no
information here, and "the best tuning" is close to meaningless as a selection
signal.

Winner: **15m, brick 1.00%, tolerance 16, room 2.0R, T1 2.5R, EMA filter OFF,
confluence NOT required** — IS n=598, win 39.3%, PF 1.42, +4,412 pts, Sharpe 3.02.

Note what tuning chose: it switched **off both of the engine's signature gates**.
The best version of the Dr Devendra Renko engine is the one that ignores
structural confluence and the trend cloud entirely.

### The five pre-registered gates

| Gate | Test | Result | |
|---|---|---|---|
| G1 | NIFTY OOS (last 40%) net > 0 | n=398, win 36.7%, PF 1.09, **+755 pts**, Sharpe 0.63 | PASS |
| G2 | >=2 of 4 other indices net > 0 | MIDCPNIFTY +4,964, FINNIFTY +776; BANKNIFTY -480, SENSEX -283 | PASS (bare 2/4) |
| G3 | Beat 200 random-entry permutations, z > 2 on net AND Sharpe | **z(pts) +1.78, z(Sharpe) +1.10; 7/200 nulls beat it outright** | **FAIL** |
| G4 | Net Rs > 0 after delta + statutory + spread | +Rs 76,705 on 1 lot | PASS |
| G5 | Net > 0 after deleting top 5 trades | +3,866 pts | PASS |

**Verdict: NO EDGE.**

### Why G3 is the one that matters

The null randomises **entry timing only** — same session, same side, same EMA
side, same per-day count — and runs the *identical* exit engine via an
`entry_override` hook, so no reimplementation can flatter either arm. The
baseline was regression-checked first: 435/746/1407 trades and identical net
points on 30m/15m/5m after the refactor.

Random entries earn **+3,040 points on average**. The red-bar trigger earns
+5,167. The gap is 1.78 standard deviations of the null — not significant, and
7 of 200 random seeds beat the real trigger outright.

This independently reproduces the Pine author's own header note
(z(sharpe) = +0.14, z(win) = +0.77, 2 of 4 null seeds beating it). Their
conclusion was to delete the strategy layer. This sweep says they were right,
and that tuning does not rescue it.

### Where the money actually was

| Exit | n | Net pts | Mean |
|---|---|---|---|
| EOD | 398 | +18,072 | +45.41 |
| SL | 564 | -17,913 | -31.76 |
| T2 | 34 | +5,008 | +147.31 |

EOD and stops nearly cancel (**+159 points across 962 trades**). **34 T2 hits —
3.4% of trades — carry 97% of the net.**

| Book | n | Avg pts/trade | vs 1.88 breakeven | Net Rs (1 lot) |
|---|---|---|---|---|
| All trades | 996 | +5.19 | clears | +Rs 76,705 |
| Excluding T2 exits | 962 | **+0.17** | fails | **-Rs 38,355** |
| Excluding top 5% | 946 | **-3.58** | fails | negative |

G5 passed only because "top 5 trades" was calibrated on the earlier 435-trade
study (1.1% of the sample); on 996 trades it is 0.5% and tests a quarter as
much. A scale-invariant gate was added **post-hoc and labelled as such** (G6,
top 5% measured against the friction hurdle rather than zero): **-3.58 pts/trade,
FAIL**. It did not decide the verdict — G3 had already failed.

### Conclusion

Five price-pattern methods have now been tested in this repo (Red Bar, Renko PRO
defaults, HA-EMA, Stochastic, Renko PRO tuned) and all five died the same way: a
thin in-sample signal that does not survive option friction, cross-symbol
validation, or a randomised-entry null. The tuned Renko engine adds one sharper
lesson — with 99% of configs profitable in-sample, this family of backtest is
not capable of distinguishing signal from drift, and the only test that
discriminated was the permutation null.

*Scripts: `backtesting/renko_engine/renko_sweep.py`, `backtesting/renko_engine/renko_engine_backtest.py`*
