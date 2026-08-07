# Strike Selection Study

A study replaying the 4 live NIFTY trades from the August 2026 contract across 7 different option strikes to measure the impact of moneyness on friction and theta decay.

---

## 1. Methodology

- **Fixed Signal**: Identical entry time, exit time, and direction. Only the contract strike was varied.
- **Strikes Tested**: OTM3 to ITM3 (NIFTY 50-point intervals).
- **Cost Model**: 0.12% x2 statutory charges, 0.41% bid-ask spread.
- **Sample**: The 4 replayable Judas Swing trades on the `11AUG26` contract (2026-08-05 to 2026-08-07).

---

## 2. Replay Results

Total net Rupees across all 4 trades, sized to a **Rs 2,00,000 Notional**:

| Moneyness | avg entry prem | avg end % | total Rs (2L notional) | wins |
|---|---|---|---|---|
| **OTM3** | 69.35 | −7.6% | **-Rs 1,485** | 1/4 |
| OTM2 | 85.79 | −6.8% | **-Rs 1,673** | 1/4 |
| OTM1 | 108.49 | −6.1% | **-Rs 1,886** | 1/4 |
| **ATM** | 127.69 | −5.5% | **-Rs 2,093** | 1/4 |
| ITM1 | 153.45 | −4.7% | **-Rs 2,184** | 1/4 |
| ITM2 | 176.29 | −4.3% | **-Rs 2,389** | 1/4 |
| **ITM3** | 213.10 | −3.6% | **-Rs 2,392** | 1/4 |

---

## 3. Findings

### Finding A: The Friction vs Theta Tension
- **Theta% decreases as you go ITM**: The average end-of-trade premium change improved monotonically from **−7.6% (OTM3)** to **−3.6% (ITM3)**. Deep ITM options lose less percentage value to time decay during the hold.
- **Friction Rs increases as you go ITM**: The absolute rupee cost of transaction charges and spreads scales with the premium. Because the premium is larger, the 0.41% spread cost outweighs the theta savings.
- **Net result**: Net Rupees got monotonically **worse** as moneyness moved ITM (from −Rs 1,485 to −Rs 2,392). Friction won the tug-of-war.

### Finding B: ATM is a Poor Vehicle
- The ATM strike represents the worst point on the curve, balancing high theta percentage decay with meaningful absolute friction.
- However, **no strike rescued the trade**. All 7 strikes lost money overall.

**Verdict: Strike selection is not a rescue lever.** You cannot fix a leaking signal (giving back open profits) by shifting the strike. The exit logic is the only lever.

*Script: `backtesting/strike_selection.py`*
