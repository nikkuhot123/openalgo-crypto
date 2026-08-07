# Content Index

A catalog of everything in the wiki. Updated on every ingestion.

---

## Active Strategies

| Strategy | File Path | Status | Key Focus |
|---|---|---|---|
| [[strategies/pov_wall_squeeze\|POV Wall-Squeeze]] | `strategies/scripts/pov_wall_squeeze_strategy.py` | Active | Order-flow positioning, SENSEX threshold trial |
| [[strategies/judas_swing\|Judas Swing]] | `strategies/scripts/judas_swing_strategy.py` | Active | Liquidity sweeps, premium give-back leak tracking |
| [[strategies/prior_levels_ema\|Prior Levels (PDH/PDL)]] | `strategies/scripts/prior_levels_ema_strategy.py` | Active | Overnight carry, first live run pending |

## Research & Backtesting

| Study | Script Path | Date | Finding |
|---|---|---|---|
| [[research/renko_pro_backtest\|Renko PRO Backtest]] | `backtesting/renko_engine/` | 2026-08-07 | Pos in points, loses on options friction. Edge is noise. |
| [[research/strike_selection\|Strike Selection]] | `backtesting/strike_selection.py` | 2026-08-07 | ITM sheds theta% but adds absolute friction. ATM worst. |

## Operations & Incidents

| Area | Component | Status | Target |
|---|---|---|---|
| [[vps_operations/log_rotation\|Log Rotation Incident]] | `openalgo-logcap` systemd timer | Fixed | Contained 16G runaway log, disk freed 86% -> 51% |

## Retired Code

- [[strategies/red_bar_x_candle\|Red Bar X-Candle]]: De-registered on 2026-08-07. Shipped PF 1.05 on unfitted window, EOD trade lost Rs 1,777.
