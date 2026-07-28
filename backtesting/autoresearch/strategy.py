"""
The ONLY file the autoresearch agent edits.

Contract (do not change the signature):
    generate_signals(df) -> (long_entries, long_exits, short_entries, short_exits)

Each return value is a boolean pandas Series aligned to df.index.

Rules:
  - Use TA-Lib (`talib as tl`) or `openalgo.ta` for indicators. Never VectorBT's
    built-in indicators.
  - Clean signals with `ta.exrem()` after `.fillna(False)` so a position is not
    re-entered on every bar while already open.
  - NEVER look ahead: only use values available at or before the current bar.
    Anything using .shift(-n) or a future index is cheating and the score is void.

BASELINE: EMA(10/30) crossover on NIFTY 5-min, long and short.
"""

import pandas as pd
import talib as tl
from openalgo import ta

FAST = 10
SLOW = 30


def generate_signals(df):
    close = df["close"].astype(float)

    ema_fast = pd.Series(tl.EMA(close.values, timeperiod=FAST), index=close.index)
    ema_slow = pd.Series(tl.EMA(close.values, timeperiod=SLOW), index=close.index)

    cross_up = ta.crossover(ema_fast, ema_slow)
    cross_dn = ta.crossunder(ema_fast, ema_slow)

    cross_up = pd.Series(cross_up, index=close.index).fillna(False).astype(bool)
    cross_dn = pd.Series(cross_dn, index=close.index).fillna(False).astype(bool)

    # long: enter on up-cross, exit on down-cross (and vice versa for short)
    long_entries = ta.exrem(cross_up, cross_dn)
    long_exits = cross_dn
    short_entries = ta.exrem(cross_dn, cross_up)
    short_exits = cross_up

    to_bool = lambda s: pd.Series(s, index=close.index).fillna(False).astype(bool)
    return to_bool(long_entries), to_bool(long_exits), to_bool(short_entries), to_bool(short_exits)
