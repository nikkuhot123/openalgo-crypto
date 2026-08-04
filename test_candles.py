"""Quick test: does broker history return partial in-progress candle, or only closed?"""
import os
os.environ['OPENALGO_API_KEY'] = '5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a'
import pandas as pd
from datetime import datetime, date
from openalgo import api

client = api(api_key=os.environ['OPENALGO_API_KEY'], host='http://127.0.0.1:5000')

# Fetch 1m history for NIFTY index — we'll see the timestamp of the last candle
df = client.history(symbol='NIFTY', exchange='NSE_INDEX', interval='1m',
                    start_date=date.today().strftime('%Y-%m-%d'),
                    end_date=date.today().strftime('%Y-%m-%d'))

print(f"Current time: {datetime.now().strftime('%H:%M:%S')}")
print(f"DataFrame shape: {df.shape if hasattr(df, 'shape') else 'not df'}")
if hasattr(df, 'tail'):
    print(f"Last 5 candles:")
    print(df.tail(5))
    print()
    print(f"Last candle index/timestamp: {df.index[-1] if len(df) else 'empty'}")
