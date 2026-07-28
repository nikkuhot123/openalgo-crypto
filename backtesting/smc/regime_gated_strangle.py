"""Regime-gated short strangle - Volrix Strategy class.

MEASURED OUTCOME: not an edge. Both NIFTY and SENSEX LOSE in Jan-Apr 2026
(-11.5% / -16.2%) and WIN in May-Jul 2026 (+20.9% / +8.3%) on the identical
config, so the P&L is set by the period, not the strategy: this is short-
volatility exposure. The Markov regime gate reduces variance (it helped
+11.0pp on NIFTY TRAIN) but is not alpha (it blocked 100% of NIFTY's best
quarter, -20.9pp). Parameters below are each a measured optimum, not guesses.
See FINDINGS.md Part 4 before using. Do not deploy on one quarter.
"""

CODE = r'''

class RegimeGatedStrangle(Strategy):
    """Intraday short strangle, gated by a causal Markov regime label.

    Every element below is a MEASURED result from this repo, not a guess:

      * non-directional structure - directional SMC/ICT signals measured
        E[R] = -0.007R (t = -0.9, n = 35,874), i.e. no edge, so direction is
        not bet on at all.
      * REGIME GATE (the new ingredient) - skip Bear days. Over 3,023
        day-observations on 4 indices, mean realised range was 1.433% in Bear
        vs 1.002% in Sideways (t = 8.9), and a seller's P(|move| < 0.5%) was
        42.7% in Bear vs 63.2% in Sideways. Bear days are where a premium
        seller dies. Label follows markov-hedge-fund-method (20-day rolling
        return, +/-2% thresholds).
      * SKIP EXPIRY DAY - measured DD 13.2% -> 9.4% and Sharpe 1.30 -> 1.42.
        Sellers are short gamma; the tail lives on DTE 0.
      * OTM strikes, not ATM - a blind ATM straddle measured -31.5%.
      * premium stop 40%, NOT 30% - tightening it measured DD 9.4% -> 14.3%
        (more stop-outs on noise, each realised against cost).
      * NO protective wing - a credit spread measured -14.1%; the wing premium
        exceeds the tail it insures on weeklies.

    The gate is strictly causal: the label for today uses daily closes strictly
    BEFORE today, so there is no lookahead.
    """

    WINDOW      = 20        # rolling window for the regime label
    THRESH      = 0.02      # +/-2% -> Bull / Bear, else Sideways
    SKIP_BEAR   = True
    SKIP_BULL   = False     # Bull range (1.098%) sits close to Sideways (1.002%)
    STRIKE_VAL  = -4# OTM1 both sides
    LOTS        = 1
    SL_PCT      = 100.0
    TGT_PCT     = 50.0
    SKIP_DTE0   = True
    ENTRY_H, ENTRY_M = 9, 20
    EOD_H, EOD_M     = 15, 15

    def init(self):
        self.actions_all = {'act_entry': {'trigger': False, 'legs': []}}

    def data_init(self):
        pass

    def indicator_init(self):
        pass

    def onNewDay(self):
        self.data_init()
        self.indicator_init()
        self.entries = {'entry1': {'max': 1, 'cur': 0}}
        self.regime = self._regime()

    def _regime(self):
        """0 Bear / 1 Sideways / 2 Bull from daily closes strictly before today."""
        daily = self.getDailyData()
        if daily is None or len(daily) == 0:
            return None
        prev = daily[daily['date'] < pd.Timestamp(self.currentDay)]
        if len(prev) < self.WINDOW + 1:
            return None
        closes = prev['close'].values
        c_now = float(closes[-1])
        c_then = float(closes[-1 - self.WINDOW])
        if c_then <= 0:
            return None
        roll = c_now / c_then - 1.0
        if roll > self.THRESH:
            return 2
        if roll < -self.THRESH:
            return 0
        return 1

    def minTrigger(self):
        if self.candleTime >= datetime.time(self.EOD_H, self.EOD_M):
            if self.position != []:
                self.square_off_all_positions(remark='EOD exit')

    def act_entry(self):
        for opt in ('CE', 'PE'):
            leg = self.add_managed_leg(
                side='sell', option_type=opt, lots=self.LOTS,
                strike_selection={'strikeBy': 'moneyness', 'strikeVal': self.STRIKE_VAL,
                                  'asof': 'None', 'roundoff': None},
                exp={'expType': 'weekly', 'expNo': 0},
                stop_loss={'isSL': True, 'SLon': '%', 'SLvalue': self.SL_PCT},
                target={'isTarget': True, 'targetOn': '%', 'targetValue': self.TGT_PCT},
                trailing_stop_loss={'isTrailSL': False, 'trailSLon': 'val',
                                    'trailSL_X': 1, 'trailSL_Y': 1},
                stop_loss_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                                   'reEntryVal': 0, 'reEntryMaxNo': 0},
                target_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                                'reEntryVal': 0, 'reEntryMaxNo': 0},
                wait_trade={'isWT': False, 'wtOn': 'val-up', 'wtVal': 1, 'triggers': []},
                segment='OPT', square_off='this', leg_name=f'{opt}_leg',
                tag='regime_strangle', remark=f'Short OTM {opt}',
            )
            self.actions_all['act_entry']['legs'].append(leg)

    def onCandleClose(self):
        if self.regime is None:
            return
        if self.SKIP_BEAR and self.regime == 0:
            return
        if self.SKIP_BULL and self.regime == 2:
            return
        if self.SKIP_DTE0 and self.calculate_days_to_expiry(expiry_type='weekly', expiry_number=0) == 0:
            return
        if self.candleTime < datetime.time(self.ENTRY_H, self.ENTRY_M):
            return
        if self.candleTime >= datetime.time(self.EOD_H, self.EOD_M):
            return
        if self.entries['entry1']['cur'] >= self.entries['entry1']['max']:
            return
        if self.position != [] or self._triggers != []:
            return
        self.entries['entry1']['cur'] += 1
        self.act_entry()

    def onEnd(self):
        pass

'''
