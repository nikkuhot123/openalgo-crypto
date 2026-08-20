"""Renko PRO red-bar entry + exit-tuned management, on REAL option premiums.

Entry logic is the Dr Devendra Renko engine EXACTLY as ported and validated in
backtesting/renko_engine/renko_engine_backtest.py: red bar sitting on a
structural level (CPR / gap fib / institutional zone / X candle / afternoon
range, 8pt tolerance on the body), long on a close above the red bar high,
short on a close below its median, filtered by the X 44/56 band, the gap band
and the slow EMA, blocked inside the X band and inside CPR, and required to have
>= 2R of room to the Renko boundary.

Exits are the ones the 12,096-config sweep selected on net rupees after friction:
stop at the previous candle low/high, book HALF at 2.5R, remainder at 3.0R,
no trailing, maximum 2 trades per day.

Why levels are managed on SPOT and not on the premium: the whole backtest is
defined in index points, so SL/T1/T2 are spot levels. add_managed_leg's SL is a
premium-based stop, which is a DIFFERENT system. So the legs carry no premium SL
and minTrigger() closes them on 1-minute spot touches instead, stop checked
first. The 50%-at-T1 book is expressed as two 1-lot legs -- leg A exits at T1,
leg B at T2, both killed by the stop.

This run is the gate the index-point study cannot pass on its own: every prior
strategy in this repo that looked acceptable in delta-translated points came out
materially worse on real premiums.
"""


class RenkoRedBarTuned(Strategy):

    BRICK_PCT = 0.66
    LEVEL_TOL = 8.0
    EMA_FAST_LEN = 10
    EMA_SLOW_LEN = 30
    T1_RR = 2.5
    T2_RR = 3.0
    MIN_ROOM_R = 2.0
    MAX_TRADES_DAY = 2
    INST_BARS = 3
    SL_FALLBACK = 30.0

    # Renko is sequential and path-dependent, so it must survive the daily
    # init() reset. Class-attribute defaults + never touching them in init()
    # gives persistence, because the engine reuses one Strategy instance.
    _renko_base = None
    _renko_dir = 0

    def init(self):
        self.actions_all = {
            'act_long': {'trigger': False, 'legs': []},
            'act_short': {'trigger': False, 'legs': []},
        }

    def data_init(self):
        self.register_candle_data(name='dt_spot', data_type='spot',
                                  previous_trading_days=3,
                                  timeframe=self.timeframe)

    def indicator_init(self):
        self.register_indicator(indicator_name='ema', name='ema_fast',
                                close=lambda: self.dt_spot['close'],
                                length=self.EMA_FAST_LEN)
        self.register_indicator(indicator_name='ema', name='ema_slow',
                                close=lambda: self.dt_spot['close'],
                                length=self.EMA_SLOW_LEN)

    def onNewDay(self):
        self.data_init()
        self.indicator_init()
        self.entries = {'entry1': {'max': self.MAX_TRADES_DAY, 'cur': 0}}

        # ---- per-day level state ----
        self.bar_no = 0
        self.x_high = None
        self.x_low = None
        self.x_44 = None
        self.x_56 = None
        self.day_open = None
        self.aft_high = None
        self.aft_low = None
        self.aft_44 = None
        self.aft_56 = None
        self.aft_set = False
        self.red_high = None
        self.red_low = None
        self.red_med = None
        self.red_ok = False
        self.red_used = False
        self.prev_close = None
        self.prev_red_high = None
        self.prev_red_med = None

        # ---- open-trade state (spot levels) ----
        self.pos_side = None
        self.sl_spot = None
        self.t1_spot = None
        self.t2_spot = None
        self.t1_done = False
        self.leg_a = None
        self.leg_b = None

        self.pdh = None
        self.pdl = None
        self.pdc = None
        self.cpp = None
        self.cpr_hi = None
        self.cpr_lo = None
        self.inst_high = None
        self.inst_low = None
        self._load_prior_day()

    def _load_prior_day(self):
        """Prior-day H/L/C for CPR, and the prior session's last bars for the
        institutional zone. Daily 'date' is datetime64, so cast currentDay."""
        daily = self.getDailyData()
        if daily is None or len(daily) == 0:
            return
        prev = daily[daily['date'] < pd.Timestamp(self.currentDay)]
        if len(prev) == 0:
            return
        r = prev.iloc[-1]
        self.pdh = float(r['high'])
        self.pdl = float(r['low'])
        self.pdc = float(r['close'])
        self.cpp = (self.pdh + self.pdl + self.pdc) / 3.0
        bc = (self.pdh + self.pdl) / 2.0
        tc = 2.0 * self.cpp - bc
        self.cpr_hi = max(tc, bc)
        self.cpr_lo = min(tc, bc)

        d = self.dt_spot
        if d is None or len(d['close']) == 0:
            return
        dts = pd.DatetimeIndex(d['datetime'])
        mask = dts.normalize() < pd.Timestamp(self.currentDay)
        ph = np.asarray(d['high'])[mask]
        pl_ = np.asarray(d['low'])[mask]
        if len(ph) >= 1:
            k = min(self.INST_BARS, len(ph))
            self.inst_high = float(np.max(ph[-k:]))
            self.inst_low = float(np.min(pl_[-k:]))

    # ------------------------------------------------------------------ levels
    def _touch(self, lvl, lo, hi):
        if lvl is None:
            return False
        return (lo - self.LEVEL_TOL) <= lvl <= (hi + self.LEVEL_TOL)

    def _gap_levels(self):
        if self.day_open is None or self.pdc is None:
            return None, None, None
        size = abs(self.day_open - self.pdc)
        if size <= 0:
            return None, None, None
        sgn = 1.0 if self.day_open < self.pdc else -1.0
        return (self.day_open + sgn * 0.44 * size,
                self.day_open + sgn * 0.50 * size,
                self.day_open + sgn * 0.56 * size)

    def _confluence(self, lo, hi):
        g_near, g_mid, g_far = self._gap_levels()
        x_mid = None
        if self.x_high is not None:
            x_mid = (self.x_high + self.x_low) / 2.0
        for lvl in (self.cpp, self.cpr_hi, self.cpr_lo, g_near, g_mid, g_far,
                    self.inst_high, self.inst_low, self.x_high, self.x_low,
                    x_mid, self.x_44, self.x_56, self.aft_44, self.aft_56):
            if self._touch(lvl, lo, hi):
                return True
        return False

    def _renko(self, close):
        """Sequential Renko: anchor to the last completed brick, step in whole
        bricks. Never a floor() lattice -- with a brick that tracks price, a
        floor lattice rewrites every level when the brick drifts."""
        brick = max(close * self.BRICK_PCT / 100.0, 0.05)
        if self._renko_base is None:
            self._renko_base = close
        elif close >= self._renko_base + brick:
            steps = int((close - self._renko_base) / brick)
            self._renko_base = self._renko_base + steps * brick
            self._renko_dir = 1
        elif close <= self._renko_base - brick:
            steps = int((self._renko_base - close) / brick)
            self._renko_base = self._renko_base - steps * brick
            self._renko_dir = -1
        return self._renko_base - brick, self._renko_base + brick, brick

    # ------------------------------------------------------------------- entry
    def act_entry(self, side, opt_type):
        for nm in ('legA', 'legB'):
            leg = self.add_managed_leg(
                side='buy', option_type=opt_type, lots=1,
                strike_selection={'strikeBy': 'moneyness', 'strikeVal': 0,
                                  'asof': 'None', 'roundoff': None},
                exp={'expType': 'weekly', 'expNo': 0},
                stop_loss={'isSL': False, 'SLon': 'val', 'SLvalue': 0},
                target={'isTarget': False, 'targetOn': 'val', 'targetValue': 0},
                trailing_stop_loss={'isTrailSL': False, 'trailSLon': 'val',
                                    'trailSL_X': 1, 'trailSL_Y': 1},
                stop_loss_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                                   'reEntryVal': 0, 'reEntryMaxNo': 0},
                target_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                                'reEntryVal': 0, 'reEntryMaxNo': 0},
                wait_trade={'isWT': False, 'wtOn': 'val-up', 'wtVal': 1,
                            'triggers': []},
                segment='OPT', square_off='this', leg_name=nm,
                tag=f'renko_{side}', remark=f'renko {side} {opt_type}')
            if nm == 'legA':
                self.leg_a = leg
            else:
                self.leg_b = leg
            self.actions_all[f'act_{side}']['legs'].append(leg)

    def onCandleClose(self):
        d = self.dt_spot
        if d is None or len(d['close']) < 2:
            return
        o = float(d['open'][-1])
        h = float(d['high'][-1])
        lo_ = float(d['low'][-1])
        c = float(d['close'][-1])
        prev_low = float(d['low'][-2])
        prev_high = float(d['high'][-2])
        self.bar_no += 1

        # Manage an existing position FIRST, on this bar -- so a position opened
        # at the previous close is exited from the bar after entry, exactly as
        # the offline engine does. Then consider a new entry.
        self._manage(h, lo_)

        if self.bar_no == 1:
            self.x_high = h
            self.x_low = lo_
            rng = h - lo_
            self.x_44 = lo_ + 0.44 * rng
            self.x_56 = lo_ + 0.56 * rng
            self.day_open = o

        r_floor, r_ceil, brick = self._renko(c)

        t = self.candleTime
        mins = t.hour * 60 + t.minute
        if 765 <= mins < 795:
            self.aft_high = h if self.aft_high is None else max(self.aft_high, h)
            self.aft_low = lo_ if self.aft_low is None else min(self.aft_low, lo_)
        elif mins >= 795 and not self.aft_set and self.aft_high is not None:
            rng = self.aft_high - self.aft_low
            self.aft_44 = self.aft_low + 0.44 * rng
            self.aft_56 = self.aft_low + 0.56 * rng
            self.aft_set = True

        # ---- red bar bookkeeping ----
        cur_rh, cur_rm = self.red_high, self.red_med
        if c < o and self.bar_no > 1:
            body_lo = min(o, c)
            body_hi = max(o, c)
            self.red_high = h
            self.red_low = lo_
            self.red_med = (h + lo_) / 2.0
            self.red_ok = self._confluence(body_lo, body_hi)
            self.red_used = False
            cur_rh, cur_rm = self.red_high, self.red_med

        # ---- triggers (Pine crossover semantics) ----
        long_trig = False
        short_trig = False
        if self.prev_close is not None:
            if self.prev_red_high is not None and cur_rh is not None:
                long_trig = self.prev_close <= self.prev_red_high and c > cur_rh
            if self.prev_red_med is not None and cur_rm is not None:
                short_trig = self.prev_close >= self.prev_red_med and c < cur_rm
        self.prev_close = c
        self.prev_red_high = cur_rh
        self.prev_red_med = cur_rm

        if self.pos_side is not None or self.position != [] or self._triggers != []:
            return
        if self.entries['entry1']['cur'] >= self.entries['entry1']['max']:
            return
        if self.red_used or not self.red_ok:
            return
        if not (long_trig or short_trig):
            return
        if self.candleTime >= datetime.time(15, 0):
            return

        ema_s = float(self.ema_slow[-1])
        g_near, g_mid, g_far = self._gap_levels()

        # ---- zone blocks: no entries inside the X band or inside CPR ----
        if self.x_44 is not None and self.x_44 <= c <= self.x_56:
            return
        if self.cpr_lo is not None and self.cpr_lo <= c <= self.cpr_hi:
            return

        side = None
        if long_trig:
            ok = c > self.x_56 and c > ema_s
            if g_far is not None and self.day_open < self.pdc:
                ok = ok and c > g_far
            sl = prev_low if prev_low < c else c - self.SL_FALLBACK
            risk = c - sl
            tgt = r_ceil + brick if r_ceil < c + self.T1_RR * risk else r_ceil
            if ok and risk > 0 and (tgt - c) >= self.MIN_ROOM_R * risk:
                side = 'long'
        if side is None and short_trig:
            ok = c < self.x_44 and c < ema_s
            if g_far is not None and self.day_open > self.pdc:
                ok = ok and c < g_far
            sl = prev_high if prev_high > c else c + self.SL_FALLBACK
            risk = sl - c
            tgt = r_floor - brick if r_floor > c - self.T1_RR * risk else r_floor
            if ok and risk > 0 and (c - tgt) >= self.MIN_ROOM_R * risk:
                side = 'short'
        if side is None:
            return

        sgn = 1.0 if side == 'long' else -1.0
        self.sl_spot = sl
        self.t1_spot = c + sgn * risk * self.T1_RR
        self.t2_spot = c + sgn * risk * self.T2_RR
        self.pos_side = side
        self.t1_done = False
        self.red_used = True
        self.entries['entry1']['cur'] += 1
        self.act_entry(side, 'CE' if side == 'long' else 'PE')
        self.addPoint(tag=f'entry_{side}', point=c,
                      remark=f'SL {sl:.0f} T1 {self.t1_spot:.0f} T2 {self.t2_spot:.0f}')

    # -------------------------------------------------------------- management
    def _manage(self, hi, lo_):
        """Exit checks on the CURRENT strategy candle's high/low.

        This deliberately mirrors the offline engine, which resolves SL/T1/T2
        against the 15-minute bar. An earlier version managed on 1-minute spot
        via getCurrentData() inside minTrigger() -- more precise than the thing
        being validated, and it silently produced ZERO exits: positions were
        carried to the end of the run, which left `pos_side` set and blocked
        every later entry. One entry in ten sessions instead of ~13.
        """
        if self.pos_side is None or self.position == []:
            return
        # STOP FIRST, always. When one bar spans both the stop and a target the
        # true order is unknowable from OHLC, and the optimistic read is how
        # backtests manufacture edges that do not exist.
        if self.pos_side == 'long':
            if lo_ <= self.sl_spot:
                self.square_off_all_positions(remark='SL')
                self._clear()
                return
            if not self.t1_done and hi >= self.t1_spot:
                self.square_off_legs_by_id(leg_ids=[self.leg_a], remark='T1 half')
                self.t1_done = True
            if self.t1_done and hi >= self.t2_spot:
                self.square_off_all_positions(remark='T2')
                self._clear()
        else:
            if hi >= self.sl_spot:
                self.square_off_all_positions(remark='SL')
                self._clear()
                return
            if not self.t1_done and lo_ <= self.t1_spot:
                self.square_off_legs_by_id(leg_ids=[self.leg_a], remark='T1 half')
                self.t1_done = True
            if self.t1_done and lo_ <= self.t2_spot:
                self.square_off_all_positions(remark='T2')
                self._clear()

    def minTrigger(self):
        if self.candleTime >= datetime.time(15, 25):
            if self.position != []:
                self.square_off_all_positions(remark='EOD')
            self._clear()

    def _clear(self):
        self.pos_side = None
        self.sl_spot = None
        self.t1_spot = None
        self.t2_spot = None
        self.t1_done = False
        self.leg_a = None
        self.leg_b = None

    def onEnd(self):
        pass
