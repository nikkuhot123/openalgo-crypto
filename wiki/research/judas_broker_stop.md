# Resting Broker Stop for Judas -- Evidence Review

Judas holds its stop **in-process**: `sl_spot` is a SPOT level, checked every ~5s
poll. If the process dies between polls, the position is unprotected. POV by
contrast rests a real stop-LIMIT order at the broker, which survives a crash.

Should Judas do the same? A resting broker order **cannot watch spot** -- it can
only rest on the OPTION premium. So the real question is whether Judas's spot
stop can be translated into a premium stop accurately enough to be worth it.

Data: 19 live sessions, 6,493 `Monitoring Trade` lines (spot + live stop), 1,345
`PATH` lines (premium), 10 entries, 6 break-even arms, 19 shutdowns. Pairing
Monitoring and PATH by nearest timestamp gives **1,338 (spot, premium) samples
inside live positions across 8 contracts**.

*Script: `backtesting/haema_signal/judas_broker_stop.py`*

---

## 1. The spot -> premium mapping is not stable enough to translate a stop

| contract | n | spot range | prem range | slope | R^2 |
|---|---|---|---|---|---|
| NIFTY18AUG2624300CE | 482 | 87.5 | 95.8 | +0.801 | 0.63 |
| NIFTY18AUG2624350CE | 255 | 42.4 | 76.0 | **+0.019** | **0.00** |
| NIFTY25AUG2624050CE | 38 | 29.3 | 19.1 | +0.645 | 0.97 |
| NIFTY25AUG2624200PE | 192 | 25.6 | 14.0 | -0.552 | 0.73 |
| SENSEX13AUG2677600CE | 119 | 175.9 | 116.9 | +0.672 | 0.94 |
| SENSEX20AUG2676900CE | 9 | 73.4 | 48.2 | +0.639 | 0.86 |
| SENSEX20AUG2677500CE | 72 | 245.2 | 196.2 | +0.856 | 0.98 |
| SENSEX20AUG2677600PE | 171 | 131.7 | 63.4 | -0.432 | 0.90 |

`|slope|` median **0.642**, range **0.019 to 0.856 -- a 46x spread**. R^2 median
0.88 is decent for most, but `NIFTY18AUG2624350CE` has **R^2 = 0.00**: premium
moved 76 points while spot moved 42 with essentially no relationship. That is an
IV/theta-dominated session, and it is precisely the day a translated stop would
sit in the wrong place.

## 2. Translation error at the stop is 14% of premium

Projecting each contract's premium at its own `sl_spot` using its own realised
slope, against the premium actually observed when spot came closest to that stop:

| contract | sl_spot | projected | actual | error | error % |
|---|---|---|---|---|---|
| NIFTY18AUG2624300CE | 24296.8 | 87.56 | 69.70 | +17.86 | **+25.6%** |
| NIFTY18AUG2624350CE | 24302.8 | 92.85 | 114.05 | -21.20 | -18.6% |
| SENSEX13AUG2677600CE | 77673.6 | 182.73 | 213.10 | -30.37 | -14.3% |
| SENSEX20AUG2676900CE | 76911.1 | 249.98 | 250.40 | -0.42 | -0.2% |
| SENSEX20AUG2677600PE | 77531.4 | 147.32 | 156.70 | -9.38 | -6.0% |

**|error| median Rs 17.86/unit = 14.3% of premium; worst Rs 30.37 = 25.6%.**
On a NIFTY lot of 65 that is about **Rs 1,161 per trade** of stop mis-placement,
and it goes BOTH ways -- +25.6% means the stop sits too high and exits early,
-18.6% means it sits too low and takes a deeper loss than intended.

Judas's measured mean outcome for the whole book is **+0.33R**. A 14% premium
error on the stop is not a rounding difference at that scale.

**Verdict on a translated stop: NO.**

## 3. The benefit is smaller than it looks

19 shutdowns. Three arrived with a position monitored seconds earlier
(2026-08-17 13:47, 2026-08-19 12:16, 2026-08-20 12:20) -- and **all three were
handled** by Judas's SIGTERM handler, which verifies the broker still holds the
position before closing it. In 19 sessions there is **no observed case** of the
in-process stop failing.

The real exposure is a **crash or SIGKILL**, where no handler runs at all. That
is not hypothetical on this box: the systemd journal shows
`State 'final-sigterm' timed out. Killing.` at both 12:20 and 14:32 on
2026-08-20. And the realised cost of having no resting stop is on record from
POV: on 2026-07-02, three SENSEX PE legs held 3+ hours **lost 75-80%** after
their stops were cancelled at a process restart.

## 4. What the evidence DOES support: a wide disaster stop

Keep the precise spot stop in-process; rest a deliberately WIDE premium stop
whose only job is to fire when the process is gone. For that to be free it must
sit beyond the worst adverse excursion any normally-managed trade reaches:

| contract | entry | min prem | worst drawdown |
|---|---|---|---|
| NIFTY18AUG2624300CE | 120.35 | 62.90 | **-47.7%** |
| NIFTY18AUG2624350CE | 122.40 | 62.95 | **-48.6%** |
| SENSEX20AUG2676900CE | 296.50 | 250.40 | -15.5% |
| SENSEX13AUG2677600CE | 230.10 | 196.70 | -14.5% |
| NIFTY25AUG2624200PE | 67.50 | 57.40 | -15.0% |
| NIFTY25AUG2624050CE | 163.65 | 150.00 | -8.3% |
| SENSEX20AUG2677600PE | 168.30 | 156.25 | -7.2% |
| SENSEX20AUG2677500CE | 482.00 | 489.55 | +1.6% |

Worst adverse excursion across all contracts: **-48.6%**. Median -14.7%.

| resting level | would have fired on |
|---|---|
| -40% | **2 / 8 contracts** (pre-empts the real stop -- rejected) |
| -50% | 0 / 8 |
| -60% | 0 / 8 |
| -70% | 0 / 8 |

**-50% is too tight**: it clears the worst observed excursion by only 1.4
percentage points, on a sample of 8 contracts. **-60%** keeps 11.4 points of
margin and still converts a potential 80-100% premium loss into a 60% one.

## Recommendation

1. **Do not translate the spot stop into a premium stop.** The mapping varies
   46x across contracts and breaks down entirely on IV-driven days; the median
   mis-placement is 14% of premium against a book that averages +0.33R.
2. **Do consider a resting disaster stop at -60% of entry premium**, as a pure
   crash backstop, with the spot stop unchanged in-process. On this evidence it
   never fires in normal operation, so its expected cost is ~zero and it caps the
   tail that actually hurt POV.

Three conditions before shipping it:

- **Must be stop-LIMIT, not SL-M.** SL-M is rejected outright for options
  (measured 33/33 on POV). A limit stop can fail to fill in a gap, so this is a
  mitigation, not a guarantee.
- **Must be cancelled on every normal exit.** An orphaned resting SELL becomes a
  naked short -- exactly the failure that produced POV's RECONCILE work and the
  9 orphaned lock files.
- **n = 8 contracts / 10 entries is a small sample.** The -60% level is chosen
  with margin precisely because of that, and should be re-checked once the give-
  back study reaches its 15-trade target.
