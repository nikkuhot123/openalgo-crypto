# HA-EMA 34 Channel Strategy (Retired)

The **HA-EMA 34 Channel** strategy was retired on 2026-08-07. The registration was dropped from the live system after backtests proved the signal has no directional edge.

---

## 1. History & Performance

The strategy uses Heiken Ashi candles to determine bias (GREEN -> long only, RED -> short only) and triggers trades on breakouts of a 34-EMA channel built on 5-minute highs and lows.

### NIFTY Index Proxy Backtest
Backtested over 1 year (2025-07-03 to 2026-07-28, 264 sessions) using the live broker data feed:
- **n**: 188 trades
- **Win Rate**: 42.0% (79W / 109L)
- **Net P&L (Index Points)**: **-5.6 pts average per trade**
- **Net P&L (Rupees)**: **-Rs 68,928** (at 1 lot futures proxy)
- **Sharpe Ratio**: **-3.82**
- **Max Drawdown**: -3.75%
- **NIFTY Buy & Hold**: -6.01% (underperformed by the strategy after trading costs)

### Option Translation
Because the strategy lost −5.6 index points per trade on the index itself (before option premium friction), it is structurally impossible for the option-buying version of this strategy to be profitable. Once weekly options spread and theta decay are factored in, the losses would be substantially worse.

---

## 2. De-registration (2026-08-07)

On 2026-08-07, the registrations for NIFTY (`ha_ema34_channel_strategy`) and SENSEX (`ha_ema34_channel_strategy_sensex`) were dropped from `strategy_configs.json`. The strategy has taken 0 trades since the 2026-07-29 backfill window and was retired to stop future exposure.

---

## 3. Reference Files

The strategy code remains in `strategies/examples/ha_ema34_channel_strategy.py` for reference. The backtest script is in `backtesting/haema_signal/NIFTY_haema_signal_backtest.py`.
