import httpx

api_key = "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a"

# Test 5m interval (should work - POV and Regime strategies use it fine)
print("=== Test 5m interval ===")
try:
    with httpx.Client(http2=False, timeout=15.0) as client:
        resp = client.post("http://127.0.0.1:5000/api/v1/history", json={
            "apikey": api_key, "symbol": "NIFTY", "exchange": "NSE_INDEX",
            "interval": "5m", "start_date": "2026-06-19", "end_date": "2026-06-22"
        })
        data = resp.json()
        print(f"Status: {data.get('status')}, Candles: {len(data.get('data', []))}")
except Exception as e:
    print(f"Error: {e}")

# Test D interval (the one that's failing)
print("=== Test D interval ===")
try:
    with httpx.Client(http2=False, timeout=15.0) as client:
        resp = client.post("http://127.0.0.1:5000/api/v1/history", json={
            "apikey": api_key, "symbol": "NIFTY", "exchange": "NSE_INDEX",
            "interval": "D", "start_date": "2026-06-10", "end_date": "2026-06-21"
        })
        data = resp.json()
        print(f"Status: {data.get('status')}, Candles: {len(data.get('data', []))}")
        if data.get("data"):
            print(f"First: {data['data'][0]}")
except Exception as e:
    print(f"Error: {e}")
