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

---

## 6. Correction -- The Entry Was Not The Problem. The Exit Was. (2026-08-19)

Section 5 concluded "no edge" and attributed the failure to the entry trigger.
**That conclusion was wrong, and the way it was wrong is instructive.**

Two defects in section 5, both raised by the user:

1. **The port hardcoded the two exits the Pine happens to ship** -- stop at the
   previous candle, T2 at the Renko structure. The Pine's *own* ranking calls
   that target the worst of the six it offers (T2 fill 5.8%, expectancy -0.41R)
   and ATR the best (23.9%). Section 5 swept the ENTRY while leaving the worst
   exit in place, so it measured the wrong half. The symptom was visible and I
   misread it: only 34 of 996 T2s ever filled, yet those 34 carried 97% of net
   points. That is an unreachable target, not a bad entry.
2. **The null was too weak.** It randomised entry TIMING but inherited the
   strategy's own day, direction and EMA side, so it could only ever test bar
   precision -- never whether the entry picks the right day or the right side.

### What was done instead

Entries **frozen** at the engine's own defaults (confluence ON, EMA filter ON,
X/gap filters ON, brick 0.66%). The whole exit surface swept instead: 4 stop
types x 7 target modes x T1 on/off x 4 trailing modes x max-trades-per-day x
cooldown = **4,032 configs x 3 timeframes = 12,096 runs**.

Trade count is priced in rather than free: selection is on **net RUPEES after
measured option friction** (delta 0.358, statutory 0.12% x2, spread 0.41%
= Rs 43.72 per round trip = a 1.87-point hurdle EVERY trade). Ranking by index
points, as section 5 did, treats a 1,000-trade book and a 200-trade book as
equivalent. They are not:

| Ranking metric | Configs positive in-sample |
|---|---|
| Index points | 7,085 / 12,096 (58.6%) |
| **Rupees after friction** | **3,736 / 12,096 (30.9%)** |

Friction alone flips a quarter of the grid.

### Winner

`15m | SL previous candle | T2 fixed 2.0R | T1 ON at 2.5R books 50% | no trail |
max 2 trades/day | no cooldown`

The T1/T2 labels understate the geometry: with T1 at 2.5R and T2 nominally 2.0R,
the "T2 strictly beyond T1" constraint resolves T2 to **3.0R**. So the shape is:
book half at 2.5R, remainder at 3.0R, stop at the prior candle, two trades a day
maximum. The top 12 configs cluster on this same geometry rather than scattering
across isolated corners -- a robustness signal, not a lone spike.

### Gates

| Gate | Result | |
|---|---|---|
| G1 | NIFTY OOS: n=344, PF 1.20, +4.18 pts/trade, **+Rs 17,236** | PASS |
| G2 | Transfer: SENSEX +11,575, BANKNIFTY +7,388, FINNIFTY +33,008, MIDCPNIFTY +116,634 -- **4/4** | PASS |
| G3b | **Strong null** (random day + direction + timing): real +Rs 65,371 vs null mean **-Rs 13,631**, **z = +2.53**, only **1/200** nulls beat it | PASS |
| G4 | Friction: +Rs 65,371 net | PASS |
| G5 | Concentration: trimming top 5% leaves -2.90 pts/trade | FAIL |

**The entry system beats random day+direction+timing selection.** That is the
opposite of section 5's finding, and it is the user's claim confirmed: the entry
carries signal, and the shipped Renko target was destroying it.

### G5 is the wrong test for this payoff, and here is the evidence

A book winning 39.7% at a 2.5R target is right-skewed **by construction**
(measured skew +1.11): small frequent losses funding rare large wins. Deleting
the best 5% of any such distribution looks catastrophic whether the edge is real
or not -- the same test "fails" a genuinely profitable long-option book. It
cannot separate fragility from skew. Three tests that can:

**1. Bootstrap** (5,000 resamples with replacement) -- is it one lucky trade?

| | |
|---|---|
| Resamples profitable | **95.9%** |
| 5th percentile | **+Rs 2,959** (still positive) |
| Median / 95th | +Rs 65,393 / +Rs 127,475 |

Not one lucky trade. The 5th percentile of the resampled distribution is above
zero, which is the honest version of what G5 was reaching for.

**2. Consistency** -- **9 of 13 quarters profitable**. But the losing quarters
matter: 2024Q3 -8,127, 2025Q2 -5,948, 2025Q3 -67, and **2026Q2 -12,269 is the
worst of the whole sample and also the most recent**. That is a decay warning,
not a clean bill of health.

**3. Equal trimming** -- trim the top 5% from the real book AND from the null
books, so both arms carry the same skew and the same exit geometry:

| | Trimmed avg pts/trade |
|---|---|
| Real | **-2.90** |
| Null (60 seeds) | **-6.05** (sd 1.75) |
| | **z = +1.80** |

The real book is better than the null after equal trimming -- but at z=1.80 it
is below the z>2 bar. So the entry's advantage is directionally real and
survives the fair version of the concentration test, without reaching
significance on it.

### Verdict -- revised

**Not "no edge". A thin, real, borderline edge.**

Passes OOS, 4/4 cross-symbol transfer, friction, the strong entry null (z=2.53)
and the bootstrap (95.9%, positive 5th percentile). Borderline on skew-controlled
significance (z=1.80) and carrying its worst quarter most recently.

That is the same profile as Red Bar, and the same conclusion follows:
**forward-test at 1 lot, do not scale.** Max drawdown per lot and real-premium
behaviour must be measured before any size.

**Required gate before deployment: real option premiums on Volrix.** Everything
above is index points translated through a delta model (0.358). Every previous
strategy in this repo that looked acceptable in delta-translated points and was
then run on real premiums came out materially worse -- that translation is
exactly where the Red Bar and Stochastic "edges" died. Until this config is run
against real ATM weekly premiums it is not deployable, however good the index-
point arithmetic looks.

### What this correction is really about

Section 5 swept 1,728 entry configs and found 99% profitable in-sample, which
was correctly read as "in-sample ranking is uninformative here". The error was
concluding from that, plus a weak null, that the ENTRY carried nothing. With the
exit fixed and a null that actually randomises day and direction, the entry
clears the null by 2.53 sigma. **The lesson is that a strategy is entry AND exit,
and holding the shipped exit fixed while sweeping the entry can only ever produce
a verdict on the pair -- not on the half being swept.**

*Scripts: `renko_exit_sweep.py`, `renko_robustness.py`, `renko_engine_backtest.py`*
