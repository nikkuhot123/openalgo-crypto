"""Shared conventions for every backtest and study in this directory.

CAPITAL
-------
Backtests report against a FIXED notional of Rs 2,00,000, never the live
account balance. Instructed 2026-08-07, and it is the right call regardless:

  - the live balance moves daily, so a result computed against it is not
    reproducible and two runs a week apart are not comparable
  - sizing to a small live balance silently truncates lots and turns a
    strategy test into a test of the account, hiding whether the EDGE works
  - it keeps research output away from anything that could be mistaken for
    an instruction to trade real money at a given size

Live capital belongs in the live strategies' own sizing, never in research.

FRICTION
--------
Measured, not assumed. Statutory is Flattrade-validated; the spread and the
entry slippage come from the live book (see slippage_study.py).
"""

BACKTEST_CAPITAL = 200_000

# Statutory charges, each way, as % of premium turnover.
OPT_COST_PCT = 0.12

# Quoted spread as % of premium. The live book has shown 0.24-0.29%; 0.41% is
# the wider figure the earlier Red Bar work measured and is kept as the
# conservative default so results are not flattered.
SPREAD_PCT = 0.41

# Effective delta of the traded ATM weekly leg against index points,
# measured 2026-08-05 (backtesting/haema_signal/redbar_backtest.py).
DELTA = 0.358

# ATM weekly premium as % of index, measured 2026-08-07: NIFTY11AUG2624550PE
# quoted 82.25 against a 24,551 spot; the 24600PE filled at 127.50 on 24,578.
PREMIUM_PCT = 0.45

LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 65, "MIDCPNIFTY": 120}


def lots_affordable(index_level, symbol="NIFTY", premium_pct=PREMIUM_PCT,
                    capital=BACKTEST_CAPITAL, max_deploy=0.5):
    """Lots that `capital` supports, deploying at most `max_deploy` of it.

    Long options only: the premium IS the capital at risk, so this is simply
    how many lots fit. Never returns more than the affordable count, and never
    less than zero -- a caller getting 0 means the test is capital-bound and
    should say so rather than silently trading a fractional lot.
    """
    lot = LOT.get(symbol.upper(), 65)
    premium = index_level * premium_pct / 100.0
    per_lot = premium * lot
    if per_lot <= 0:
        return 0
    return int((capital * max_deploy) // per_lot)


def round_trip_cost(premium, lot, spread_pct=SPREAD_PCT):
    """Rupees to open and close one lot: statutory both ways plus one spread."""
    return (2 * premium * lot) * OPT_COST_PCT / 100.0 + premium * lot * spread_pct / 100.0


def breakeven_index_points(index_level, symbol="NIFTY", spread_pct=SPREAD_PCT):
    """Index points a trade must clear before it earns anything."""
    lot = LOT.get(symbol.upper(), 65)
    premium = index_level * PREMIUM_PCT / 100.0
    return round_trip_cost(premium, lot, spread_pct) / (DELTA * lot)
