## [2026-08-24] fix(startup): the master-contract race killed 4 live instances

Renko was not armed today because both instances were DEAD from 09:16:02, and
PDH-PDL died at 09:20:09. One shared cause.

The flattrade master contract finished downloading at **09:20:50**. Anything
that queried the exchange master before that got HTTP 500 from BOTH
`optionchain` and `optionsymbol`, so lot-size detection returned nothing:

| instance | start | outcome |
|---|---|---|
| Renko SENSEX / MIDCPNIFTY | 09:16:00 | 500 at 09:16:02 -> `sys.exit(1)`, NO retry |
| PDH-PDL NIFTY / SENSEX | 09:10:00 | retried 600s from start, gave up 09:20:09 -- **41s early** |
| POV, Judas, collector | 09:20-09:45 | started after the master was ready, unaffected |

Standing down rather than guessing a size stays correct: on 2026-08-12 a
hardcoded guess produced 51 rejected orders across both books. The defect was
the DEADLINE -- anchored to process start instead of to the moment the size is
actually needed.

Fixes:
- Renko: the fatal exit is replaced by a retry loop whose deadline is the entry
  cutoff (`ENTRY_END`), sleeping in `nap(10)` slices so SIGTERM still lands. It
  would have waited 4m48s today and resolved.
- PDH-PDL: dropped the fixed `LOT_SIZE_WAIT_SECS` budget. In overnight mode the
  size is not needed until `ENTRY_TIME` (15:05), so the deadline is now that
  moment; intraday uses `EXIT_TIME`. The failure log names the deadline it
  missed, so a recurrence is not silent.
- Neither strategy can invent a size; `QUANTITY` remains the only override.

Also corrected `test_no_hardcoded_lot_fallback_in_source`, which pinned the
phrase "standing down rather than" and so reported a correct change as a
regression. It now asserts the invariant: no numeric literal in the resolution
path, override still documented.

Verified lot sizes resolve now: SENSEX 20, MIDCPNIFTY 120, NIFTY 65.
9 new tests in test/test_lot_size_wait.py; 300 pass. Deployed, md5-matched,
compiles on the VPS.

All four remain `is_scheduled=True` with no `manually_stopped` flag, so they
auto-start tomorrow at 09:10/09:16 with the fix in place.

## [2026-08-20] fix(locks): unify lock format, namespace & overnight staleness across all 4 strategies

Discovered that the cross-strategy lock system had three fundamental fissures:

1. **Format/Staleness Divergence**:
   - POV, Judas, and Renko wrote pipe-delimited `owner|ts|pid`.
   - PDH-PDL wrote JSON `{"strategy":..., "ts":..., "pid":...}`.
   - POV/Renko's parser split on `|` -> produced empty `ts` on JSON -> marked it STALE and reclaimed PDH's live locks.
   - Judas had NO staleness check at all -> leaked locks wedged contracts forever.

2. **Namespace & Granularity Mismatch**:
   - POV/Renko/Judas locked the **option contract** (`{symbol}.lock`) and claimed direction as `{und}.{slug}.{side}.dir`.
   - PDH locked the **underlying** (`{und}.lock` as an undocumented instance singleton) without locking the option contract, and wrote direction as `{und}_{side}.dir` (invisible to `{und}.*.dir` scans).
   - This meant PDH could hold NIFTY PE overnight while POV/Renko opened NIFTY CE next morning -- creating a delta-neutral straddle paying double premium and double spread.

3. **Overnight Carry Erasure**:
   - POV/Renko/Judas date-based staleness expired locks across midnight.
   - For an intraday strategy, this clears crashed leftovers. For PDH carrying overnight, it caused siblings to treat its active position as stale at 09:15.
   - Added a live-pid grace check: if the owner PID is alive and age < 24h, the claim is honored across session rollover.

**Changes applied**:
- Added `_read_lock()` helper in all 4 strategies handling both pipe and JSON formats defensively.
- Added staleness check + PID recording to Judas so leaked locks expire.
- Updated PDH-PDL to:
  - Acquire contract-level lock on the purchased option symbol (`sym`) on entry, release on exit/shutdown.
  - Write direction locks as `{und}.{slug}.{side}.dir` matching the shared namespace.
  - Separate its instance singleton lock (`acquire_instance_lock`).
- Added live-pid 24h grace to staleness checks across all strategies.
- Added `test/test_lock_interop.py` with 35 tests covering all cross-strategy locking & direction exclusion matrices. All 291 strategy tests + 7 CI tests pass.
- Deployed and verified on VPS across `scripts/` and `examples/`.

# Chronological Log

An append-only record of wiki updates, backtests, and VPS operations.

---

## [2026-08-19] bugfix | Judas monitored a ghost position for 18 minutes

- LIVE. A manual square-off closed `NIFTY25AUG2624050CE` at 11:58 (+Rs 520).
  Judas was still logging `Monitoring Trade` at 12:16.
- Root cause: `live_position_qty()` appeared **nowhere** except inside
  `if exit_triggered`. Judas is otherwise a pure spot watcher, so a close it
  did not initiate was invisible. POV had `sync_positions_with_book`; Judas had
  no equivalent.
- Consequences: symbol lock held, session blocked, and the +Rs 520 never
  reached the books or the circuit breakers.
- Not a money risk: every SELL path (exit + shutdown) already verifies the
  broker holds the position. Verified live -- SIGTERM logged
  `broker reports ... qty=0 - already flat, no SELL` and placed no order.
- Fix 1: `detect_external_close()` on positive evidence only -- entry
  rejected/cancelled -> never a position; broker flat + entry complete ->
  externally closed; flat + entry undetermined -> 3 consecutive misses;
  positionbook unverifiable -> no decision (the 2026-08-14 lesson).
  Throttled to RECON_SECS=30 (in-trade poll is 5s).
- Fix 2: `find_external_exit_price()` reads the closing SELL from the broker's
  tradebook, then orderbook. Tracking closes even when unpriced -- a permanent
  ghost is worse than an unpriced trade.
- Fix 3 (latent, made reachable by Fix 1): `state = "DONE"` was memory-only, so
  a mid-session restart came up IDLE and could open a SECOND trade on a day
  Judas had already traded. Added `persist_done()`/`load_done_date()`; boot
  reads the marker before `persist_trade({})` can erase it.
- Verified against the real ghost on the live broker: exit 167.75 reconstructs
  gross Rs +520.00 exactly (matching the broker), net Rs +468.29 after cost.
- End-to-end on the VPS: booted the real strategy with today's marker ->
  `Already traded today (2026-08-19) - standing down`, zero orders placed.
- 31 new tests, 171 pass. Deployed judas 418e4461 (md5 verified both dirs).

## [2026-08-20] review | all 3 strategies: protection missing on the RECOVERY path

Reviewed the session's changes across Judas, POV and Renko. Three defects, all
the same shape: the protection I added exists on the happy path and is absent on
the restart/recovery path.

1. **Judas -- an adopted position had no resting backstop.** The restored-context
   branch carried a `disaster_oid` that may long since have been cancelled or
   filled, and the unknown-orphan branch built a fresh dict with NO oid at all --
   so after a restart the position ran with only its in-process spot stop while
   the code believed a backstop was resting. Adoption now checks `orderstatus`
   for "open / trigger pending / pending" and re-arms when it is not live,
   pricing off entry_fill_price -> entry_opt_price -> the broker's own average
   for an unknown orphan. Warns explicitly when no backstop can be armed.
   (POV already did this for its SL; this is the same requirement.)

2. **Renko -- adoption never re-acquired the locks.** Our previous pid is dead,
   so `_pid_alive` marks the old lock stale and any sibling may take it. POV
   could therefore open the OPPOSITE direction on the same underlying while
   renko still held a live position -- precisely the paid straddle the direction
   lock exists to prevent. Adoption now re-claims both contract and direction
   locks and logs "managing to exit only" if either is held elsewhere.

3. **Renko -- `daily_pnl` was dead state.** Accumulated in two places, never
   read: accounting that looked like accounting. Now emits one
   `SESSION <date> closed | trades N | realised Rs X | losses Rs Y | streak Z`
   line on rollover, so it is auditable against the shadow CSV and the broker's
   realised P&L. Only logged when the session actually traded.

Also fixed a test that had become ambiguous: `test_oid_is_persisted...` anchored
on the first `place_disaster_stop` call site, and there are now two (entry and
adoption). It asserts both arming sites persist the oid, since a restart cannot
cancel a resting order it does not know about.

256 strategy tests + 7 CI-safe tests pass. Deployed judas de13468ed6,
renko 5566db865c, pov e086ebacba3 -- md5 verified across scripts/ and examples/,
all three compile on the VPS.

## [2026-08-20] audit | POV: stated R:R is not actual R:R (logged, not corrected)

- Asked whether POV shares Judas's stop problems. It does NOT share the one that
  prompted the question, and it has a different one.
- **Not wrong**: POV's `sl = round(lo, 2)` is the OPTION candle's own low, i.e.
  already a premium level, so it rests natively with no spot->premium translation.
  Verified live: 6 SL orders armed, all triggers 0.05-tick aligned, 0 rejections,
  0 UNPROTECTED events. It also already re-arms the stop on restart adoption and
  logs UNPROTECTED at ERROR on failure. That machinery is sound.
- **Wrong**: sl/t1/t2/t3 are computed from the SIGNAL CANDLE CLOSE and never
  re-derived from the fill. Recovering the signal close from t1=e+1.5(e-sl) over
  the first 6 live entries: fills deviated -15.8% to +4.1%, so T1 -- believed to
  be 1.50R always -- actually landed between **0.72R and 6.36R**, and on **2 of 6
  trades T1 paid LESS than the stop risked**.
- Same class as Judas's MIN_EFFECTIVE_RR bug (stated R:R != actual R:R) but via
  fill slippage rather than stop flooring. Judas is structurally immune to this
  variant because its geometry is in SPOT, which an option fill cannot move.
- Also: 2 of 6 entries stopped only ~2.5% from premium, against a measured ~0.41%
  spread -- about six spreads of room.
- NOT corrected. POV is the only positive-expectancy strategy (+Rs 108/trade, 62%
  win) and earned that WITH this geometry; moving its targets is an untested
  change to the one thing that works. Same reasoning as the POV ratchet question:
  instrument first.
- Added GEOMETRY / GEOMETRY INVERTED logging at entry, plus a guard so a stop at
  or above the fill warns instead of dividing by zero. Wrapped so a diagnostic
  can never affect the position just opened.
- Decision point: at ~15 GEOMETRY lines, compare outcomes for inverted (<1R) vs
  normal geometry, then decide whether to re-derive from the fill.
- 10 new tests, 250 pass. Deployed pov e086ebacba3d (md5 verified, compiles).

## [2026-08-20] ship | Judas resting disaster stop at -60%

- Implemented the recommendation from the broker-stop study. The spot stop and
  the break-even ratchet are UNCHANGED -- this is a backstop, not a replacement.
- DISASTER_STOP_PCT=60 (of the actual entry FILL, not the quote),
  SL_LIMIT_BUFFER_PCT=5. stop-LIMIT never SL-M (rejected 33/33 for options, and
  the API reported it as success with orderid=null). Order id persisted so a
  restart can cancel it.
- Cancelled on every exit path: normal exit, external close, shutdown-flat,
  shutdown-close. An orphaned resting SELL is a naked short.
- Deliberately LEFT ARMED when the positionbook is unverifiable at shutdown, and
  when the shutdown close fails -- those are the cases it exists for.
- Unarmed backstop now logs at WARNING. Silence was the original failure mode.
- Edge case caught by tests: a 0.10 entry premium gives a raw trigger of 0.04
  which TICK-ROUNDS UP to exactly 0.05, slipping past a post-rounding `< 0.05`
  guard and arming a meaningless "sell at almost zero" stop. Guard now runs on
  the raw pre-rounded value and requires >= 0.10.
- Also fixed a pre-existing test whose 600-char slice window no longer reached
  persist_done(today) after the cancel block was inserted; widened to 1400 since
  the assertion is about presence, not proximity.
- 17 new tests, 240 pass. Deployed judas 4a03a7c2e6 (md5 verified both dirs,
  compiles on VPS). Effective at tomorrow's 09:45 start. No resting orders
  currently open; judas not running (schedule 09:45-15:20).

## [2026-08-20] study | resting broker stop for Judas -- translated NO, disaster stop YES

- Question: should Judas rest its stop at the broker like POV does? A resting
  order cannot watch SPOT, only premium, so the real question is whether Judas's
  spot stop can be translated to a premium stop accurately enough.
- Evidence: 19 live sessions, 6,493 Monitoring lines (spot+stop), 1,345 PATH
  lines (premium), paired by timestamp -> 1,338 (spot, premium) samples inside
  live positions across 8 contracts.
- **Mapping is not stable**: |dPrem/dSpot| median 0.642, range 0.019-0.856 = a
  46x spread. One contract (NIFTY18AUG2624350CE) had R^2 = 0.00 -- premium moved
  76 pts while spot moved 42, i.e. IV/theta dominated.
- **Translation error at the stop**: median Rs 17.86/unit = 14.3% of premium,
  worst 25.6%. ~Rs 1,161/trade on a NIFTY lot, and BOTH directions (+25.6% exits
  early, -18.6% takes a deeper loss). Judas's mean outcome is +0.33R, so that is
  material. -> translated premium stop REJECTED.
- **Benefit is smaller than assumed**: of 19 shutdowns, 3 arrived with a live
  position and ALL 3 were handled by the SIGTERM handler. No observed in-process
  stop failure. Real exposure is crash/SIGKILL -- which is real on this box
  (journal: "final-sigterm timed out. Killing." at 12:20 and 14:32 today) and
  whose realised cost is POV's 2026-07-02 incident (3 legs, 3+ hours, -75-80%).
- **What the evidence supports**: a WIDE disaster stop, spot stop unchanged.
  Worst adverse premium excursion on any contract -48.6%, median -14.7%.
  A resting stop at -40% would have fired on 2/8 (pre-empting the real stop);
  -50% on 0/8 but only 1.4pp of margin; **-60% on 0/8 with 11.4pp margin**.
  Recommend -60% as a crash backstop: ~zero expected cost, converts an 80-100%
  tail into 60%.
- Conditions before shipping: stop-LIMIT not SL-M (SL-M rejected 33/33 on
  options); must be cancelled on every normal exit or it becomes a naked short;
  n=8 contracts is small, re-check at the 15-trade give-back target.
- NOT implemented -- Judas is live money and this is a stop-architecture change
  on n=8. Analysis only, awaiting a decision.

## [2026-08-20] harden | renko: cross-strategy locks + circuit breakers

- Renko went live with neither locks nor breakers, while POV SENSEX trades the
  same underlying. Both added, deliberately copying the EXISTING semantics rather
  than inventing new ones.
- **Breakers** (same defaults as Judas and POV): LOSS_STREAK_LIMIT=3,
  DAILY_LOSS_LIMIT_RS=10000. Behaviour copied from POV, not Judas: halt NEW
  ENTRIES but keep managing an open position. Judas sets state=DONE, which is
  equivalent for a one-trade-per-day strategy but would abandon a live position
  here. Losses feed the breakers only on a FULL exit -- a T1 part-book is not a
  closed trade.
- **Locks**: byte-compatible with POV/Judas on purpose -- same dir, same
  filenames, same `owner|iso|pid` body. A private scheme would coordinate with
  nothing. Contract lock per option symbol; DIRECTION lock per underlying, which
  is the one that matters (holding CE while POV holds PE is a delta-neutral
  straddle paying double premium for no view). Shadow instances take no locks so
  they can never block a live sibling. Released on every path: full exit, EOD,
  rejected entry, shutdown, and new session.
- **Finding: THREE incompatible lock conventions exist in this repo.**
  PDH-PML EMA writes a JSON body with `{UND}.lock` and `{UND}_{SIDE}.dir`;
  POV/Judas/renko use a pipe body with `{option_symbol}.lock` and
  `{UND}.{slug}.{SIDE}.dir`. The two schemes cannot see each other, so renko
  coordinates with POV/Judas but NOT with PDH/PDL. Overlap is small in practice
  (PDH exits 09:20-09:30, renko's first entry is 09:30+), so this is recorded
  rather than refactored the night before trading.
- 223 tests pass. Deployed renko 7c834cb46f (md5 verified both dirs, compiles on
  VPS).

## [2026-08-20] review | 4 real defects in the just-deployed LIVE renko strategy

Reviewed the code I had promoted to live the same hour. Four money-losing
defects, all now fixed with tests. Recording them because three were bugs I had
already fixed ELSEWHERE and reintroduced in a new file.

1. **CRITICAL -- part-lot exits could never fill, and broke the STOP.**
   The backtest books half at T1. An option order must be a whole multiple of the
   lot size, so `int(20 * 0.5) = 10` on SENSEX is rejected ("Quantity must be in
   multiples of lot size 20" -- the 2026-08-12 failure). The old code then did
   `pos["qty"] -= 10`, leaving 10, after which EVERY later exit **including the
   stop-loss** was a non-multiple and also rejected: the position would have run
   unprotected to broker auto-squareoff.
   Fixed: `CAN_SPLIT = MAX_LOTS >= 2`; at 1 lot T1 is skipped and the position
   rides to T2. Measured on the validated harness with entries held IDENTICAL
   (T1_RR stays 2.5 because it feeds the room gate): whole lot at 3.0R is equal
   or better -- NIFTY +68,517 vs +65,371, MIDCPNIFTY +128,769 vs +116,634,
   SENSEX +10,937 vs +11,575.
   Also caught during that measurement: varying T1_RR changes ENTRIES (room
   gate), so naive exit variants were not like-for-like -- trade count moved
   868 -> 765. And SENSEX has only 39-56 local trades, far too few to retune an
   exit on.

2. **CRITICAL -- acceptance treated as a fill.** The entry recorded a position
   from the pre-trade QUOTE and armed SL/T1/T2 without checking the order
   filled. This is exactly the POV phantom-entry bug fixed on 2026-08-14,
   reintroduced. Added `confirm_entry_fill()`: rejected/cancelled -> no position
   and no stop armed; complete -> book the real average (a fill with an
   unreadable average is STILL a fill); unknown -> track as live, because an
   untracked real position with no stop is worse than a phantom.

3. **HIGH -- no state persistence, so a restart orphaned a live position.**
   STATE_FILE was declared and never written. This box restarted three times on
   2026-08-20 alone. Added persist/load plus boot-time orphan adoption with the
   broker authoritative on size, and seeded `trade_day` so the loop's new-day
   reset does not wipe the adoption on its first pass. Shutdown now verifies the
   broker still holds the position before selling (never a naked short) and keeps
   the snapshot when it cannot.

4. **MEDIUM -- SIGTERM ignored for up to 5 minutes.** The off-hours guard used
   `time.sleep(300)`, so a SIGTERM'd instance was still alive 33s later (observed
   on pid 1351230). Under schedule_stop's SIGTERM plus TimeoutStopSec that
   guarantees a SIGKILL -- the same unclean-shutdown class fixed at the systemd
   level earlier today. Added `nap()`, which sleeps in <=2s slices.

Also: `pkill -f scripts/renko_engine_strategy` over SSH matches the remote
shell's own command line and kills the session (three exit-255s). Filter on
process name instead.

State: 9 registrations, all scheduled; Judas and PDH-PDL `manually_stopped` has
cleared so they auto-start tomorrow too. No processes running (schedule_stop
15:20), no stale state files. 214 tests pass. Deployed renko c32a0eede6.

## [2026-08-20] deploy | Renko Engine promoted to LIVE on SENSEX + MIDCPNIFTY

- User explicitly confirmed: make both registrations genuinely LIVE.
- Checked capital first: SENSEX ATM CE ~Rs 1,420/lot (lot 20), MIDCPNIFTY
  ~Rs 9,372/lot (lot 120, monthly) against Rs 31,420 cash. Book flat (0 open).
- Promoted via `scripts/promote_renko_live.py`:
  Renamed "Renko Engine (SENSEX) SHADOW" -> "Renko Engine (SENSEX)"
  Renamed "Renko Engine (MIDCPNIFTY) SHADOW" -> "Renko Engine (MIDCPNIFTY)"
  env: {"DRY_RUN": "false"} on both
  Cleared manually_stopped
- App restarted with TimeoutStopSec=75 -- clean stop (inactive, port free),
  restart verified with single gunicorn master 1281408, HTTP 200.
- SENSEX came up automatically (was running pre-stop) and log confirms:
  `[WARNING] LIVE MODE -- real orders. lot=20 lots=1 qty=20` (pid 1287524).
- MIDCPNIFTY registered live (`is_running=False`), scheduled start tomorrow 09:16.
- POV x2 and greeks collector relaunched and running.
- Restated the risk profile for the record: SENSEX is profitable in 2 of 7 months
  on real premiums with March 2026 carrying the whole result; MIDCPNIFTY has zero
  real-premium validation (unsupported on Volrix, monthly only, 1.94x hurdle).

## [2026-08-20] ops | Renko registered in the UI; restart hazard found and fixed

- User: "strategies are not visible after restart", then "integrate the renko
  strategies in the UI". Three separate causes, none of them data loss.
- **Nothing was lost**: strategy_configs.json was intact with all 7 entries the
  whole time. An empty UI list is the session -- /python/api/strategies is behind
  @check_session_validity, which 401s the SPA after a restart.
- **Judas x2 and PDH/PDL x2 carry `manually_stopped: True`**, and
  python_strategy.py:1160 makes the scheduler SKIP auto-start on that flag. Only
  pressing Start in the UI clears it (1822-3), so they stay down across every
  restart. Left alone -- four real-money strategies, flag may be deliberate.
- **Real bug: restarts were unclean.** `TimeoutStopSec=20` while every strategy
  polls on a ~20s cycle, so they needed up to 20s just to NOTICE SIGTERM.
  journal showed "State 'final-sigterm' timed out. Killing." then "Found
  left-over process in control group while starting unit" at both 12:20 and
  14:32 -- systemd was starting a second app instance beside an orphan, which is
  exactly how state goes inconsistent across a restart. Raised to 75s (backup
  kept). The subsequent stop/start was verified clean: port free, no leftovers,
  ONE gunicorn master.
- Registered both renko instances -> **9 registrations, "Restored 9 scheduled
  strategies"**, visible in the UI. Shadow is belt-and-braces: env DRY_RUN=true
  AND "SHADOW" in the registration name (the only channel that survives the
  UI-only upload path).
- Retired the standalone path first (systemd renko-shadow.timer disabled,
  processes killed, pidfiles removed) so the platform scheduler cannot end up
  running duplicates.
- They start tomorrow 09:16 -- the boot path only relaunches strategies that
  were already is_running, and today's entry window closed at 15:00 anyway, so
  running the last 5 minutes had no value.
- Caveat recorded: log/openalgo.log grows ~67 KB/s (~240 MB/h) from `data: Raw
  Response` INFO lines dumping full broker payloads. The hourly logcap timer
  stops the disk filling; the verbosity is the real fix and is untouched.

## [2026-08-20] deploy | Renko Engine SHADOW on SENSEX + MIDCPNIFTY (intraday only)

- User asked to deploy SENSEX and MIDCPNIFTY for forward testing, overruling my
  MIDCPNIFTY caution (no real-premium evidence). Deployed both.
- **Shadow, not live.** analyzer is a single GLOBAL flag and POV is live with
  real money, so sandboxing globally would paper-trade the one profitable
  strategy. Used the in-strategy DRY_RUN pattern instead: signals and P&L are
  computed from LIVE option quotes, no orders, no locks, no capital. DRY_RUN
  defaults TRUE here (inverted vs the other strategies) because SENSEX carries
  its whole backtest in one month and MIDCPNIFTY has zero real-premium checks.
- **Intraday only** (user requirement): PRODUCT pinned to MIS, not env-readable,
  no NRML path in code. Found and fixed a real flaw doing this -- the EOD
  square-off sat BEHIND the once-per-bar gate, so a stalled feed after 15:15
  would have carried a position overnight. Hard clock-driven exit now runs every
  poll, above the bar gate, and the off-hours idle guard is `pos is None`-gated
  so it can never skip that exit.
- Live bug found and fixed during bring-up: the 1m feed carries a PRE-OPEN
  artefact (flat 09:00 SENSEX candle o=h=l=c=77468.45) which resampled into its
  own 15m bucket and became bar[0], making the X candle DEGENERATE
  (x_high == x_low -> x_44 == x_56), silently neutering the X-band zone block
  and the close>x_56 filter. Now filtered to 09:15..15:30 before resampling.
  After the fix: SENSEX X 77375.8-77494.8, MIDCPNIFTY X 14906.3-14960.8.
- Lot sizes auto-detected correctly: SENSEX 20, MIDCPNIFTY 120, two-source with
  fail-closed (no hardcoded guess -- the 2026-08-12 lesson).
- Launcher is pidfile-idempotent; a pgrep check could not work because the
  command line carries no UNDERLYING and would have spawned duplicates of both.
  systemd `renko-shadow.timer` (OnBootSec + Mon..Fri 09:16 IST) for resilience;
  the strategy idles overnight and resets per session, so one launch persists.
- Live platform strategies untouched -- no app restart, POV still live
  (analyze_mode = 0). Running: POV x2, greeks collector, renko shadow x2.
- 30 new tests (201 total). Deployed renko_engine_strategy d7532f99.
- PASS CONDITION (pre-registered): profitable in a MAJORITY of forward months,
  counted from log/strategies/renko_shadow_<UND>.csv. One month in seven is what
  the backtest already gives and is not enough.

## [2026-08-19] decision | which indexes to forward-test -- SENSEX only

- Question: drop NIFTY and deploy the other indexes? Mostly no, and the reason
  is structural.
- Measured the model's two key inputs against real Volrix fills:
  realised |dOpt| per favourable index point = **0.52** (not the assumed 0.358 --
  model understated rupees/point by 46%, which is why SENSEX real beat model);
  MONTHLY ATM premium = **1.469% of spot** (n=138 real fills) = **3.26x** the
  0.45% weekly figure the model applied to all five indices.
- BANKNIFTY / FINNIFTY / MIDCPNIFTY have no weeklies, so monthly is their only
  instrument. Repricing each against its own correct hurdle:
  NIFTY 3.96x, SENSEX 8.07x, MIDCPNIFTY 1.94x (was 4.36x), FINNIFTY 0.85x,
  BANKNIFTY 0.54x. Monthly capital is ~Rs 20-22k/lot vs ~Rs 7k weekly.
- Validation of the correction: BANKNIFTY old model +Rs 7,388, corrected model
  -Rs 52,601, real premiums -Rs 44,870 (PF 0.80). The corrected inputs get the
  sign right; the old ones did not.
- DECISION: paper forward-test **SENSEX only, 1 lot**. NIFTY no (real PF 0.80,
  but decay not economics -- cheapest hurdle at 1.29 pts, revisit on regime).
  BANKNIFTY no (rejected by corrected model AND real premiums). FINNIFTY no
  (0.85x, below hurdle). MIDCPNIFTY NOT YET -- 1.94x and +Rs 106k look fine but
  there is zero real-premium evidence, it is monthly-only, and Rs 20,328/lot.
  Deploying it would be acting on model-only evidence.
- Fixed LOT["FINNIFTY"] 65 -> 60 (live symbol master); overstated FINNIFTY
  rupees by ~8%, changes no verdict. NIFTY regression still 435/746/1407.

## [2026-08-19] correction | matched windows: the delta model was RIGHT

- User challenged the section-7 conclusion ("OpenAlgo showed strong results
  across all indexes"). Correct challenge -- section 7 compared a 3.3-year
  offline run against a 6-month Volrix run and blamed the delta translation.
- Matched window (2026-02-20..2026-05-27), same config:
  NIFTY model -Rs 75 vs real -Rs 5,902 (PF 1.06 vs 1.00);
  SENSEX model +Rs 6,383 vs real **+Rs 25,129** (PF 1.23 vs 1.30).
  The delta model (0.358, premium 0.45%) is ACCURATE on NIFTY and PESSIMISTIC
  on SENSEX. My "premium decay kills it" attribution was wrong.
- Decomposition of the NIFTY gap: regime/window (Jun-Aug 2026, outside offline
  data) -Rs 42,631; premium translation error, matched window -Rs 5,827.
  ~88% was regime, not premiums.
- Also: the offline model does NOT claim a strong NIFTY result on that window --
  70 trades, avg +1.92 pts/trade against a 1.87 friction hurdle, model -Rs 75.
  The +Rs 65,371 headline is a 3.3-year number.
- Real reason it still fails, now measured on real premiums:
  NIFTY total -48,533, March alone +42,911, without March **-91,444**, 2/7 months positive.
  SENSEX total +18,483, March alone +67,965, without March **-49,482**, 2/7 months positive.
  SENSEX's whole positive result is ONE month. Same concentration the index-point
  run found (34/996 trades = 97% of net), now visible monthly in a second engine.
- Jun-Aug 2026 (most recent data): NIFTY -42,631, SENSEX -12,838.
- Verdict unchanged (not deployable) but the reason is concentration + recent
  decay, NOT measurement error. The delta translation is validated for reuse.
- Falsifiable condition to revisit: SENSEX profitable in a MAJORITY of forward
  months at 1 lot, rather than one month in seven.

## [2026-08-19] gate | Renko PRO on REAL option premiums -- rejected

- Ran the exit-tuned config on Volrix real ATM premiums, 2026-02-20..2026-08-19,
  15m, Rs 2L, slippage 0.25% (measured live) + transaction costs.
- NIFTY weekly: n=160 win 29.4% **PF 0.80 net -Rs 48,533** maxDD -88,502.
  Raw (no slip/cost) already -Rs 38,600 -- the gross book loses.
- NIFTY skip DTE-0: -Rs 31,037, maxDD -62,305, **PF still 0.80**. Expiry-day
  theta is ~1/3 of the damage, not the cause.
- BANKNIFTY monthly: PF 0.80, -Rs 44,870.
- SENSEX weekly: **PF 1.10, +Rs 18,483**, Sharpe 0.74 -- the only positive, but
  net/maxDD = 0.36 (25% DD to earn 9%); at 0.5% slip it is 0.21. Not deployable.
- Delta model was badly optimistic: index points said NIFTY +Rs 65k over 3y with
  entry beating the strong null at z=2.53. Example of the gap:
  NIFTY02MAR2624600PE bought 50.20, exited 9.20 (-82%) on expiry day.
- **Index question answered, and my section-6b ranking corrected**: BANKNIFTY,
  FINNIFTY and MIDCPNIFTY have NO weekly options (chain on 2026-03-04 shows
  BANKNIFTY nearest = monthly 2026-03-30). MIDCPNIFTY, ranked "best" on index
  points, cannot run this strategy at all. Only NIFTY and SENSEX can; of those
  SENSEX wins, agreeing with the scale-free hurdle ranking (5.55x vs 2.73x).
- Two of my own port bugs recorded: (1) I misread zero trades -- the Volrix
  trades payload is double-nested; (2) managing exits on 1-min spot in
  minTrigger (more precise than the 15m offline engine) produced ZERO exits,
  carrying positions to run end and blocking entries -- 1 entry per 10 sessions.
  Fixed by managing on the 15m bar in onCandleClose, which is also faithful.
- Six price-pattern methods now tested, six rejected. POV (OI/positioning, not
  price geometry) remains the only positive-expectancy live strategy.

## [2026-08-19] correction | Renko PRO -- the entry was fine, the exit was not

- Reverses the same-day "no edge" call. Two defects, both flagged by the user:
  the port hardcoded the Pine's shipped exits (stop = prev candle, T2 = Renko --
  the Pine's own worst target at 5.8% fill), and the null randomised entry
  TIMING only while inheriting the strategy's day, direction and EMA side.
- Symptom I had misread: 34 of 996 T2s filled yet carried 97% of net points.
  That is an unreachable target, not a bad entry.
- Swept the EXIT surface with entries frozen at engine defaults: 4 SL types x
  7 target modes x T1 x 4 trail modes x max-trades/day x cooldown = 12,096 runs.
- Trade count priced in per user request: selection on net RUPEES after friction
  (Rs 43.72/round trip = 1.87 pts). Points ranking gives 58.6% of configs
  positive; rupees gives 30.9%. Friction flips a quarter of the grid.
- Winner 15m: SL prev candle, T1 2.5R books 50%, T2 resolves to 3.0R, no trail,
  max 2 trades/day. Top 12 cluster on the same geometry.
- G1 OOS +Rs 17,236 PASS | G2 transfer 4/4 PASS | G3b STRONG null (random day +
  direction + timing) real +Rs 65,371 vs null mean -Rs 13,631, z=+2.53, 1/200
  beat it, PASS | G4 friction PASS | G5 top-5% FAIL.
- G5 is the wrong test for a 39.7%-win 2.5R book (skew +1.11): trimming the top
  5% of any right-skewed payoff looks fatal. Fair replacements: bootstrap 95.9%
  of 5,000 resamples profitable with 5th pct +Rs 2,959; 9/13 quarters positive;
  equal-trimmed real -2.90 vs null -6.05 pts/trade, z=+1.80 (better, not
  significant).
- Revised verdict: thin real borderline edge. Forward-test 1 lot, do not scale.
  Worst quarter (2026Q2, -Rs 12,269) is also the most recent.
- REQUIRED before deployment: real option premiums on Volrix. Everything here is
  delta-translated index points, which is exactly where Red Bar and Stochastic died.

## [2026-08-19] research | Renko PRO parameter sweep -- pre-registered protocol

- Swept 1,728 configs (576 params x 5/15/30m) on NIFTY in-sample only, with the
  selection rule and five validation gates fixed in the script beforehand.
- **99.0% of configs were profitable in-sample** (1,708/1,726). Noise gives ~50%,
  so IS ranking carries almost no information for this strategy family.
- Winner (15m, brick 1.00%, T1 2.5R) switched OFF both signature gates --
  confluence and the trend cloud.
- Passed G1 OOS (+755 pts), G2 transfer (bare 2/4), G4 friction (+Rs 76,705),
  G5 top-5. **Failed G3, the random-entry null**: nulls average +3,040 pts vs
  +5,167 real, z(pts) +1.78 / z(Sharpe) +1.10, and 7/200 seeds beat it outright.
- Added `entry_override` to the faithful port so the null reuses the IDENTICAL
  exit engine; regression-checked that 30m/15m/5m reproduce 435/746/1407 trades
  and identical net points.
- Money source: EOD +18,072 and SL -17,913 nearly cancel; 34 T2 hits (3.4%)
  carry 97% of net. Excluding T2: +0.17 pts/trade against a 1.88-pt friction
  breakeven = -Rs 38,355.
- G5 was mis-specified (top-5 = 0.5% of 996 trades vs 1.1% of the earlier 435).
  Post-hoc G6 (top 5% vs friction hurdle) fails at -3.58 pts/trade; recorded as
  post-hoc since G3 had already decided it.
- Verdict: no edge. Fifth price-pattern method to die the same way.

## [2026-08-16] bugfix | RECONCILE cancelled stops on two LIVE positions (14-Aug)
- 14-Aug ran LIVE. All three prior fixes held: lot sizes correct (65/20, zero rejections vs 51 on 12-Aug), greeks collector 1,356 option rows (was 0), TAPE quadrant tagging live on 7 entries.
- Recorded P&L +Rs 1,661 (Judas 24300CE +2,507 target; POV SENSEX 78000CE +352 target; POV 24450CE -714 max-hold; POV 24400CE -483 SL).
- BUT: POV opened 3 SENSEX legs at 12:49 and RECONCILE pruned all three, cancelling their stops. Only one was correct:
    77800CE entry REJECTED          -> prune correct
    78100CE entry COMPLETE @ 333.05 -> LIVE position, stop cancelled
    77900CE entry COMPLETE @ 433.95 -> LIVE position, stop cancelled (stop 420.1, leg trading 426-436 at prune time)
- Both ran unprotected to broker MIS auto-squareoff. P&L never reached the books or the circuit breakers. Exits unrecoverable (broker serves current session only; those thin strikes have no candle history).
- Second occurrence of this failure mode; July lost 75-80% on three legs the same way via a different trigger.
- FIX: sync_positions_with_book now requires POSITIVE evidence. entry rejected/cancelled -> prune. SL complete -> prune. entry complete + SL live -> DISCREPANCY, keep position AND keep stop armed, log error. Undetermined -> RECON_MISS_LIMIT (3) consecutive misses before touching a stop. entry_orderid now stored on the position so the check is possible at all.
- Judas and PDH/PDL checked: neither cancels a stop on a passive book miss. POV only.
- 9 new tests reproducing the exact 14-Aug scenario; 132 pass. Deployed pov eb149dbf.
- FOLLOW-UP FIX (same day): POV treated order ACCEPTANCE as a fill. 77800CE was accepted then rejected by the exchange; fetch_fill_price() returns None for both "rejected" and "unreadable", so the caller fell back to the pre-trade quote, armed a stop and logged "Trade entered ... Opt entry: 499.45" for a position that never existed. New confirm_entry_fill() returns complete/dead/unknown: dead aborts the entry with no stop and no position; unknown is deliberately treated as LIVE (an untracked real fill is worse than a phantom, and RECONCILE now settles it). 8 more tests, 140 pass. Deployed pov c073b360.

## [2026-08-12] research | OpenMTOps upstream review + OI feed verification
- Reviewed CApsUNlocked123/openmtops pov_engine.py: our POV port is faithful, every constant matches (PRE 50k, C2 30k, 5/5 gate, 1.5/3/5R targets). Upstream sources OI from the candle feed, same as us.
- Verified the optionchain 404 did NOT degrade OI scoring: optionchain was only ever called in fetch_lot_size. Cross-checked history() OI against the collector's independent quote path (NIFTY25AUG26FUT, 312 minutes): mean abs diff 2,128 = 0.017% of OI, early samples exact. Feed is trustworthy.
- Scoring improved that day (5,148 polls vs 4,247) and POV hit 5/5 three times -- first STRONG signals in 3 days -- all killed by the lot-size bug, not by data.
- narrative.py (730 lines, 7 templates): descriptive not predictive, no backtest, no edge claim. Half of it needs IV which history() does not return. Verdict: do not port the narration layer; the one cheap use is annotating existing trades with the OI x price quadrant as diagnostic context for the give-back study.
- SEPARATE BUG: greeks_collector has ZERO option rows across 08-10/11/12 -- only spot and futures. The greeks columns are empty because no option leg is sampled. It is not serving its stated purpose.

## [2026-08-12] bugfix | lot-size detection sent invalid quantities in analyzer
- Symptom: trades fired but never reached sandbox positions/trades. 51 rejections: "Quantity must be in multiples of lot size 65" (NIFTY) / "... 20" (SENSEX).
- Cause: fetch_lot_size() had ONE source, client.optionchain(), which returned 404 "No strikes found ... update master contract" all session on BOTH indices despite the master holding 462 CE rows for that expiry. Failure is INTERMITTENT (same call worked 2h later).
- On failure the code fell through to a hardcoded `QUANTITY = 75` -- NIFTY's lot size before the 2025-12-31 change to 65, never correct for SENSEX (20).
- Only SENSEX orders on 08-11 reached sandbox (4, all complete); every NIFTY order died at validation.
- Fix: second independent source -- optionsymbol() returns lotsize at the top level and is the same endpoint the strategies already call every cycle to resolve their leg. Removed the 75 guess; both strategies now stand down rather than size a trade they cannot size.
- Verified live: NIFTY 65, SENSEX 20 through both sources. 14 new tests, 61 total pass. Deployed judas 4f00cd76, pov ad27ecd3.
- NOTE: red_bar / ha_ema / regime_momentum carry the same `QUANTITY = 75` fallback but are de-registered. Fix before any re-registration.

## [2026-08-10] research | Variance Risk Premium -- selling vs buying
- Researched (agent-reach/web): documented statistically significant positive VRP in Indian index options; implied variance systematically exceeds realized. All five prior strategies BOUGHT options and paid it.
- Volrix test 1, short ATM straddle 30% SL: NIFTY PF 0.90 -Rs 22,921; SENSEX PF 1.00 -Rs 16,466. Win rate 38% = stop firing on noise, mis-specified.
- Volrix test 2, iron fly (defined risk, no stop): NIFTY PF 1.00 -Rs 1,261 Sharpe -0.09 maxDD 13.2%; SENSEX PF 1.00 -Rs 3,392 maxDD 8.4%. Drawdown halved, Sharpe near zero.
- Finding: VRP edge is real and almost exactly consumed by 4-leg friction. avgWin +1,548 vs avgLoss -1,550 on NIFTY.
- Six strategies now land between PF 0.70 and 1.00. Binding constraint is friction and capital, not signal.
- Selling needs ~Rs 1.5-2L margin/lot -- not accessible at live balance regardless.

## [2026-08-10] research | Stochastic Crossover (SKB) -- OpenAlgo + Volrix
- Analysed the SKB chart: Stochastic (14,3,3), buy on %K/%D cross up from <20, sell on cross down from >80, NIFTY 15m.
- OpenAlgo engine: chart defaults lose on every timeframe (15m PF 0.88, Sharpe -3.77).
- Swept 162 NIFTY configs; only 16 profitable. All top 12 used the `range` regime filter, validating the chart's own "works best in sideways markets" caveat.
- Best NIFTY config (30m z30 range rr3.0): PF 1.21 overall but IS +Rs 383,892 vs OOS -Rs 10,106, maxDD 189.9% of capital. Key parameter inverts on SENSEX (its champions used `none`); SENSEX champion loses -Rs 535,014 on NIFTY.
- Volrix (REAL option premiums, 6-month plan limit): NIFTY n=51 PF 0.90 -Rs 6,796 Sharpe -0.86; SENSEX n=54 PF 0.70 -Rs 21,714 Sharpe -2.16.
- Verdict: do not deploy. Two engines agree.

## [2026-08-07] operations | retired four strategies
- Stopped `openalgo` service and removed four registrations from `strategy_configs.json` (11 -> 7 remaining).
- **Red Bar X-Candle**: Retired. Walk-forward thin edge (1.20) collapsed on unfitted window (1.05); live trade today lost Rs 1,777 at EOD.
- **Overnight Drift**: Retired. Sizing model requires Rs 52 Lakhs for 1 lot at 0.36 exposure; live balance cannot support it.
- **HA-EMA 34 Channel** (NIFTY/SENSEX): Retired. Backtest over 264 sessions showed negative expectancy (-5.6 index pts avg per trade, Sharpe -3.82, net Rs -68,928), making option profit structurally impossible.

## [2026-08-07] operations | contained 16G runaway service log
- Found `/opt/openalgo/log/openalgo.log` at 16 GB, filling `/` to 86%.
- Cause: systemd unit redirecting stdout/stderr with no logrotate in place.
- Action: archived tail, truncated file (freeing 16G, disk -> 51%), and installed `openalgo-logcap` hourly systemd timer for size-gated copytruncate.
- Tested: confirmed truncate works on test file.

## [2026-08-07] research | Renko PRO Index Backtest (RAW)
- Backtested NIFTY/SENSEX/BANKNIFTY/FINNIFTY under RAW flat-sizing (fixed lot sizes, no stop-based risk sizing) and option friction.
- Finding: NIFTY 30m is positive (+Rs 315,534 / +157.8%, Sharpe 0.94) but has a maximum drawdown of Rs 314,250 (157.1% of capital) which wipes out the account during the run. All other pairs lose (SENSEX 30m: -Rs 51,702, BANKNIFTY 30m: -Rs 123,230, FINNIFTY 30m: -Rs 315,187). Edge is an artifact of concentration (top 5 of 435 trades are 51% of points) and does not survive cross-symbol validation.

## [2026-08-07] research | Renko Stock Intraday Backtest (RAW)
- Backtested the Renko PRO strategy on 5 liquid stocks (RELIANCE, SBIN, HDFCBANK, ICICIBANK, TCS) using 60 days of 15m/30m data.
- Modelled flat Rs 1,00,000 position size per trade (no stop-based risk sizing) and cash friction (0.035% turnover).
- Finding: Consistently negative results (15m: -Rs 23,500 / -11.7% net, PF 0.66, maxDD 12.5%, Sharpe -3.16; 30m: -Rs 6,670 / -3.3% net, PF 0.83, maxDD 6.7%, Sharpe -1.01). Removing stop-based risk sizing confirms the core signal itself lacks a directional edge on stocks.


## [2026-08-07] research | Judas strike selection
- Replayed 4 live Judas trades across 7 strikes (OTM3 to ITM3).
- Finding: ITM decreases theta% bleed but scales up friction Rs due to premium size. ATM is worst, but no strike rescues the leak. Exit is the lever.

## [2026-08-07] bugfix | PDH/PDL quote crash
- Fixed two dead calls in `prior_levels_ema_strategy.py` (`client.quote` and `client.orderhistory`) that blocked all live entries.
- Fixed pre-market 09:10 crash loop by adding retry wait loop for exchange master.
- Added AST check `test_strategy_sdk_surface.py` to prevent future dead calls.

## [2026-08-07] instrumentation | Judas & POV premium paths
- Added throttled `PATH` logging (default 30s) to Judas and POV.
- Pre-registered gate for Judas exit change: wait for n>=15 trades before acting.
