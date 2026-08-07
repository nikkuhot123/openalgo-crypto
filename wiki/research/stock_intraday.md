# Stock Intraday Backtest (Renko PRO)

A study testing the `Doctor_Diven_Smart_Renko_Engine_Pro_Combined.pine` strategy on liquid cash equity (MIS) stocks to evaluate if lower transaction friction rescues the strategy's performance.

---

## 1. Methodology

- **Capital**: Rs 2,00,000 (fixed research notional).
- **Position Sizing**: Flat Rs 1,00,000 allocation per trade (no dynamic risk sizing based on stops).
- **Friction**: 0.035% of turnover (no brokerage under Flattrade, STT 0.025% on sell, transaction charges, GST, stamp duty).
- **Symbols**: RELIANCE, SBIN, HDFCBANK, ICICIBANK, TCS (yfinance 60-day intraday limit, June - August 2026).

---

## 2. Results (RAW Flat-Sizing)

### 15-Minute Timeframe (327 trades)

| Symbol | n | Win Rate | Profit Factor | Net P&L (Rs) | Max DD (Rs) | Sharpe (Trade) |
|---|---|---|---|---|---|---|
| RELIANCE | 66 | 31.8% | 0.65 | **-Rs 4,369** | Rs 7,616 (3.8%) | -1.45 |
| SBIN | 70 | 27.1% | 0.43 | **-Rs 10,465** | Rs 10,883 (5.4%) | -2.76 |
| HDFCBANK | 68 | 35.3% | 0.82 | **-Rs 1,984** | Rs 3,276 (1.6%) | -0.66 |
| ICICIBANK | 68 | 29.4% | 0.65 | **-Rs 5,198** | Rs 5,432 (2.7%) | -1.57 |
| TCS | 55 | 38.2% | 0.87 | **-Rs 1,484** | Rs 3,738 (1.9%) | -0.43 |
| **ALL** | **327** | **32.1%** | **0.66** | **-Rs 23,500** | **Rs 25,005 (12.5%)** | **-3.16** |

### 30-Minute Timeframe (192 trades)

| Symbol | n | Win Rate | Profit Factor | Net P&L (Rs) | Max DD (Rs) | Sharpe (Trade) |
|---|---|---|---|---|---|---|
| RELIANCE | 34 | 41.2% | 1.14 | **+Rs 682** | Rs 3,145 (1.6%) | +0.25 |
| SBIN | 43 | 39.5% | 0.87 | **-Rs 1,058** | Rs 2,398 (1.2%) | -0.36 |
| HDFCBANK | 47 | 40.4% | 0.71 | **-Rs 2,931** | Rs 4,094 (2.0%) | -0.95 |
| ICICIBANK | 33 | 30.3% | 0.57 | **-Rs 3,305** | Rs 4,301 (2.2%) | -1.44 |
| TCS | 35 | 45.7% | 0.99 | **-Rs 57** | Rs 4,524 (2.3%) | -0.02 |
| **ALL** | **192** | **39.6%** | **0.83** | **-Rs 6,670** | **Rs 13,403 (6.7%)** | **-1.01** |


---

## 3. Findings

1. **Consistently Negative Expectancy**: The strategy loses money on almost every stock and timeframe even under raw flat-sizing. At 15m, the overall portfolio loses Rs 23,500 (-11.7% return) with a win rate of 32.1% and a Sharpe of -3.16.
2. **Signal Lacks Edge**: Removing the compounding effect of stop-loss-based risk sizing confirms that the underlying signal simply lacks a directional edge. At 30m, only RELIANCE scraped a tiny profit (+Rs 682, PF 1.14, Sharpe +0.25); the other 4 stocks were negative or flat.
3. **Low Friction Cannot Save It**: Even though transaction friction is minimal (only Rs 70 per trade round-trip on average at Rs 1L size), the gross trading outcomes are consistently negative.

**Verdict: Do not trade this strategy on stocks intraday.** Lowering friction by switching from index options to cash equities does not make a strategy with no directional edge profitable.

*Script: `backtesting/renko_engine/stock_backtest.py`*
