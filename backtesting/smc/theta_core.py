"""Control strategy: blind intraday short ATM straddle.
Result: -31.49% on TRAIN (128 trades, PF 0.8). Kept as the control that
proves the ICT liquidity SELECTION carries the value in sweep_fade_credit.py,
not merely the act of selling premium.
"""

CODE = r'''

class ThetaCore(Strategy):
    """Canonical intraday short ATM straddle. Establishes whether premium selling
    works at all in this window - the control against which overlays are judged.
    Trades every day, so trade count is structural, not tuned."""

    ENTRY_H, ENTRY_M = 9, 20
    EOD_H, EOD_M     = 15, 15
    SL_PCT           = 30.0     # per-leg premium stop
    TGT_PCT          = 0.0      # 0 = no target
    LOTS             = 1
    SKIP_DTE0        = False

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

    def minTrigger(self):
        if self.candleTime >= datetime.time(self.EOD_H, self.EOD_M):
            if self.position != []:
                self.square_off_all_positions(remark='EOD exit')

    def act_entry(self):
        for opt in ('CE', 'PE'):
            leg = self.add_managed_leg(
                side='sell', option_type=opt, lots=self.LOTS,
                strike_selection={'strikeBy': 'moneyness', 'strikeVal': 0,
                                  'asof': 'None', 'roundoff': None},
                exp={'expType': 'weekly', 'expNo': 0},
                stop_loss={'isSL': True, 'SLon': '%', 'SLvalue': self.SL_PCT},
                target={'isTarget': self.TGT_PCT > 0, 'targetOn': '%',
                        'targetValue': self.TGT_PCT},
                trailing_stop_loss={'isTrailSL': False, 'trailSLon': 'val',
                                    'trailSL_X': 1, 'trailSL_Y': 1},
                stop_loss_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                                   'reEntryVal': 0, 'reEntryMaxNo': 0},
                target_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                                'reEntryVal': 0, 'reEntryMaxNo': 0},
                wait_trade={'isWT': False, 'wtOn': 'val-up', 'wtVal': 1, 'triggers': []},
                segment='OPT', square_off='this', leg_name=f'{opt}_leg',
                tag='theta', remark=f'Short ATM {opt}',
            )
            self.actions_all['act_entry']['legs'].append(leg)

    def onCandleClose(self):
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
