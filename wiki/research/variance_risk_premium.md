# Variance Risk Premium — Selling vs Buying

Six strategies have now been tested in this repo. All five that came before this page **bought** options. This study asks whether that was the structural mistake.

---

## 1. The mechanism

Research confirms a documented, statistically significant **positive variance risk premium (VRP) in Indian index options** — implied variance systematically exceeds realized variance on NIFTY.

- [Does the VRP from NIFTY options drive returns?](https://economic-sciences.com/index.php/journal/article/view/356) — *"India VIX and realized variance from log returns shows that implied variance regularly surpasses realized variance, producing a statistically significant and positive VRP."*
- [Dynamics of variance risk premium: Evidence from India](https://ideas.repec.org/a/eee/reveco/v70y2020icp321-334.html) — rejects the hypothesis that variance risk is unpriced in high-retail-participation markets.
- [Implied, realized and historical volatility (JBEM)](https://journals.vilniustech.lt/index.php/JBEM/article/download/3056/2532)

**Implication**: option buyers pay this premium on average. Every strategy tested here until now was on the paying side of it.

---

## 2. The full ladder (Volrix, real option premiums, 2026-02-09 → 2026-08-08)

| Approach | n | Win% | PF | Net | Max DD | Sharpe | Sortino |
|---|---|---|---|---|---|---|---|
| BUY stoch NIFTY | 51 | 33.3 | 0.90 | -Rs 6,796 | 19.2% | -0.86 | -1.97 |
| BUY stoch SENSEX | 54 | 25.9 | 0.70 | -Rs 21,714 | 26.4% | -2.16 | -4.19 |
| SELL straddle NIFTY (30% SL) | 244 | 38.5 | 0.90 | -Rs 22,921 | 26.2% | -0.92 | -1.29 |
| SELL straddle SENSEX (30% SL) | 244 | 38.1 | 1.00 | -Rs 16,466 | 19.7% | -0.67 | -0.90 |
| **IRON FLY NIFTY** | 488 | 45.3 | **1.00** | **-Rs 1,261** | **13.2%** | **-0.09** | -0.10 |
| **IRON FLY SENSEX** | 488 | 46.5 | **1.00** | -Rs 3,392 | **8.4%** | -0.44 | -0.58 |

Reports:
- Short straddle — [NIFTY](https://app.volrix.ai/report/96927005-5ecd-4edd-b21f-d351c55528c9?account=33ca1fd3c1423f2cbed97b1f62fda5ec95b7627616b8d55bcb8c9e4032f417bd) · [SENSEX](https://app.volrix.ai/report/e98233e1-f137-47f1-b356-28414950c945?account=42f525cd3f8e8e6266ad7df29e7ea36a1fb21f202a7e7dfed4c7ac096933a4d3)
- Iron fly — [NIFTY](https://app.volrix.ai/report/1526d519-9bd2-4312-9712-616ce15ff28c?account=e1341d3221f186dbcf92c12461953775ac5a94369d0a0582d77cf46b430cdd4d) · [SENSEX](https://app.volrix.ai/report/b18da29d-f6fb-47a4-9970-158d7e5db4c1?account=a49961d8fbe69797036f385900b0ce6f441b351396a52957082d6772abfdca22)

---

## 3. What the ladder shows

### 3.1 The naive sell fails for a diagnosable reason
A premium-selling strategy winning **38%** is mis-specified. Selling should win 60-70% with rare large losses. A 30% stop on a short ATM option fires on ordinary intraday noise, converting the seller's edge into a loss. That is a structural error, not a parameter to sweep.

### 3.2 Fixing the structure works — directionally
Replacing the noise-triggered stop with long wings (defined risk, no stop at all):
- Max drawdown **26.2% → 13.2%** (NIFTY), **19.7% → 8.4%** (SENSEX)
- Sharpe **-0.92 → -0.09** (NIFTY)
- Win rate 38.5% → 45.3% (daily 54.9%)

### 3.3 And then stops exactly at break-even
Both iron flies land on **PF 1.00**. NIFTY avgWin +Rs 1,548 vs avgLoss -Rs 1,550 — symmetric to the rupee.

**The VRP edge is real and is almost exactly consumed by friction.** An iron fly pays four bid-ask spreads plus four sets of statutory charges per round trip. At the measured 26-39 bps entry slippage per leg, four legs cost roughly what the premium edge is worth.

---

## 4. Consequences

1. **Leg count is the lever, not the signal.** Four legs pay four spreads. A two-leg structure (short strangle, or a credit spread) halves the friction against the same premium edge. This is mechanical, not fitted.
2. **The cost wall is confirmed from a second direction.** Buying loses to theta plus friction; selling earns theta and loses to friction. Six strategies now land between PF 0.70 and PF 1.00. That consistency is the finding.
3. **Capital blocks this route anyway.** Selling index straddles requires roughly Rs 1.5-2 lakh margin per lot. At the live balance this is not accessible regardless of edge.

---

## 5. Verdict

Do not deploy either sell structure. The iron fly is the best-behaved system tested in this repo — lowest drawdown, Sharpe near zero rather than deeply negative — and it still does not clear costs.

The honest read across all six tests: **this account's binding constraint is transaction friction and capital, not signal discovery.** Further signal search has low expected value. The two things with measured positive expectancy remain POV (OI-based, +Rs 108/trade) and the unfixed give-back leak in Judas.

*Strategy code and runs via Volrix MCP. Research via agent-reach (web backend).*
