# Wiki Schema & Guidelines

This document outlines the rules, conventions, and workflows that LLM agents must follow when maintaining this wiki. It ensures consistency, prevents drift, and gates changes.

---

## 1. Core Conventions

- **Fixed Notional Capital**: All backtest results, performance metrics, and portfolio statistics MUST be reported against a fixed notional of **Rs 2,00,000** (defined in `backtesting/config.py`). They must never use the live account balance.
- **Cost Model**: All option translation backtests must use the Flattrade-validated cost model:
  - **Statutory Charges**: 0.12% of premium turnover (both ways).
  - **Bid-Ask Spread**: 0.41% of premium (conservative default) or 0.25% (limit order trial).
  - **Delta**: 0.358 (for index option premium to spot conversion).
  - **Premium**: 0.45% of index level for weekly ATM.
- **Evidence-Driven Changes**: No strategy logic or parameter changes can be deployed to the live system without:
  - A backtest or replay over at least **n=15 trades**.
  - A paired bootstrap confidence interval that excludes zero.
  - Verification that the change is a risk-reduction or loss-prevention measure (like a break-even ratchet), rather than a parameter-tuned trail that risks capping upside.

---

## 2. Ingestion Workflow

When a new source (log, backtest script, email thread, user request) is introduced:
1. **Read**: Load the source and extract the facts.
2. **Reconcile**: Check for contradictions with existing pages. If a new backtest contradicts an older one, document the reason (e.g., "earlier run had data corruption, corrected in commit X").
3. **Edit**: Update the relevant strategy, research, or operations page. Do not create duplicate pages.
4. **Index**: Update `wiki/index.md` if a new page was created.
5. **Log**: Append a chronological entry to `wiki/log.md` starting with `## [YYYY-MM-DD] <type> | <description>`.

---

## 3. Linting Guidelines

Periodically run a pass to check:
- **Orphans**: Ensure every page has at least one inbound link.
- **Stale Claims**: Mark configurations or parameters as "superseded" if newer research has replaced them.
- **Fills vs Models**: Ensure no modeled performance is presented as "realized" unless verified from the broker tradebook.
