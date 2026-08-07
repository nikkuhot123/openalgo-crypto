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

## 2. NIFTY Results (Rs 2,00,000 Notional)

Backtested over NIFTY 5-minute bars from **2023-04-05 to 2026-05-27** (57,005 bars):

| Timeframe | n | Win Rate | PF (points) | avg pts | Option P&L (2L notional) |
|---|---|---|---|---|---|
| 5m | 1407 | 41.4% | 1.09 | +1.3 | **-Rs 18,906** (14 lots) |
| 15m | 746 | 43.0% | 1.15 | +2.6 | **+Rs 13,226** (OOS -Rs 7,488) |
| 30m | 435 | 48.0% | 1.25 | +4.1 | **+Rs 22,538** (OOS +Rs 17,735) |

*Note: 30m requires clearing a 1.9 index-point hurdle per trade to cover option friction.*

---

## 3. Findings & Verdict

Although the 30-minute timeframe showed positive overall returns, it was rejected for live deployment due to four fatal flaws:

1. **Severe Concentration**: The top 5 trades out of 435 accounted for **51% of all points** (+917 of +1,782). Without those 5 outliers, the average return drops to **+2.01 points** — scraping the 1.9-point breakeven hurdle.
2. **Directional Bias**: Long trades averaged only **+1.1 points** (losing money). The entire net profit came from the short side (avg +7.6 points).
3. **Cross-Symbol Failure**: Running the same 30m config on other indices generated consistent losses:
   - **BANKNIFTY**: −Rs 8,802
   - **FINNIFTY**: −Rs 22,513
   - **SENSEX (n=31)**: −Rs 3,693
4. **Timeframe Sensitivity**: Performance did not scale with holding time. At 60m, average return dropped back to **+2.4 points**, and 120m collapsed (n=9).

**Verdict: No edge.** The index-point edge is a statistical artifact driven by a tiny handful of outlier trades on one side of the book, and it does not survive the option friction hurdle.

*Script: `backtesting/renko_engine/renko_engine_backtest.py`*
