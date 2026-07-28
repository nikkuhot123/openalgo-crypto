"""
SMC / ICT Sweep-Reversal + Unicorn model - Volrix Strategy class.

RESULT: does NOT meet our targets out of sample. See FINDINGS.md.
  VAL portfolio (NIFTY+SENSEX, 1% option slippage): 17 trades, -2.14%,
  Sharpe -2.32, PF 0.7. Train looked positive (PF 1.6-4.0) on 4-9 trades;
  none of it survived. Kept as a measured negative + a reusable primitive set.

Source material (5 SKB Trading Lab infographics) mapped to code:

  1. "Institutional Order Blocks & Smart Money Zones"
       - order block = last opposite-colour candle before displacement -> _find_ob()
       - FVG = 3-candle imbalance                                      -> _find_fvg()
       - sequence sweep -> BOS -> displacement -> FVG -> OB
         -> retracement -> continuation                                -> state machine
       - "wait for price to retrace into OB"                           -> ARMED -> zone touch
       - "Stop Loss: above/below the Order Block"                      -> SL at sweep extreme
       - "Target: previous highs or next liquidity zone"               -> _next_pool()
       - checklist "risk-to-reward at least 1:2"                        -> MIN_RR
  2. "Buy-side & Sell-side Liquidity (BSL/SSL)"
       - liquidity above swing highs / below swing lows                -> _pivots() + prev-day H/L
       - "LIQUIDITY SWEEP vs REAL BREAKOUT"                            -> _sweep() needs a wick
         through the pool AND a close back inside it
       - "A sweep is NOT an entry. Wait for confirmation."             -> CHOCH gate is mandatory
       - "Target the next liquidity pool"                              -> _next_pool()
  3. "Unicorn Model" (ICT #30)
       - Breaker/OB + FVG overlap = Unicorn Zone                       -> REQUIRE_OVERLAP
  4./5. Volume analysis
       - "high volume = real move, low-volume breakout = trap"         -> VOL_MULT gate
       - Volrix spot candles carry NO volume, so this reads FUTURES
         volume (measured: 611,930/bar). A data fact, not a choice.

Deliberate deviations, and why:
  - Premium/Discount is not a separate filter: a short's zone always sits ABOVE
    the CHOCH low (i.e. in premium) by construction, so the check is redundant.
  - A premium (option-price) stop floor sits on top of the spot stop. Learned
    live: a spot-only stop lets a bought option bleed on decay while spot is
    still inside the stop distance.

TWO BUGS THIS FILE ENCODES THE FIX FOR (both produced silent zero-trade runs):
  1. NEVER call .max()/.min()/.mean() on a Volrix data slice. The docs promise
     numpy.ndarray; the engine returns a plain sequence, so it raises
     AttributeError - and Volrix SWALLOWS hook exceptions, so the run
     "completes" with no trades and no error. Use builtin max()/min()/sum().
  2. CHOCH must break the MOST RECENT pivot, not min()/max() of the whole
     lookback window (that demands breaking the deepest low in 40 bars).

One class, no imports, no module-level state - per Volrix diagnostics rules.
"""

CODE = r'''
class SMCSweepReversal(Strategy):
    """Liquidity sweep -> CHOCH -> displacement(+volume) -> OB/FVG zone -> retracement entry."""

    # ---- structure detection ----
    PIVOT_K        = 2      # bars either side of a pivot
    PIVOT_LOOKBACK = 40     # candles scanned for pivots (~3.3h on 5m)
    ATR_LEN        = 14
    HTF            = 15     # higher-timeframe bias ("align with higher TF trend")

    # ---- confluence gates ----
    DISP_ATR        = 0.60  # displacement body >= x * ATR
    VOL_MULT        = 1.20  # displacement futures volume >= x * mean(vol, 20)
    REQUIRE_OVERLAP = True  # Unicorn: FVG must overlap the order block
    REQUIRE_HTF     = True  # trade only with the 15m bias
    MIN_RR          = 1.50  # reward:risk floor to the next liquidity pool
    SETUP_LIFE      = 10    # candles a setup stays valid before expiring
    MAX_ENTRIES     = 2     # per day

    # ---- execution ----
    STRIKE_VAL   = 0        # 0 = ATM, +1 = ITM1, -1 = OTM1
    LOTS         = 1
    PREM_SL_PCT  = 35.0     # option-price stop floor
    SL_BUF       = 0.0005   # 0.05% beyond the sweep extreme
    SKIP_DTE0    = False    # skip expiry day
    ENTRY_START  = (9, 30)
    ENTRY_END    = (14, 30)
    EOD          = (15, 15)

    # ------------------------------------------------------------------ setup

    def init(self):
        self.actions_all = {'act_entry': {'trigger': False, 'legs': []}}
        self.htf_grid = self._grid(self.HTF)

    @staticmethod
    def _grid(tf):
        """Clock times at which an HTF candle closes."""
        base = datetime.datetime.combine(datetime.date(2000, 1, 1), datetime.time(9, 15))
        return {(base + datetime.timedelta(minutes=k)).time() for k in range(tf, 376, tf)}

    def data_init(self):
        self.register_candle_data(name='dt_spot', data_type='spot',
                                  previous_trading_days=2, timeframe=self.timeframe)
        self.register_candle_data(name='dt_htf', data_type='spot',
                                  previous_trading_days=2, timeframe=self.HTF)
        # volume lives on futures only - spot has none
        self.register_candle_data(name='dt_fut', data_type='fut',
                                  exp={'expType': 'monthly', 'expNo': 0},
                                  previous_trading_days=2, timeframe=self.timeframe)

    def indicator_init(self):
        self.register_indicator(indicator_name='atr', name='idc_atr',
                                df=lambda: self.dt_spot, length=self.ATR_LEN)
        self.register_indicator(indicator_name='ema', name='idc_htf_ema',
                                close=lambda: self.dt_htf['close'], length=20)
        self.register_indicator(indicator_name='prevDaysRange', name='idc_pdr',
                                df=lambda: self.dt_spot,
                                currentDay=self.currentDay, prevDays=1)

    def onNewDay(self):
        self.data_init()
        self.indicator_init()
        self.setup     = None      # pending sweep/confirmation state
        self.trade_dir = None      # 'CE' | 'PE' while in a trade
        self.trade_sl  = 0.0
        self.trade_tgt = 0.0
        self.htf_bull  = None
        self.n_entries = 0
        self.bars      = 0

    # ------------------------------------------------------- structure helpers

    def _pivots(self, high, low):
        """Confirmed pivot highs/lows in the lookback window -> (highs, lows) as
        lists of (index, price). A pivot needs PIVOT_K bars either side, so the
        last PIVOT_K candles are never pivots (look-ahead safe)."""
        n = len(high)
        k = self.PIVOT_K
        start = n - self.PIVOT_LOOKBACK
        if start < k:
            start = k
        ph = []
        pl = []
        for i in range(start, n - k):
            seg_h = high[i - k:i + k + 1]
            if float(high[i]) >= float(max(seg_h)):
                ph.append((i, float(high[i])))
            seg_l = low[i - k:i + k + 1]
            if float(low[i]) <= float(min(seg_l)):
                pl.append((i, float(low[i])))
        return ph, pl

    def _levels(self, pivots, prev_key):
        """Liquidity pool prices = confirmed pivots + previous-day extreme."""
        out = [p for _, p in pivots]
        pdr = self.idc_pdr
        if pdr is not None and prev_key in pdr:
            v = pdr[prev_key]
            if v is not None and float(v) > 0:
                out.append(float(v))
        return out

    def _sweep(self, hi, lo, cl, ph, pl):
        """A sweep = wick THROUGH a pool, close back INSIDE it (failed break).
        Returns ('PE', level, extreme) for a buy-side sweep (short setup),
                ('CE', level, extreme) for a sell-side sweep (long setup)."""
        h = float(hi[-1])
        l = float(lo[-1])
        c = float(cl[-1])
        # buy-side liquidity taken above equal/prior highs -> expect reversal DOWN
        ups = [L for L in self._levels(ph, 'range High') if h > L and c < L]
        if ups:
            return 'PE', max(ups), h
        # sell-side liquidity taken below prior lows -> expect reversal UP
        dns = [L for L in self._levels(pl, 'range Low') if l < L and c > L]
        if dns:
            return 'CE', min(dns), l
        return None, 0.0, 0.0

    def _find_fvg(self, hi, lo, direction, span):
        """3-candle imbalance inside the displacement leg.
        Bearish FVG: high[i] < low[i-2]  -> zone (high[i], low[i-2])
        Bullish FVG: low[i]  > high[i-2] -> zone (high[i-2], low[i])"""
        n = len(hi)
        for back in range(0, span):
            i = n - 1 - back
            if i - 2 < 0:
                break
            if direction == 'PE':
                a = float(hi[i])
                b = float(lo[i - 2])
                if a < b:
                    return a, b
            else:
                a = float(hi[i - 2])
                b = float(lo[i])
                if b > a:
                    return a, b
        return None, None

    def _find_ob(self, op, cl, hi, lo, direction, span):
        """Order block = last opposite-colour candle before the displacement.
        Bearish OB (before a down move) = last GREEN candle -> zone (body top, high).
        Bullish OB (before an up move)  = last RED candle   -> zone (low, body bottom)."""
        n = len(cl)
        for back in range(1, span):
            i = n - 1 - back
            if i < 0:
                break
            o = float(op[i])
            c = float(cl[i])
            if direction == 'PE' and c > o:
                return max(o, c), float(hi[i])
            if direction == 'CE' and c < o:
                return float(lo[i]), min(o, c)
        return None, None

    def _next_pool(self, price, direction, ph, pl):
        """'Target the next liquidity pool' - nearest opposite pool beyond price."""
        if direction == 'PE':
            below = [L for L in self._levels(pl, 'range Low') if L < price]
            return max(below) if below else None
        above = [L for L in self._levels(ph, 'range High') if L > price]
        return min(above) if above else None

    # -------------------------------------------------------------- execution

    def _enter(self, direction, entry_px, sl, tgt):
        self.trade_dir = direction
        self.trade_sl  = sl
        self.trade_tgt = tgt
        self.n_entries += 1
        leg = self.add_managed_leg(
            side='buy', option_type=direction, lots=self.LOTS,
            strike_selection={'strikeBy': 'moneyness', 'strikeVal': self.STRIKE_VAL,
                              'asof': 'None', 'roundoff': None},
            exp={'expType': 'weekly', 'expNo': 0},
            stop_loss={'isSL': True, 'SLon': '%', 'SLvalue': self.PREM_SL_PCT},
            target={'isTarget': False, 'targetOn': 'val', 'targetValue': 0},
            trailing_stop_loss={'isTrailSL': False, 'trailSLon': 'val',
                                'trailSL_X': 1, 'trailSL_Y': 1},
            stop_loss_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                               'reEntryVal': 0, 'reEntryMaxNo': 0},
            target_reentry={'isReEntry': False, 'reEntryOn': 'asap',
                            'reEntryVal': 0, 'reEntryMaxNo': 0},
            wait_trade={'isWT': False, 'wtOn': 'val-up', 'wtVal': 1, 'triggers': []},
            segment='OPT', square_off='this', leg_name='smc_leg',
            tag='smc', remark=f'SMC {direction} sweep-reversal',
        )
        self.actions_all['act_entry']['legs'].append(leg)

    def minTrigger(self):
        """Spot-based stop/target at 1-minute resolution + EOD flat."""
        if self.candleTime >= datetime.time(self.EOD[0], self.EOD[1]):
            if self.position != []:
                self.square_off_all_positions(remark='EOD exit')
                self.trade_dir = None
            return
        if self.position == [] or self.trade_dir is None:
            return
        cd = self.getTimestampData(symbol=self.underlyingName, position='current', timeframe=1)
        if cd is None:
            return
        px = float(cd['close'])
        if self.trade_dir == 'PE':
            if px >= self.trade_sl:
                self.square_off_all_positions(remark='Spot SL')
                self.trade_dir = None
            elif px <= self.trade_tgt:
                self.square_off_all_positions(remark='Liquidity target')
                self.trade_dir = None
        else:
            if px <= self.trade_sl:
                self.square_off_all_positions(remark='Spot SL')
                self.trade_dir = None
            elif px >= self.trade_tgt:
                self.square_off_all_positions(remark='Liquidity target')
                self.trade_dir = None

    # ------------------------------------------------------------ signal loop

    def onCandleClose(self):
        self.bars += 1

        if self.candleTime in self.htf_grid:
            self.htf_bull = float(self.dt_htf['close'][-1]) > float(self.idc_htf_ema[-1])

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
        op = self.dt_spot['open']
        if len(cl) < self.PIVOT_LOOKBACK or len(self.dt_fut['volume']) < 21:
            return

        atr = float(self.idc_atr[-1])
        if atr <= 0:
            return
        ph, pl = self._pivots(hi, lo)

        # ---------- stage 1: liquidity sweep ----------
        if self.setup is None:
            d, level, extreme = self._sweep(hi, lo, cl, ph, pl)
            if d is not None:
                self.setup = {'dir': d, 'level': level, 'extreme': extreme,
                              'bar': self.bars, 'armed': False,
                              'zlo': 0.0, 'zhi': 0.0}
            return

        if self.bars - self.setup['bar'] > self.SETUP_LIFE:
            self.setup = None
            return

        d = self.setup['dir']

        # ---------- stage 2: CHOCH + displacement + volume ----------
        if not self.setup['armed']:
            body = abs(float(cl[-1]) - float(op[-1]))
            if body < self.DISP_ATR * atr:
                return
            vol = self.dt_fut['volume']
            vwin = vol[-21:-1]
            vmean = float(sum(vwin)) / float(len(vwin)) if len(vwin) > 0 else 0.0
            if vmean > 0 and float(vol[-1]) < self.VOL_MULT * vmean:
                return                                    # low-volume move = trap
            if d == 'PE':
                if not pl:
                    return
                # CHOCH = break the MOST RECENT swing low (pl is index-ascending),
                # not the deepest low in the window.
                if float(cl[-1]) >= pl[-1][1]:
                    return
                if float(cl[-1]) >= float(op[-1]):
                    return
            else:
                if not ph:
                    return
                if float(cl[-1]) <= ph[-1][1]:
                    return
                if float(cl[-1]) <= float(op[-1]):
                    return
            if self.REQUIRE_HTF and self.htf_bull is not None:
                if d == 'PE' and self.htf_bull:
                    self.setup = None
                    return
                if d == 'CE' and not self.htf_bull:
                    self.setup = None
                    return

            f_lo, f_hi = self._find_fvg(hi, lo, d, 5)
            o_lo, o_hi = self._find_ob(op, cl, hi, lo, d, 6)
            if f_lo is None and o_lo is None:
                return
            if self.REQUIRE_OVERLAP:
                if f_lo is None or o_lo is None:
                    return
                z_lo = max(f_lo, o_lo)                    # Unicorn = the overlap
                z_hi = min(f_hi, o_hi)
                if z_lo >= z_hi:
                    return
            else:
                z_lo, z_hi = (f_lo, f_hi) if f_lo is not None else (o_lo, o_hi)
            self.setup['armed'] = True
            self.setup['zlo'] = z_lo
            self.setup['zhi'] = z_hi
            self.setup['bar'] = self.bars
            return

        # ---------- stage 3: retracement into the zone ----------
        z_lo = self.setup['zlo']
        z_hi = self.setup['zhi']
        extreme = self.setup['extreme']
        if d == 'PE':
            if float(hi[-1]) < z_lo:
                return
            if float(cl[-1]) > extreme:
                self.setup = None
                return
            entry = float(cl[-1])
            sl = extreme * (1.0 + self.SL_BUF)
            tgt = self._next_pool(entry, 'PE', ph, pl)
            if tgt is None:
                return
            risk = sl - entry
            reward = entry - tgt
        else:
            if float(lo[-1]) > z_hi:
                return
            if float(cl[-1]) < extreme:
                self.setup = None
                return
            entry = float(cl[-1])
            sl = extreme * (1.0 - self.SL_BUF)
            tgt = self._next_pool(entry, 'CE', ph, pl)
            if tgt is None:
                return
            risk = entry - sl
            reward = tgt - entry
        if risk <= 0 or reward < self.MIN_RR * risk:
            return
        self.setup = None
        self._enter(d, entry, sl, tgt)

    def onEnd(self):
        pass

'''
