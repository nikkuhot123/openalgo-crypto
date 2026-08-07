# Judas Swing Strategy

The **Judas Swing** strategy exploits intraday liquidity sweeps and session range structure. While the index-point signal is highly profitable, the strategy currently loses money due to option premium decay (theta) during long holding times.

---

## 1. Live Configuration

- **Underlying**: NIFTY (NFO) and SENSEX (BFO).
- **Schedule**: Starts 09:45, exits by 15:10.
- **Size**: 1 lot.
- **Exits**:
  - Target: 2R (index spot points).
  - Stop Loss: opposite side of range sweep.
  - **Break-even Ratchet (Added 2026-08-06)**: Moves the spot stop to entry once the trade reaches +1R in spot points.

---

## 2. Recent Performance

Based on 4 real round trips backfilled from 2026-07-29 to 2026-08-07:

- **n**: 4 trades
- **Win Rate**: 25%
- **Net P&L**: **-Rs 2,231** (at 1 lot)
- **Avg P&L/trade**: **-Rs 558**
- **Scaled to Rs 2,00,000 Notional (13 lots)**: **-Rs 29,003** (−14.5%)

This recent stretch sits against a larger historical backtest showing **+4.73R over 25 trades** (before option friction). The difference is the premium give-back leak.

---

## 3. The Premium Leak Case Study

On 2026-08-07, Judas PE entered at 11:40 and was held until EOD at 15:10:
- **Entry Premium**: 127.50
- **Peak Premium (14:15)**: 148.50 (**+16.5%**, +Rs 1,365)
- **Exit Premium (15:10)**: 109.70 (**-14.0%**, -Rs 1,157)
- **Give-back**: **Rs 2,522** (30.4% of entry)

The break-even ratchet armed at 14:12 (spot hit +1.01R) and did nothing because the stop is on **spot** and spot never reversed to entry (closing 21 points in favour). The option premium bled to death over 3.5 hours.

---

## 4. Operational Plan: The n>=15 Gate

- **Instrumentation**: Added throttled `PATH` logging (default 30s) inside the `IN_TRADE` branch to record the premium path before contract expiry.
- **Study**: Replay rules (premium breakeven at 5%/8%, premium trails, time stops) on the collected logs.
- **Pre-registered Gate**: Ship a premium-based exit only if:
  - The sample size reaches **n >= 15 trades**.
  - The winner is a **break-even** rule rather than a trailing stop (trailing was proven to cap runners and worsen overall return in the 25-trade spot study).
  - The paired bootstrap confidence interval excludes zero.
