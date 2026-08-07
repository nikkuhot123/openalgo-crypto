# Stock Intraday Backtest (Renko PRO)

A study testing the `Doctor_Diven_Smart_Renko_Engine_Pro_Combined.pine` strategy on liquid cash equity (MIS) stocks to evaluate if lower transaction friction rescues the strategy's performance.

---

## 1. Methodology

- **Capital**: Rs 2,00,000 (fixed research notional).
- **Position Sizing**: 1% risk per trade (Rs 2,000 risk budget based on entry-to-SL distance), capped by 5x MIS leverage (max Rs 10,000,000 position value).
- **Friction**: 0.035% of turnover (no brokerage under Flattrade, STT 0.025% on sell, transaction charges, GST, stamp duty).
- **Symbols**: RELIANCE, SBIN, HDFCBANK, ICICIBANK, TCS (yfinance 60-day intraday limit, June - August 2026).

---

## 2. Results

### 15-Minute Timeframe (327 trades)

| Symbol | n | Win Rate | Profit Factor | Net P&L (Rs) | Avg Turnover |
|---|---|---|---|---|---|
| RELIANCE | 66 | 31.8% | 0.68 | **-Rs 25,703** | Rs 1,415,405 |
| SBIN | 70 | 27.1% | 0.44 | **-Rs 57,341** | Rs 1,277,700 |
| HDFCBANK | 68 | 35.3% | 0.76 | **-Rs 18,745** | Rs 1,496,749 |
| ICICIBANK | 68 | 29.4% | 0.55 | **-Rs 44,643** | Rs 1,414,782 |
| TCS | 55 | 38.2% | 0.87 | **-Rs 9,588** | Rs 1,400,414 |
| **ALL** | **327** | **32.1%** | **0.64** | **-Rs 156,019** | **-78.0% on 2L** |

### 30-Minute Timeframe (192 trades)

| Symbol | n | Win Rate | Profit Factor | Net P&L (Rs) | Avg Turnover |
|---|---|---|---|---|---|
| RELIANCE | 34 | 41.2% | 0.85 | **-Rs 4,389** | Rs 1,256,494 |
| SBIN | 43 | 39.5% | 1.01 | **+Rs 438** | Rs 1,090,789 |
| HDFCBANK | 47 | 40.4% | 0.85 | **-Rs 6,986** | Rs 1,176,883 |
| ICICIBANK | 33 | 30.3% | 0.41 | **-Rs 25,058** | Rs 1,063,672 |
| TCS | 35 | 45.7% | 0.87 | **-Rs 4,624** | Rs 971,754 |
| **ALL** | **192** | **39.6%** | **0.79** | **-Rs 40,618** | **-20.3% on 2L** |

---

## 3. Findings

1. **Disastrous Performance**: The strategy loses money on almost every stock and timeframe. At 15m, it wiped out 78% of the notional capital in 60 days.
2. **The Leverage Trap**: While cash intraday friction is extremely low in percentage terms (0.035%), the tight stop-loss distances allowed the risk-sizing model to max out the 5x leverage ceiling (average turnover was ~Rs 10-15 Lakhs per trade). Because the signal lacks a directional edge, leverage simply accelerated the losses.
3. **Moneyness & Friction**: Even though the average trade paid Rs 340-524 in transaction friction, the gross trading losses before friction were already deeply negative.

**Verdict: Do not trade this strategy on stocks intraday.** Lowering friction by switching from index options to cash equities does not help because the core signal has no predictive edge.

*Script: `backtesting/renko_engine/stock_backtest.py`*
