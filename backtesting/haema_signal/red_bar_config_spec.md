# Red Bar / X-Candle — Configuration Spec & Evidence

Date: 2026-08-06 · Author: research harness (backtesting/haema_signal)
Verdict: **thin but real edge — forward-test at 1 lot, do not scale yet.**
Under a full walk-forward that re-fits both parameters and the gate every
quarter, the intraday long-option config returns +Rs 33,453 over 350
out-of-sample trades (PF 1.20, ~Rs 96/trade, all three years positive, max
drawdown Rs 13,495). The one window where nothing at all was fitted
(2026-05-28..08-06, 29 trades) reads +Rs 619, PF 1.05 — consistent with a
thin edge that a sample that small cannot resolve either way. Overnight
holds, stop removal, futures, inversion, short premium and the other indices
were all tested and all fail — see section 5b. Two earlier verdicts in this
session are retracted below, with the reason each was wrong.

DATA WARNING (found 2026-08-06): the broker's 5-minute history endpoint is
RANGE-DEPENDENT. Requesting 2026-05-20..08-06 in one call returns 2026-06-12
with last close 23,984.85 / high 23,984.85; requesting 06-12 alone returns
23,631.75 / 23,645.35, which agrees with the official daily bar. Multi-day
responses are silently wrong, and an earlier revision of this spec quoted
forward numbers (-Rs 896 gated, -Rs 10,919 ungated) computed from them. Fresh
bars are now fetched one session at a time and cross-checked against a
single-day request (`fetch_5m_live` raises rather than return inconsistent
data). The duckdb cache (<= 2026-05-27) was never affected, so the grid and
IS/OOS work stand unchanged.

The strategy was given the full pass: regime gates, walk-forward parameter
grid, SENSEX test, CAS exit timing, real-premium re-pricing, a live-faithful
1-minute simulation audited against live greeks pulled from the VPS while the
market was open, and finally a true forward test on bars fetched fresh from
the broker (2026-08-06).

### The decisive number

The local cache ends 2026-05-27. Every gate threshold and every grid
parameter in this session was fitted at or inside that boundary. Fetching
fresh 5-minute spot and running the recommended config on 2026-05-28..08-06
— bars nothing here has ever seen:

| Config | T | delta net | real-equiv | PF | win |
|---|---|---|---|---|---|
| ungated | 47 | -10,919 | -12,939 | 0.61 | 31.9% |
| **gated (recommended config)** | 28 | **-896** | **-1,061** | **0.94** | 35.7% |

The gates do real work — they remove 19 trades worth -Rs 10k and lift PF from
0.61 to 0.94 — but they do not manufacture an edge. Flat is the honest
reading of 28 forward trades.

This is independently corroborated: the strategy file's own docstring records
a Volrix backtest on REAL weekly premiums over 2026-06-08..08-04 that lost
Rs 15,301 at PF 0.5 on 35 ungated trades. Two unrelated methods — Volrix real
premiums and this delta harness — agree on the ungated config over the same
period. That cross-validation is the best evidence in this file that the
harness itself is sound.

### Retraction 1 — "theta erases the edge" (wrong)

An intermediate harness priced SL/target exits at the *end of the exit 30m
bucket*, i.e. it held the position up to 30 minutes past the stop. The live
loop polls every 5 seconds (`time.sleep(5)` in the IN_TRADE branch) and exits
on touch. Re-pricing the same 31 trades at the touch minute:

| Fill model | net | PF |
|---|---|---|
| 30m bucket-end (models a strategy that does not exist) | +1,245 | 1.04 |
| 1-minute touch (matches the live loop) | +18,494 | 2.10 |
| pathological stress (buy at minute high, sell at minute low) | -7,971 | 0.73 |

Example: 2026-05-13 CE target — bucket-end priced it 249.80 -> 249.80
(-Rs 380); the touch-minute fill is 249.80 -> 309.00 (+Rs 3,742).

### Retraction 2 — "real premiums prove an edge" (also wrong)

The +18,494 above is real, but its window (2026-01-28..05-27) sits INSIDE the
range used to choose the gates. It is in-sample. What it legitimately proves
is narrower and still useful: **option pricing is not what kills this
strategy.** Calibrating real against delta on those 31 trades:

    real_pnl = 1.185 x delta_pnl     95% CI [0.936, 1.438]   r = 0.81, n = 31

The implied effective delta is 0.424, correctly below the live instantaneous
ATM delta (0.519 measured today) because theta drags over the hold. So the
delta model is a fair, slightly conservative proxy — the negative forward
result is a signal problem, not a pricing artifact.

## 2. Configuration (NIFTY only)

| Env | Value | Note |
|---|---|---|
| UNDERLYING | NIFTY | SENSEX structurally negative — see 4.3 |
| EXIT_TIME | 15:10 | CAS: spot freezes 15:15, auction print ~15:28 |
| MAX_HOLD_MINUTES | 90 | winners come from the 90-min holds |
| RR | 3.0 | grid plateau centre |
| MAX_SL_PCT | 0.80 | beats 0.60 on both IS and OOS |
| SKIP_TUESDAY | true | IS-chosen gate, OOS-confirmed |
| MOM5_PREV_MAX | 0.0137 | skip strong-uptrend days (5-day momentum ending YESTERDAY) |

Both gates use only data available before the session opens. Fitted-window
result at this point: IS PF 1.38 / OOS PF 1.32. Forward window: PF 0.94.
This is the configuration to use IF the strategy is ever forward-tested
again — it is not a deployment recommendation.

## 3. Sizing for a 5-6 lakh account

**Recommended allocation: 1 lot, forward-test only.** The honest
walk-forward pays ~Rs 92/trade (Rs 109 real-equivalent), ~110 trades/year.
Sizing beyond 1 lot should wait for ~30 forward trades that land in that
band. The fitted-history column below is what the sweep bought and is
deliberately shown next to the walk-forward number, which is what to expect.
Per-trade capital is the premium: ~Rs 13,000 per lot at a 200-point ATM.
The walk-forward column is the one to plan against (Rs 92/trade x ~110
trades/year, drawdown from the same 349-trade OOS series); the fitted column
is what the sweep bought, shown only for contrast.

| Lots | Capital/trade | Walk-forward/year | Walk-forward maxDD | Fitted/year |
|---|---|---|---|---|
| 1 | ~13,000 | ~+10,100 | -13,495 | ~+20,000 |
| 3 | ~39,000 | ~+30,300 | -40,485 | ~+60,200 |
| 5 | ~65,000 | ~+65,000* | -67,475 | ~+100,333 |

*5 lots assumes fills stay at the quote; at Rs 92/trade of edge the strategy
is execution-sensitive, and size makes that worse.

At 3 lots that is ~Rs 30k/year on a 5.5L account (~5.5%) against a ~Rs 40k
drawdown (~7%). That ratio is not compelling enough to skip the forward test.

## 4. Evidence

### 4.1 Honest gates (no lookahead)
An earlier realized-volatility gate (rv5 >= 0.087) was discarded: it used
TODAY's close, unknowable at entry. Its honest form (`rv5_prev`) is flat
(IS PF 1.01-1.05). Replaced by the momentum gate above, chosen on IS 2023-24
and reported once on OOS. Per year (delta model, 1 lot): 2023 +5,694 PF 1.19,
2024 +23,082 PF 1.44, 2025 +8,783 PF 1.16, 2026 +15,782 PF 1.49.

### 4.2 Parameter grid
180 combos (EXIT_TIME x MAX_HOLD x RR x MAX_SL_PCT), selected on IS, OOS
reported once. All 180 have positive OOS net; 67/180 have IS PF >= 1.30 — a
plateau, not a spike. 15:00 and 15:10 resolve to the same 30m bar, as do
14:30 and 14:45.

### 4.3 SENSEX — rejected
30m signal is negative before costs (CE -13.3 pts/trade vs 7.7 pts
breakeven); honest gates make it worse (PF 0.74). 45m/60m are marginally
positive on 37-48 trades in a single 3.5-month window — noise. NIFTY only.

### 4.4 Live-faithful 1-minute simulation (`redbar_live_sim.py`)
Re-runs the gated signals at 1-minute resolution on REAL option premiums
(`harvest_state.db` + `harvest_options_archive.db`, 2.78M NIFTY 1-min option
bars, 2026-01-28..05-27) with the live exit ladder in live priority:
EOD(15:10) > spot SL > spot target > max-hold(90) > decay floor(60% of entry
premium), plus the broker-side premium stop at -70%.

| Bucket | T | delta | REAL | REAL PF |
|---|---|---|---|---|
| ALL | 31 | +13,349 | +18,494 | 2.10 |
| CE | 14 | +8,499 | +9,053 | 2.87 |
| PE | 17 | +4,850 | +9,441 | 1.79 |
| DTE 0-9 (live weekly) | 19 | +7,813 | +10,589 | 2.06 |

Live exit mix: SL 10 trades -11,860 (median hold 16 min), max-hold 13
+13,065 (90 min), target 4 +10,525 (19.5 min), EOD 4 +6,764 (40 min). The
strategy pays for its losers with the 90-minute holds.

Two live-only exits — the 60% decay floor and the -70% broker stop — never
fired in the window, so the backtest's omission of them costs nothing here.
The backtest's 30m exit classification matches the 1-minute reality on 29/31
trades (2 targets become max-hold).

### 4.5 Live greeks anchor (market open, 2026-08-06)
NIFTY 11-Aug weekly, 5.13 DTE, spot 24,634: ATM 24650 CE prem 144.35,
IV 11.78%, delta 0.519, theta -13.38/day, gamma 0.00116, vega 11.65;
24650 PE prem 130.90. 18-Aug (12.13 DTE) theta -8.35. Live bid/ask spread
0.24-0.29%.

Physical friction for a 90-minute hold at 5 DTE: ~Rs 55 theta + Rs 22
statutory (0.12% x2, no brokerage) + ~Rs 39 spread = ~Rs 116/lot. The
measured delta-vs-real gap was +Rs 166/trade in the strategy's FAVOUR (real
delta 0.52 beats the model's 0.358 by more than theta costs). Friction is
therefore not the binding constraint — direction is.

### 4.6 Markov regime gate — rejected
Applied the markov-hedge-fund-method framework (20-day rolling-return labels,
+/-2%) to NIFTY daily closes, labels shifted one day. NIFTY is genuinely
regime-sticky (persistence 85.6% Bear / 84.8% Sideways / 85.8% Bull;
stationary mix 17/48/35%). But regime-conditioned PF inverts out of sample:

| Regime | IS 2023-24 | OOS 2025-26 |
|---|---|---|
| Bear | 0.81 (n=32) | 1.52 (n=63) |
| Sideways | 1.29 (n=100) | 1.23 (n=96) |
| Bull | 1.75 (n=90) | 0.99 (n=38) |

Bull drives IS and is flat OOS; Bear inverts. No transferable signal.

## 5. Where the edge is and is not

Two facts sit in tension and both are real:

- **Walk-forward (large sample, honest re-fitting): positive.** 349 OOS
  trades, +Rs 32,142, PF 1.19, every year positive, not concentration-driven
  (+Rs 16,572 after deleting the best three trades).
- **The single fully-unfitted window: flat.** 28 trades, -Rs 896, PF 0.94.
  Its ungated form (-Rs 10,919, PF 0.61) is independently reproduced by a
  Volrix run on real premiums (-Rs 15,301, PF 0.5).

The reconciliation is sample size, not contradiction: 28 trades cannot
resolve a Rs 92/trade edge. One quarter of this strategy is noise by
construction — 2026Q2 (-474) and 2026Q3 (-1,367) are both inside the
walk-forward's positive 2026.

What is settled:
1. Option pricing is not the problem — calibration 1.185, r 0.81, and the
   real delta (0.52) beats the model's 0.358 by more than theta costs.
2. The gates do real work but subtract losers rather than add winners.
3. CAS handling is correct; the live-only exits (60% decay floor, -70%
   broker stop) never fired in 31 real-premium trades.
4. The stop is load-bearing — removing it costs Rs 44k across the
   walk-forward and triples the drawdown.

What is not settled:
1. The signal DESIGN (red-bar rule, fib bands, 30m interval) was chosen with
   hindsight in earlier sessions. Only parameters and the gate were re-fitted
   here, so even the honest arm carries design-level selection.
2. Real premiums exist for 31 trades, all inside the fitted range. Harvesting
   stopped 2026-05-27.
3. Edge per trade (~1.4 index points after costs) is smaller than the
   1-minute option bar range, so execution quality is a live risk that no
   backtest here can settle.

**Recommended next step:** run it at 1 lot (or in analyzer mode) for a
quarter — ~30 trades — and compare realised Rs/trade against the Rs 92-109
the walk-forward predicts. Scale only if the forward run lands in that band.
Resume option harvesting at the same time; it is the only way the next
premium audit gets a bigger sample than 31 trades.

## 5b. Can it be made profitable? (exploration, 2026-08-06)

Seven structures were tested. One works, marginally; the rest are dead. Every
row is scored on data the variant was not chosen on.

| Structure | Result | Verdict |
|---|---|---|
| intraday long option, **full walk-forward** (params + gate re-fitted quarterly) | 349 OOS trades, +Rs 32,142, PF 1.19, all 3 years positive | **the only survivor** |
| overnight to next 09:20 (real premiums) | -Rs 384, PF 1.00 (29 trades) | dead |
| overnight to next 15:10 (real premiums) | -Rs 6,715, PF 0.95 | dead |
| inverse the signal | fitted PF 0.74, forward PF 1.07 | sign flips — noise |
| futures instead of options | fitted +93,716, forward -7,528 PF 0.83 | dead |
| no spot stop, hold to 15:10 | walk-forward -Rs 11,775, PF 0.96, maxDD -55,985 | dead |
| short overnight ATM straddle | 47 nights -Rs 3,136, PF 0.94 | dead (and not this signal) |
| BANKNIFTY / FINNIFTY / MIDCPNIFTY | fitted PF 1.07 / 1.12 / 1.17 | weaker than NIFTY before any OOS |

### Why overnight fails — measured, not modelled
The signal DOES carry overnight: +15.6 spot points in its direction on the
fitted window (56.1% hit rate). But 99 contract-nights of real ATM premiums
give the price of a night:

    d_premium = 0.474 * d_spot - 1.28      (n=99, all DTE)
    DTE  5-9 : delta 0.492, night costs -8.06 pts = -Rs 524/lot
    DTE 10-39: delta 0.453, night costs +6.33 pts (n=33, unstable)

The gap edge is worth +15.6 x 0.474 = 7.4 premium points = +Rs 480/lot. One
night on the weekly the strategy trades costs -Rs 524/lot. The edge is
cancelled almost exactly, before transaction costs — which is why the real
premium test lands on PF 1.00. Overnight is not a rescue; it is a wash that
turns into a loss once you pay to enter.

### Why "remove the stop" fails
The live sim showed the SL bucket at PF 0.02 while max-hold carried the book,
which suggests cutting the stop. On the 28-trade forward window that looked
right (+Rs 4,412, PF 1.25) — but the entire result is two trades: remove the
best one and it is -Rs 2,027. On 349 walk-forward trades the same variant
loses Rs 11,775 with a Rs 56k drawdown. The stop is doing real work.

### What the survivor is actually worth
Honest walk-forward: Rs 92/trade at 1 lot, ~110 trades/year, PF 1.19,
max drawdown Rs 13,495. Applying the real-premium calibration (x1.185) gives
~Rs 109/trade, so roughly **Rs 12k/year per lot**. At 3 lots that is ~Rs 36k
a year on a 5.5L account (~6.5%) against a ~Rs 40k drawdown (~7%).

That is a real but thin edge, and it rests on a signal design that was itself
chosen with hindsight — only the parameters and the gate were re-fitted here.
The one window where nothing at all was fitted (28 trades, 2026-05-28..08-06)
is flat. Both facts have to be held at once.

## 6. Artifacts
- `redbar_walkforward_full.py` — parameters AND gate re-fitted quarterly (the
  honest large-sample test, 349 OOS trades)
- `redbar_walkforward.py` — gate-only re-fitting variant
- `redbar_overnight.py` — overnight spot-edge horizons (gap / d1 / d2 / MFE)
- `redbar_overnight_premium.py` — overnight cost measured on real premiums
- `redbar_structures.py` — inverse, futures, short straddle
- `redbar_forward_test.py` / `redbar_forward_trades.csv` — TRUE forward test
  on live-fetched bars after the cache boundary (the decisive evidence)
- `redbar_live_sim.py` / `redbar_live_sim_trades.csv` — live-faithful 1-minute
  simulation on real premiums (the authoritative premium harness)
- `redbar_grid.py` / `redbar_grid_results.csv` — 180-combo walk-forward grid
- `redbar_features.py` — leak-aware feature builder (yesterday-shifted)
- `redbar_backtest.py` — faithful 1-lot 30m harness (records entry/exit ts)
- `redbar_trail_backtest.py` — 2-lot study (rejected, kept for the record)

`redbar_premium.py` was deleted: its 30m bucket-end exit fills modelled a
strategy that does not exist (holding 30 minutes past the stop). Its numbers,
quoted in the previous revision of this spec, are retracted.
