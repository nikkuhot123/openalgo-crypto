# Stochastic Crossover (SKB) — Backtest

A study of the SKB Trading Lab "Stochastic Crossovers" method, tested on the OpenAlgo event-driven engine (spot signal + modelled option translation) and then confirmed on Volrix with **real option premiums**.

---

## 1. The Strategy (as specified on the source chart)

- **Indicator**: Stochastic **(14, 3, 3)** — %K length 14, %K smooth 3, %D 3
- **BUY**: %K crosses **above** %D from the oversold zone (**< 20**)
- **SELL**: %K crosses **below** %D from the overbought zone (**> 80**)
- Chart timeframe: NIFTY 50, **15m**

The chart carries its own caveat, and it turned out to be the most important line on the image:

> *"Stochastic works best in Trading Ranges (Sideways Market). In strong trending markets, use it with other tools like Price Action, Support & Resistance, Trendlines."*

---

## 2. OpenAlgo engine — chart defaults (Rs 2,00,000 notional, option-translated)

| Timeframe | n | Win Rate | PF | Net P&L | Max DD | Sharpe |
|---|---|---|---|---|---|---|
| 5m | 5000 | 34.5% | 0.87 | **-Rs 5,461,512** | 2745% | -7.65 |
| **15m** (chart's own) | 1767 | 34.5% | 0.88 | **-Rs 2,371,738** | 1266% | -3.77 |
| 30m | 961 | 38.5% | 0.98 | **-Rs 749,138** | 522% | -1.40 |

The published configuration loses on every timeframe including its own.

---

## 3. Tuning sweep (162 NIFTY configurations)

Swept zones (20/80, 25/75, 30/70) x regime filter (none / with-trend / range) x RR (1.5/2/3) x stop (0.20%/0.35%).

**Only 16 of 162 configurations were profitable (10%).** Every one of the top 12 used the **`range` regime filter** — price must sit within 1.5 x ATR of the EMA50. That is the chart's own caveat validating itself mechanically.

Best NIFTY config — `30m, zone 30/70, range filter, RR 3.0, SL 0.35%`:
- n=373, win 41.6%, PF 1.21, **+Rs 347,416**, maxDD Rs 379,796 (**189.9%**), Sharpe 0.81

---

## 4. Why the tuned config was rejected

### 4.1 The edge is in-sample only

| | IS | OOS |
|---|---|---|
| NIFTY champion | **+Rs 383,892** (PF 1.35, Sharpe 1.12) | **-Rs 10,106** (PF 1.08, Sharpe -0.04) |

All of the profit sits in the first half. Out of sample it is flat-to-negative — and the OOS split here is *generous*, because parameters were chosen using the whole period.

### 4.2 The discriminating parameter flips between symbols

- Every NIFTY top-12 config used the **`range`** filter.
- Every SENSEX top-7 config used **`none`**.

A real mechanism does not invert when you change the index. Cross-applying confirms it: the SENSEX champion run on NIFTY loses **-Rs 535,014**.

### 4.3 Drawdown wall

The NIFTY champion's max drawdown is 189.9% of capital — the account is wiped out before the profit arrives.

---

## 5. Volrix confirmation — REAL option premiums

Tuned config (30m, zone 30/70, range-gated, ATM weekly, 1 lot, SL 35% / target 105% of premium, EOD 15:15). Volrix plan limits history to 6 months.

| Symbol | Period | n | Win Rate | PF | Net P&L | Max DD | Sharpe | Sortino | % of 2L |
|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 2026-02-12 → 2026-08-04 | 51 | 33.3% | **0.90** | **-Rs 6,796** | -Rs 28,866 (19.2%) | **-0.86** | -1.97 | -3.40% |
| SENSEX | 2026-02-12 → 2026-08-07 | 54 | 25.9% | **0.70** | **-Rs 21,714** | -Rs 39,584 (26.4%) | **-2.16** | -4.19 | -10.86% |

Worst losing streaks: NIFTY 11 trades (-Rs 18,424), SENSEX 8 trades (-Rs 18,344).

Reports:
- NIFTY — [report](https://app.volrix.ai/report/830d80cd-3c90-4754-a296-9176c1b0a79c?account=4099bbb14bad9b568f0c878740b597106366b88d377c5427c296e53a0c98fd50) · [metrics](https://app.volrix.ai/report/830d80cd-3c90-4754-a296-9176c1b0a79c?account=4099bbb14bad9b568f0c878740b597106366b88d377c5427c296e53a0c98fd50#metrics)
- SENSEX — [report](https://app.volrix.ai/report/80b139dd-0764-49ec-b04d-951fc7b8b298?account=c924fc086f3fc134435475d42ff90727b8d2a50c47e84a75762ccb666e65d56a) · [metrics](https://app.volrix.ai/report/80b139dd-0764-49ec-b04d-951fc7b8b298?account=c924fc086f3fc134435475d42ff90727b8d2a50c47e84a75762ccb666e65d56a#metrics)

---

## 6. Verdict

**Do not deploy.** Two independent engines agree:

1. The published configuration loses on every timeframe, including the 15m it is advertised on.
2. Tuning produced one attractive NIFTY config out of 162, whose profit is entirely in-sample, whose key parameter inverts on SENSEX, and whose drawdown exceeds capital.
3. Volrix, using real option premiums rather than a delta model, returns PF 0.90 on NIFTY and PF 0.70 on SENSEX with negative Sharpe on both.

The one genuinely useful finding is that the chart's own caveat is measurable and correct: on NIFTY the range-regime filter was the single discriminator between profitable and unprofitable configurations. That does not rescue the method, but it does mean the caveat is honest.

*Scripts: `backtesting/stochastic/stoch_backtest.py`, `backtesting/stochastic/validate.py`*
