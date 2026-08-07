# Red Bar X-Candle Strategy (Retired)

The **Red Bar X-Candle** strategy was retired on 2026-08-07. The registration was dropped from the live system to stop capital bleed and focus resources on strategies with positive expectancy (POV).

---

## 1. History & Performance

Red Bar went through multiple research iterations as data bugs were found:

- **Initial Overfit**: PF 1.90 in-sample.
- **Walk-forward (corrected)**: **350 OOS trades, PF 1.20, +Rs 33,453** (avg +Rs 95/trade).
- **Frozen Hindsight**: +Rs 54,548, PF 1.36.
- **Strictly Unfitted Window (28 May - 06 Aug)**: **29 gated trades, +Rs 619, PF 1.05**.
- **Ungated (same window)**: 47 trades, −Rs 3,017, PF 0.86 (proving the gates are load-bearing).
- **Overnight Carry Variant**: Dead (real premium test −Rs 384 PF 1.00 short-hold, −Rs 6,715 PF 0.95 long-hold).

### The Live Verdict
The strategy showed a real but paper-thin edge (+Rs 92/trade) that collapses under options friction (the 1.9 index-point hurdle). The walk-forward decay (1.9 -> 1.36 -> 1.20 -> 1.05) pointed to a marginal system.

---

## 2. De-registration (2026-08-07)

On 2026-08-07, the strategy was run in live mode and took one trade:
- **NIFTY11AUG2624550PE**: Entered at 14:17 on a late breakdown signal.
- **Execution**: Spot entry 24526.85, premium entry 114.70.
- **EOD Exit (15:10)**: Forced close at 87.60.
- **P&L**: **-Rs 1,777** (at 1 lot).

This trade illustrated the core structural weakness: it entered late in the window (14:17) and had only 53 minutes of runway before the 15:10 EOD flat. Spot drifted against the PE (up +30 pts) and the option bled time value.

Because the strategy was marked "do not scale, forward-test at 1 lot only" and took the worst loss of the day, the registration was retired to prevent further drawdowns.

---

## 3. Reference Files

The strategy code remains in `strategies/examples/red_bar_x_candle_strategy.py` as it contains the benchmark gating implementations:
- `SKIP_WEEKDAYS` (Tuesday)
- `MOM5_PREV_MAX` (0.0137)
- `DRY_RUN` shadow mode
