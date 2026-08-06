# Red Bar / X-Candle — Configuration Spec & Evidence

Date: 2026-08-06 · Author: research harness (backtesting/haema_signal)
Verdict: **DO NOT DEPLOY.** The gates work, the CAS handling is correct, and
real option premiums are NOT the problem — but on the only window never used
to fit anything (2026-05-28 .. 08-06) the gated config is flat-to-negative:
28 trades, -Rs 896, PF 0.94. Two earlier verdicts in this session are
retracted below, with the reason each was wrong.

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

**Recommended allocation today: zero.** The forward test does not support
risking capital. The numbers below exist only so the sizing question has an
answer if ~50 forward trades later turn positive.

Per-trade capital is the premium: ~Rs 13,000 per lot at a 200-point ATM.
On the FITTED history (419 trades, real-equivalent, 1 lot) the strategy made
~Rs 20k/year with a Rs 11,823 max drawdown; the same series over the forward
window makes -Rs 1,061. Sizing off the fitted number would be sizing off the
curve fit.

| Lots | Capital/trade | Fitted-history/year | Fitted max DD | Forward window |
|---|---|---|---|---|
| 1 | ~13,000 | ~+20,000 | -11,823 | -1,061 |
| 3 | ~39,000 | ~+60,200 | -35,469 | -3,183 |
| 5 | ~65,000 | ~+100,333 | -59,115 | -5,305 |

If it is ever deployed: 3 lots is ~7% of a 5.5L account per trade with a ~6%
account drawdown on fitted history — and fitted drawdowns are always optimistic.

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

## 5. Why it fails, and what is still unknown

What the evidence supports:
1. The signal has no forward edge. Ungated PF 0.61 over 47 forward trades,
   corroborated by an independent Volrix run on real premiums (PF 0.5).
2. The gates are genuinely useful — they lift the forward window from
   -Rs 10,919 to -Rs 896 — but they subtract losers rather than add winners.
3. Option pricing is NOT the problem (calibration 1.185, r 0.81), CAS
   handling is correct, and the live-only exits (60% decay floor, -70%
   broker stop) never fired in 31 trades.

What remains unknown:
1. 28 forward trades is a small sample; the true forward expectancy could be
   mildly positive or clearly negative. It is not, on this evidence, good.
2. Real premiums exist for only 31 trades, all inside the fitted range. The
   harvest stopped 2026-05-27; nothing after it has real option data.
3. The 5-second exit polling was validated on those 31 trades only. On the
   forward window only the 30-minute delta model was available.

**Recommended next step:** do not allocate. If the strategy is kept alive as
research, resume option harvesting (it stopped 2026-05-27) and run the gated
config in analyzer/paper mode. Revisit only if ~50 further forward trades
turn clearly positive — the bar is a forward PF above ~1.2, not above 1.0,
because 1.0 does not pay for the attention.

## 6. Artifacts

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
