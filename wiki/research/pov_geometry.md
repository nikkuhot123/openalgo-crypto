# POV: stated R:R is not actual R:R

Asked whether POV shares Judas's stop problems. It does not share the one that
prompted the question -- and it has a different one.

## What POV does NOT have wrong

Judas's stop is a SPOT level held in-process, so it cannot be rested at a broker
without translating spot to premium, and that mapping is too unstable to use
(see `judas_broker_stop.md`). POV is immune to all of that: `sl = round(lo, 2)`
is the **option candle's own low**, already a premium level, so it rests natively.

Verified live: 6 SL orders armed, all triggers 0.05-tick aligned
(8.3 / 103.9 / 69.75 / 22.6 / 44.35 / 49.15), **0 rejections, 0 UNPROTECTED
events**. POV also already re-arms its stop on adoption after a restart, and logs
`position UNPROTECTED` at ERROR if that fails. That machinery is sound.

## What POV does have wrong

`sl`, `t1`, `t2`, `t3` are computed in `evaluate_pov()` from the **signal
candle's close**. The position then fills somewhere else, and **the geometry is
never re-derived from the fill**.

Recovering the signal close from `t1 = e + 1.5*(e - sl)`:

| contract | signal close | fill | slip | SL % of premium | **actual R at T1** |
|---|---|---|---|---|---|
| NIFTY18AUG2624300CE | 9.75 | 10.15 | +4.1% | 18.2% | **0.96R** |
| NIFTY25AUG2624150CE | 105.65 | 106.45 | +0.8% | **2.4%** | **0.72R** |
| SENSEX20AUG2677500CE | 75.25 | 71.65 | -4.8% | **2.7%** | 6.24R |
| SENSEX20AUG2677700CE | 31.95 | 27.20 | -14.9% | 16.9% | 4.08R |
| SENSEX20AUG2677600CE | 59.10 | 60.00 | +1.5% | 26.1% | 1.36R |
| SENSEX20AUG2677500PE | 64.60 | 54.40 | -15.8% | 9.7% | 6.36R |

The code believes **every T1 is 1.50R**. The realised range is **0.72R to
6.36R**, and on **2 of 6 trades T1 paid less than the stop risked** -- the first
target returns less than the position is risking to reach it.

This is the same class as Judas's documented `MIN_EFFECTIVE_RR` bug -- *"the
reward:risk silently inverts... while RR reads 2.0"* -- reached by a different
route. Judas's inversion came from flooring the STOP while the target kept using
raw risk; POV's comes from the ENTRY moving while both stop and target stay
pinned to the signal candle. Judas is structurally immune to POV's variant
because its geometry is in spot, which an option fill cannot move.

Two entries also stopped only **~2.5% from the premium**. Measured live spread is
~0.41% of premium, so that is roughly six spreads of room.

## Why it is logged and not fixed

Recomputing `t1/t2/t3` from the actual fill would restore the intended 1.5R/3R/5R
and is arguably the correct fix. It is deliberately **not** applied:

POV is the **only positive-expectancy strategy** in this repo (+Rs 108/trade,
62% win) and it earned that record **with this geometry**. Silently moving its
targets is an untested change to the one thing that works. The same reasoning
was applied to the POV ratchet question: instrument first, decide on evidence.

So entry now emits:

```
GEOMETRY <sym> fill=106.45 sl=103.90 risk=2.55 (2.4% of premium) T1=108.28 -> 0.72R actual (intended 1.50R)
GEOMETRY INVERTED on <sym>: T1 pays 0.72R against 1.00R of risk
```

with a separate warning when the stop sits at or above the fill, so risk is never
divided by zero into a bogus R. All of it wrapped so a diagnostic can never
affect a position that was just opened.

**Decision point:** once ~15 entries carry `GEOMETRY` lines, compare outcomes for
inverted (<1R) versus normal geometry. If the inverted ones underperform,
re-derive from the fill. If they do not, the distortion is harmless and the
current behaviour stays.

*10 tests in `test/test_pov_geometry.py`. Deployed pov `e086ebacba3d`.*
