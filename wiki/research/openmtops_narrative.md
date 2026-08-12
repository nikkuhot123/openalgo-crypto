# OpenMTOps — Upstream Source & the Narrative Tape-Reading Module

[CApsUNlocked123/openmtops](https://github.com/CApsUNlocked123/openmtops) is the repo our POV strategy derives from. Reviewed 2026-08-12 for (a) fidelity of our port and (b) whether its `narrative.py` tape-reading feature is worth adopting.

---

## 1. Our POV port is faithful

`pov_engine.py` (174 lines) specifies:

```
PRE: sum of positive OI changes over last 3 candles >= 50,000
C1:  volume > rolling_mean(5) * 3.0
C2:  abs(oi_change) < 30,000      [7% of total OI for MIDCPNIFTY]
C3:  (high-low) > prev(high-low) * 2.0
C4:  (min(open,close) - low) / (high-low) < 0.15
C5:  close > open
Score 5/5 -> STRONG | 4/5 -> WATCH | <4 -> WAIT
entry=close, sl=low, t1/t2/t3 = 1.5R / 3.0R / 5.0R
```

Every constant matches our implementation, including the 5/5 entry gate. Our SENSEX recalibration (1600/550) is a rescaling of `_PRE_OI_MIN` / `_OI_ABS_THRESHOLD`; upstream is Dhan/NIFTY-only and never hit that problem.

**Key architectural fact**: upstream sources OI from the *candle feed* (`oi_change = current_oi - previous_candle_oi`), not from an option-chain endpoint. Our port does the same via `client.history()`.

---

## 2. OI feed verified independently

The 2026-08-12 `optionchain` 404 outage did **not** touch POV scoring — `optionchain` was only ever called inside `fetch_lot_size`.

Cross-checked the `history()` OI feed against the greeks collector's independent live-quote path (NIFTY25AUG26FUT, same session):

| metric | value |
|---|---|
| matched minutes | 312 |
| mean absolute difference | 2,128 (**0.017% of OI**) |
| max absolute difference | 26,390 |
| early samples | exact to the unit |

The residual is a sampling-time offset (collector samples mid-minute; `history()` reports the bar). Immaterial against POV's 50,000 PRE threshold and a typical 33,456/min |dOI|. **The OI feed is trustworthy.**

Scoring in fact *improved* that day: 5,148 polls (vs 4,247 on 08-10), and POV reached **5/5 three times** — its first STRONG signals in three days. All three were destroyed by the lot-size bug, not by data quality.

---

## 3. `narrative.py` — what it actually is

730 lines, 7 detector templates producing plain-English bullets from chain data:

| Template | Detects | Runnable on our feed? |
|---|---|---|
| `strike_actions` | OI x price quadrant: long buildup / fresh writing / short covering / long unwinding | **Yes** — needs OI + price only |
| `walls` | OI concentration levels | **Yes** — needs per-strike OI |
| `volume_spikes` | volume vs rolling median | **Yes** |
| `spot_rejections` | spot reversing off a wall | Yes, with a chain scan |
| `iv_regime` | chain-wide IV expansion / compression | **No — our `history()` returns no IV** |
| `divergences` | option price moving against its expected spot relation, IV-confirmed | **No — needs IV** |
| `chain_regime` | aggregate chain state | No — needs IV |

The core quadrant logic:

```python
if d_oi > 0 and d_price > 0:  "long buildup"
if d_oi > 0 and d_price < 0:  "fresh writing"
if d_oi < 0 and d_price > 0:  "short covering"
if d_oi < 0 and d_price < 0:  "long unwinding"
```

---

## 4. Assessment

### Against it
- **It is descriptive, not predictive.** Its own docstring: each bullet "describes ONE observable event". There is no backtest, no edge claim, no expectancy anywhere in the module.
- Thresholds are self-described as "intentionally conservative" and are unvalidated.
- **Half of it cannot run on our data.** `client.history()` returns `close/high/low/oi/open/volume` — no IV. Every IV template is out unless we add per-strike `optiongreeks` calls, which is a large call-volume increase.
- It wants a **full chain**; POV tracks ~5 legs. Different data volume entirely.
- We are headless. The narration itself — the actual product of the module — has no consumer here.

### For it
- It is built **entirely on OI / IV / volume**, i.e. positioning. That is the only family that has produced positive expectancy in this repo: POV is +Rs 108/trade at 62% win, while six price-geometry strategies died between PF 0.70 and 1.00.
- The quadrant label is cheap, needs only data we already pull, and is a genuine hypothesis rather than a fitted parameter.

### Verdict

**Not useful as a feature. Possibly useful as instrumentation.**

The honest highest-value use is not signal generation but **trade annotation**: tag every POV and Judas entry with the OI x price quadrant at that moment. That costs nothing (the data is already in the candles we fetch) and would help answer why trades fail — which is the open question behind the n>=15 give-back study.

If the annotation shows outcomes separating cleanly by quadrant, *that* becomes a pre-registered filter hypothesis with a real sample behind it. If it does not, we have lost nothing.

**Do not port the narration layer.**

---

## 5. Separate finding: the greeks collector is not collecting options

`cas_ticks_2026-08-{10,11,12}.csv` contain **zero option rows** — only `spot` (936) and `future` (624) kinds. The `delta/theta/gamma/vega/iv` columns exist but are empty throughout, because no option leg is ever sampled.

This matters: the collector was retained specifically as "the only forward source of real ATM option quote/greek data". It is not currently fulfilling that purpose. Needs a separate fix.
