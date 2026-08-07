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
