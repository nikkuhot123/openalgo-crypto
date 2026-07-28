# NIFTY Autoresearch

An experiment in having the LLM autonomously improve a trading strategy.

Adapted from **[marketcalls/emacrossover-autoresearch](https://github.com/marketcalls/emacrossover-autoresearch)**
(video: *Self-Improving AI Backtesting with Claude Code, OpenAlgo & VectorBT*,
<https://www.youtube.com/watch?v=scj3NbqzYds>), which in turn follows
[karpathy/autoresearch](https://github.com/karpathy/autoresearch).

## What differs from the reference, and why

The reference scores a **single window** and keeps any change that improves it.
On this account that is an overfitting machine, and we have the receipt: on
2026-07-28 a weekday filter scored **+5.38% / Sharpe 1.33** over Feb–Jul, then
split into **+7.20%** on the first half and **−10.42%** on the second. One number
hid a regime break.

So `prepare.py` cuts the data three ways on day boundaries:

| slice | share | role |
|---|--:|---|
| TRAIN | 60% | explore freely |
| VAL | 20% | **the score comes from here** — a change must generalise |
| TEST | 20% | never scored; printed for information only |

The baseline shows why: EMA 10/30 gives **+1.09% / Sharpe 0.94 on TRAIN** and
**−8.97% / Sharpe −7.44 on VAL**. The reference harness would have reported the
train figure and looked healthy.

`train_val_gap` is printed every run. A large positive gap means you fitted TRAIN.
Treat a shrinking gap as progress even when the score moves slowly.

## Setup

1. **Agree a run tag** with the user, e.g. `jul28`. Branch `autoresearch/<tag>` must not exist.
2. `git checkout -b autoresearch/<tag>`
3. Read `prepare.py` (read-only harness) and `strategy.py` (the file you edit).
4. Create `results.tsv` with the header row only. Leave it **untracked**.
5. Confirm, then start the loop.

## Fixed configuration (do not change)

| item | value |
|---|---|
| symbol / exchange | NIFTY / NSE_INDEX (index, futures-style proxy) |
| interval | 5m, `source="db"` (OpenAlgo Historify DuckDB) |
| lookback | 400 days (~13 months, 180 sessions) |
| capital | Rs 20,00,000 · 1 lot = 65 |
| costs | 0.018% + Rs 20/order (F&O futures) |
| split | 60 / 20 / 20 train / val / test |

**Targets (judged on VAL):** return ≥ 4% · Sharpe ≥ 1.0 · max DD ≤ 6% · trades ≥ 15.
`score` is the distance from those (lower is better; 0.0 = all met).

## Rules

**You MAY** edit `strategy.py` only: indicators, entry/exit logic, filters,
parameters, session windows, stop/target logic.

**You MAY NOT**
- modify `prepare.py` (harness, costs, split, scoring)
- change symbol, exchange, interval, lookback, capital, or fees
- install packages — available: `talib`, `openalgo` (`api`, `ta`), `pandas`,
  `numpy`, `vectorbt`, `duckdb`
- **look ahead.** No `.shift(-n)`, no future indices, no using a bar's own high/low
  to decide entry on that same bar. Lookahead invalidates the run entirely — if you
  are unsure whether something peeks, assume it does and pick another idea.
- keep the `generate_signals(df)` signature — it returns
  `(long_entries, long_exits, short_entries, short_exits)`.

## Ideas, roughly in priority order

Costs are ~Rs 20 + 0.018% per order, and the baseline turns over 242 times on
TRAIN alone — **fee drag is the first thing to attack**.

1. Cut trade count: require a wider EMA separation, or a minimum bar range, before entering.
2. Session filter: avoid the first/last 15 minutes; test 09:30–14:30 only.
3. Trend filter: only long above a higher-timeframe EMA/SMA; only short below.
4. Regime filter: ADX > 20, or skip when ATR is in its lowest quartile (chop).
5. Tune EMA pair (5/20, 8/21, 12/26, 9/30, 20/50).
6. Asymmetry: this account's evidence says the short side behaves differently
   from the long — try enabling one side only.
7. Volatility-scaled stops: ATR-based stop/target instead of pure crossover exit.
8. Confirmation: MACD histogram sign, or RSI not already extended.
9. Time-based exit (close after N bars) to stop losers bleeding.
10. Combine the two best near-misses.

## Output

```
grep "^score:\|^total_return:\|^sharpe:\|^max_drawdown:\|^total_trades:\|^all_targets:\|^train_val_gap:" run.log
```

## Logging

Append to `results.tsv` (TAB separated, 9 columns, untracked):

```
commit	score	val_return	val_sharpe	val_maxdd	val_trades	train_val_gap	status	description
```

`status` is `keep`, `discard` or `crash` (use score `999.999999` for crashes).

## The loop

Run on the dedicated branch. LOOP:

1. Check git state.
2. Edit `strategy.py` with one idea. Change **one thing at a time** — bundled
   changes make attribution impossible.
3. `git commit`
4. `C:/Users/nikhi/Desktop/openalgo/venv/Scripts/python.exe prepare.py > run.log 2>&1`
5. Grep the metrics (above).
6. Empty grep = crash. `tail -n 50 run.log`, read the traceback, fix if trivial;
   after a few failed attempts abandon that idea and log `crash`.
7. Append the row to `results.tsv`.
8. Score improved → keep the commit (advance the branch).
9. Score equal or worse → `git reset --hard` back to the previous commit.
10. `all_targets: YES` → log it, then **check TEST once**. If TEST also holds,
    report to the user before continuing. If TEST collapses while VAL passed,
    you have fitted VAL — say so plainly rather than celebrating.

**Do not stop to ask whether to continue.** Run until interrupted. If out of
ideas, re-read `strategy.py` for angles, revisit near-misses, or combine filters.

**Honesty clause:** the point is a strategy that works out of sample, not a low
number. If a result looks too good, suspect lookahead or a fee/sizing artefact
and check before logging `keep`.
