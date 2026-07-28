"""Sweep-fade CREDIT model - the SMC sweep signal expressed as a short option.
Honest base config (skip DTE-0, no volume gate). Measured: +3.91% TRAIN ->
-14%/-20% VAL -> -17.07% over the full 6 months on 340 trades (PF 0.9).
Beats blind premium selling by ~44pp, but loses money. See FINDINGS.md.
"""

CODE = r'''

class SweepFadeCredit(Strategy):
    """The SMC sweep signal, expressed as a CREDIT position instead of a debit one.

    Same DNA as the buying model (pivot/prev-day liquidity pools -> sweep = wick
    through + close back inside), but when buy-side liquidity is swept and
    rejected we SELL the CE above it rather than BUY a PE. Rationale: a seller
    only needs the swept level to hold, so the trade does not have to overcome
    premium decay - which is exactly what killed the debit version. That removes
    the need for the RR>=1.2 and Unicorn-overlap gates that starved the sample.

    Volume (infographics 4/5) is used INVERTED and this is the key risk control:
    a seller's ruin is the REAL breakout, so a sweep candle carrying unusually
    high futures volume is skipped as genuine displacement rather than a trap.

    NOTE: builtin max()/min() only - Volrix data slices are not numpy despite the
    docs, and .max() raises an AttributeError that the engine silently swallows.
    """

    PIVOT_K        = 2
    PIVOT_LOOKBACK = 40
    VOL_MAX        = 1.60    # skip sweep candles with volume > x * mean(20) = real move
    SL_PCT         = 40.0    # premium stop (sellers need room for noise)
    TGT_PCT        = 50.0    # book at 50% premium decay
    STRIKE_VAL     = -1      # -1 = OTM1: sell just beyond the swept pool
    LOTS           = 1
    MAX_ENTRIES    = 2
    SL_BUF         = 0.0008  # spot invalidation buffer past the sweep extreme
    ENTRY_START    = (9, 30)
    ENTRY_END      = (14, 30)
    EOD            = (15, 15)
    USE_VOL        = False
    SKIP_DTE0      = True

    def init(self):
        self.actions_all = {'act_entry': {'trigger': False, 'legs': []}}

    def data_init(self):
        self.register_candle_data(name='dt_spot', data_type='spot',
                                  previous_trading_days=2, timeframe=self.timeframe)
        self.register_candle_data(name='dt_fut', data_type='fut',
                                  exp={'expType': 'monthly', 'expNo': 0},
                                  previous_trading_days=2, timeframe=self.timeframe)

    def indicator_init(self):
        self.register_indicator(indicator_name='prevDaysRange', name='idc_pdr',
                                df=lambda: self.dt_spot,
                                currentDay=self.currentDay, prevDays=1)

    def onNewDay(self):
        self.data_init()
        self.indicator_init()
        self.n_entries = 0
        self.side      = None    # 'CE' short  or 'PE' short
        self.invalid   = 0.0     # spot level that invalidates the fade

    def _pivots(self, high, low):
        n = len(high)
        k = self.PIVOT_K
        start = n - self.PIVOT_LOOKBACK
        if start < k:
            start = k
        ph = []
        pl = []
        for i in range(start, n - k):
            if float(high[i]) >= float(max(high[i - k:i + k + 1])):
                ph.append(float(high[i]))
            if float(low[i]) <= float(min(low[i - k:i + k + 1])):
                pl.append(float(low[i]))
        return ph, pl

    def _levels(self, pivots, prev_key):
        out = [p for p in pivots]
        pdr = self.idc_pdr
        if pdr is not None and prev_key in pdr:
            v = pdr[prev_key]
            if v is not None and float(v) > 0:
                out.append(float(v))
        return out

    def act_entry(self, opt):
        leg = self.add_managed_leg(
            side='sell', option_type=opt, lots=self.LOTS,
            strike_selection={'strikeBy': 'moneyness', 'strikeVal': self.STRIKE_VAL,
                              'asof': 'None', 'roundoff': None},
            exp={'expType': 'weekly', 'expNo': 0},
            stop_loss={'isSL': True, 'SLon': '%', 'SLvalue': self.SL_PCT},
            target={'isTarget': self.TGT_PCT > 0, 'targetOn': '%', 'targetValue': self.TGT_PCT},
            trailing_stop_loss={'isTrailSL': False, 'trailSLon': 'val',
                                'trailSL_X': 1, 'trailSL_Y': 1},
            stop_loss_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                               'reEntryVal': 0, 'reEntryMaxNo': 0},
            target_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                            'reEntryVal': 0, 'reEntryMaxNo': 0},
            wait_trade={'isWT': False, 'wtOn': 'val-up', 'wtVal': 1, 'triggers': []},
            segment='OPT', square_off='this', leg_name=f'fade_{opt}',
            tag='sweepfade', remark=f'Fade sweep - short {opt}',
        )
        self.actions_all['act_entry']['legs'].append(leg)

    def minTrigger(self):
        if self.candleTime >= datetime.time(self.EOD[0], self.EOD[1]):
            if self.position != []:
                self.square_off_all_positions(remark='EOD exit')
                self.side = None
            return
        if self.position == [] or self.side is None:
            return
        cd = self.getTimestampData(symbol=self.underlyingName, position='current', timeframe=1)
        if cd is None:
            return
        px = float(cd['close'])
        # the fade is wrong if price reclaims the swept extreme -> it was a real move
        if self.side == 'CE' and px > self.invalid:
            self.square_off_all_positions(remark='Sweep reclaimed - real breakout')
            self.side = None
        elif self.side == 'PE' and px < self.invalid:
            self.square_off_all_positions(remark='Sweep reclaimed - real breakdown')
            self.side = None

    def onCandleClose(self):
        if self.position != [] or self._triggers != []:
            return
        if self.n_entries >= self.MAX_ENTRIES:
            return
        if self.SKIP_DTE0 and self.calculate_days_to_expiry(expiry_type='weekly', expiry_number=0) == 0:
            return
        if self.candleTime < datetime.time(self.ENTRY_START[0], self.ENTRY_START[1]):
            return
        if self.candleTime >= datetime.time(self.ENTRY_END[0], self.ENTRY_END[1]):
            return

        hi = self.dt_spot['high']
        lo = self.dt_spot['low']
        cl = self.dt_spot['close']
        if len(cl) < self.PIVOT_LOOKBACK:
            return
        ph, pl = self._pivots(hi, lo)
        h = float(hi[-1])
        l = float(lo[-1])
        c = float(cl[-1])

        # volume gate, inverted for a seller: high volume = genuine displacement
        if self.USE_VOL:
            vol = self.dt_fut['volume']
            if len(vol) >= 21:
                vwin = vol[-21:-1]
                vmean = float(sum(vwin)) / float(len(vwin))
                if vmean > 0 and float(vol[-1]) > self.VOL_MAX * vmean:
                    return

        ups = [L for L in self._levels(ph, 'range High') if h > L and c < L]
        if ups:
            self.n_entries += 1
            self.side = 'CE'
            self.invalid = h * (1.0 + self.SL_BUF)
            self.act_entry('CE')
            return
        dns = [L for L in self._levels(pl, 'range Low') if l < L and c > L]
        if dns:
            self.n_entries += 1
            self.side = 'PE'
            self.invalid = l * (1.0 - self.SL_BUF)
            self.act_entry('PE')

    def onEnd(self):
        pass

'''
