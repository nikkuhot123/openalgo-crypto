"""
Hostile test of the "Best Intraday Playbook" (Volume Profile & VWAP).

FACT: The playbook demands Order Flow, Delta divergence, VWAP, and Volume Profile.
FACT: Indian spot indices (NIFTY, SENSEX) DO NOT PRODUCE VOLUME OR TICK DELTA.
      We measured it: out of 173,751 NIFTY 1m bars, exactly 270 had volume > 0.
      Anyone claiming to trade "Nifty Order Flow" on spot data is selling snake oil
      (or trading the future, which introduces its own basis noise).

We cannot conjure volume. But the structural *geometry* of the playbook can be
tested perfectly by reverting to the original Market Profile (TPO - Time Price
Opportunity) and TWAP (Time-Weighted Average Price), which rely on time-at-price.

TEST 1: Value Area Rotation (Range Days)
  "Price opens inside previous day value area... Short near VAH... Buy near VAL...
   Target POC."
   -> We build a 1-minute TPO profile per day to find POC, VAH, VAL (70% value area).
   -> If today's open is between VAL and VAH:
        - If price breaches VAH and rejects back inside -> Short, target POC.
        - If price breaches VAL and rejects back inside -> Long, target POC.
   -> Scored on realized points (expectancy).

TEST 2: Opening Drive + TWAP Pullback (Trend Days)
  "Market opens above value area... wait for pullback to VWAP... Buy"
   -> If price breaks the 30-min Opening Range (OR):
   -> Wait for a pullback touching daily TWAP (Low <= TWAP for longs).
   -> Enter in trend direction, exit EOD or Stop.

No option premiums here. Pure price-path expectancy in points.
If the structural Edge exists, E[R] will be positive. If E[R] <= 0, the setup
is a coin flip and "order flow" is just hindsight bias.

Usage:
    ../venv/Scripts/python.exe backtesting/intraday/playbook_study.py
"""

import sys
import numpy as np
import pandas as pd
import duckdb
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "market_cache.duckdb"
SYMBOLS = ("NIFTY", "BANKNIFTY")

def load_1m(sym):
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute("""
        SELECT to_timestamp(timestamp)::TIMESTAMP AS ts, open, high, low, close
        FROM market_data WHERE symbol = ? AND interval = '1m' ORDER BY timestamp
    """, [sym]).fetchdf()
    con.close()
    if df.empty:
        return df
    df = df.set_index("ts")
    return df[~df.index.duplicated(keep="first")]

def compute_tpo_profile(day_df):
    """Compute Point of Control (POC), Value Area High (VAH), and Value Area Low (VAL)
    using Time Price Opportunity (1-minute bars)."""
    min_p = int(day_df['low'].min())
    max_p = int(day_df['high'].max())
    if max_p == min_p:
        return min_p, min_p, min_p
    
    bins = np.zeros(max_p - min_p + 1)
    for _, r in day_df.iterrows():
        l, h = int(r['low']), int(r['high'])
        if h == l: h = l + 1
        bins[l-min_p:h-min_p] += 1  # 1 unit of time per price level
        
    poc_idx = np.argmax(bins)
    poc = min_p + poc_idx
    
    total_tpo = bins.sum()
    target_tpo = 0.70 * total_tpo
    current_tpo = bins[poc_idx]
    
    low_idx = poc_idx
    high_idx = poc_idx
    
    while current_tpo < target_tpo and (low_idx > 0 or high_idx < len(bins)-1):
        v_up = bins[high_idx + 1] if high_idx < len(bins)-1 else -1
        v_dn = bins[low_idx - 1] if low_idx > 0 else -1
        
        if v_up >= v_dn:
            high_idx += 1
            current_tpo += bins[high_idx]
        else:
            low_idx -= 1
            current_tpo += bins[low_idx]
            
    val = min_p + low_idx
    vah = min_p + high_idx
    return poc, vah, val

def test_value_area_rotation(df):
    """Test Playbook #3: Value Area Rotation."""
    days = [group for _, group in df.groupby(df.index.date)]
    results = []
    
    prev_poc, prev_vah, prev_val = None, None, None
    for day in days:
        if len(day) < 300: # Need full session
            continue
            
        # Execute if we have yesterday's profile
        if prev_poc is not None and prev_vah > prev_val:
            d_open = day.iloc[0]['open']
            if prev_val <= d_open <= prev_vah: # Condition: Open inside Value
                trade_taken = False
                for i in range(15, len(day)-15):
                    if trade_taken: break
                    bar = day.iloc[i]
                    # Setup Short: Price went above VAH, closes back inside
                    if bar['high'] > prev_vah and bar['close'] < prev_vah:
                        entry = bar['close']
                        stop = day.iloc[i-5:i+1]['high'].max() + 2 # Strict visual stop
                        target = prev_poc
                        risk = stop - entry
                        if risk < 2: risk = 2
                        
                        # Walk forward to exit
                        for j in range(i+1, len(day)):
                            if day.iloc[j]['high'] >= stop:
                                results.append({'dir': -1, 'R': -1.0, 'pts': entry-stop})
                                trade_taken = True
                                break
                            if day.iloc[j]['low'] <= target:
                                results.append({'dir': -1, 'R': (entry-target)/risk, 'pts': entry-target})
                                trade_taken = True
                                break
                        if not trade_taken: # EOD
                            results.append({'dir': -1, 'R': (entry - day.iloc[-1]['close'])/risk, 'pts': entry - day.iloc[-1]['close']})
                            trade_taken = True

                    # Setup Long: Price went below VAL, closes back inside
                    elif bar['low'] < prev_val and bar['close'] > prev_val:
                        entry = bar['close']
                        stop = day.iloc[i-5:i+1]['low'].min() - 2
                        target = prev_poc
                        risk = entry - stop
                        if risk < 2: risk = 2
                        
                        # Walk forward to exit
                        for j in range(i+1, len(day)):
                            if day.iloc[j]['low'] <= stop:
                                results.append({'dir': 1, 'R': -1.0, 'pts': stop-entry})
                                trade_taken = True
                                break
                            if day.iloc[j]['high'] >= target:
                                results.append({'dir': 1, 'R': (target-entry)/risk, 'pts': target-entry})
                                trade_taken = True
                                break
                        if not trade_taken: # EOD
                            results.append({'dir': 1, 'R': (day.iloc[-1]['close'] - entry)/risk, 'pts': day.iloc[-1]['close'] - entry})
                            trade_taken = True
        
        # Calculate for tomorrow
        prev_poc, prev_vah, prev_val = compute_tpo_profile(day)
        
    return pd.DataFrame(results)

def test_twap_pullback(df):
    """Test Playbook #2: Opening Drive + TWAP Pullback."""
    days = [group for _, group in df.groupby(df.index.date)]
    results = []
    
    for day in days:
        if len(day) < 300:
            continue
            
        typical_price = (day['high'] + day['low'] + day['close']) / 3
        twap = typical_price.expanding().mean()
        
        # 30-min opening range
        or_bars = day.iloc[:30]
        or_high = or_bars['high'].max()
        or_low = or_bars['low'].min()
        
        trade_taken = False
        dir = 0
        or_broken_idx = 0
        
        # Detect opening drive breakout
        for i in range(30, 180): # Breakout must happen before 12:15
            if day.iloc[i]['close'] > or_high:
                dir = 1
                or_broken_idx = i
                break
            elif day.iloc[i]['close'] < or_low:
                dir = -1
                or_broken_idx = i
                break
                
        if dir == 0: continue
        
        # Wait for pullback to TWAP
        for i in range(or_broken_idx + 5, len(day)-30):
            bar = day.iloc[i]
            cur_twap = twap.iloc[i]
            
            if dir == 1 and bar['low'] <= cur_twap and bar['close'] > cur_twap:
                entry = bar['close']
                stop = cur_twap - (bar['high'] - bar['low']) * 1.5 # Anchor + swing low proxy
                if stop >= entry: stop = entry - 10
                risk = entry - stop
                
                for j in range(i+1, len(day)):
                    if day.iloc[j]['low'] <= stop:
                        results.append({'setup': 'TWAP_Long', 'R': -1.0, 'pts': stop-entry})
                        trade_taken = True; break
                if not trade_taken:
                    results.append({'setup': 'TWAP_Long', 'R': (day.iloc[-1]['close']-entry)/risk, 'pts': day.iloc[-1]['close']-entry})
                break
                
            elif dir == -1 and bar['high'] >= cur_twap and bar['close'] < cur_twap:
                entry = bar['close']
                stop = cur_twap + (bar['high'] - bar['low']) * 1.5
                if stop <= entry: stop = entry + 10
                risk = stop - entry
                
                for j in range(i+1, len(day)):
                    if day.iloc[j]['high'] >= stop:
                        results.append({'setup': 'TWAP_Short', 'R': -1.0, 'pts': entry-stop})
                        trade_taken = True; break
                if not trade_taken:
                    results.append({'setup': 'TWAP_Short', 'R': (entry-day.iloc[-1]['close'])/risk, 'pts': entry-day.iloc[-1]['close']})
                break

    return pd.DataFrame(results)

def main():
    print("====================================================================")
    print(" PLAYBOOK TEST: Value Area Rotation & TWAP Pullbacks (Pure Price)")
    print("====================================================================")
    
    for sym in SYMBOLS:
        print(f"\\nLoading {sym} 1m bars...")
        df = load_1m(sym)
        if df.empty: continue
        print(f"Loaded {len(df)} 1m bars spanning {df.index[0].date()} to {df.index[-1].date()}")
        
        # Test 1
        res1 = test_value_area_rotation(df)
        print(f"\\n{sym} - Value Area Rotation (Target POC)")
        if not res1.empty:
            avg_R = res1['R'].mean()
            win_rate = (res1['R'] > 0).mean() * 100
            avg_pts = res1['pts'].mean()
            sum_pts = res1['pts'].sum()
            print(f" Trades     : {len(res1)}")
            print(f" Win Rate   : {win_rate:.1f}%")
            print(f" Avg R      : {avg_R:+.3f} R")
            print(f" Avg Points : {avg_pts:+.2f} pts/trade")
            print(f" Total Pts  : {sum_pts:+.0f} pts (GROSS, NO SLIPPAGE)")
        else:
            print(" No trades triggered.")
            
        # Test 2
        res2 = test_twap_pullback(df)
        print(f"\\n{sym} - Opening Drive + TWAP Pullback")
        if not res2.empty:
            avg_R = res2['R'].mean()
            win_rate = (res2['R'] > 0).mean() * 100
            avg_pts = res2['pts'].mean()
            sum_pts = res2['pts'].sum()
            print(f" Trades     : {len(res2)}")
            print(f" Win Rate   : {win_rate:.1f}%")
            print(f" Avg R      : {avg_R:+.3f} R")
            print(f" Avg Points : {avg_pts:+.2f} pts/trade")
            print(f" Total Pts  : {sum_pts:+.0f} pts (GROSS, NO SLIPPAGE)")
        else:
            print(" No trades triggered.")

if __name__ == '__main__':
    main()
