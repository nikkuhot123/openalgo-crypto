# POV Wall-Squeeze Strategy

The **POV Wall-Squeeze** strategy is currently the only positive expectancy book running on the live system. It exploits order-flow imbalances and options-chain concentration (the "wall") rather than pure price patterns on the index.

---

## 1. Live Configuration

- **Underlying**: NIFTY (NFO) and SENSEX (BFO).
- **Schedule**: Starts 09:30, exits by 15:10.
- **Size**: 1 lot.
- **Instrument**: ATM Weekly PE / CE.
- **Exits**:
  - T1 Target: 1.5R (booked in premium points).
  - SL: Fixed premium stop placed at entry.
  - Max-Hold: 45 minutes.
  - Decay Floor: 60% of entry premium.

---

## 2. Recent Performance

Based on 8 real round trips backfilled from 2026-07-29 to 2026-08-07:

- **n**: 8 trades
- **Win Rate**: 62%
- **Net P&L**: **+Rs 862** (at 1 lot)
- **Avg P&L/trade**: **+Rs 108**
- **Scaled to Rs 2,00,000 Notional (13 lots)**: **+Rs 11,206** (+5.6%)

---

## 3. SENSEX Calibration

POV was previously blocked from trading SENSEX because the absolute OI thresholds were sized for NIFTY. SENSEX has 31x to 54x less volume per 5-minute bar.

On 2026-08-07, the thresholds were updated to scale with the underlying:
- **NIFTY**: 50,000 / 30,000 (unchanged)
- **SENSEX**: 1,600 / 550 (recalibrated)

This unblocked the signal: SENSEX logs now show active scoring (1/5, 2/5, 3/5) rather than 45/45 scans at 0/5. The forward test at 1 lot continues to see if it can reach the 4/5 entry bar.

---

## 4. Instrumentation: PATH logs

- **Added 2026-08-07**: Logs `PATH {symbol} ltp={live_ltp:.2f} entry={entry:.2f} R={r:.2f} rmult={rmult:+.2f}` every cycle while holding a position.
- **Why**: POV trades the front weekly, which is deleted from the broker master immediately after expiry. Without this log, the intraday premium path is lost and cannot be replayed for exit-rule research.
- **Note**: Line 1128 overwrites `entry_opt_price = _fill_entry` at fill time, meaning the logged quote is the fill, and slippage reads as 0. This is a known instrumentation gap.
