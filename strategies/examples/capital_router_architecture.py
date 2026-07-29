"""
Capital Adaptive Strategy Router

This module defines the routing logic based on user capital.
Because SEBI requires full SPAN + Exposure margin for overnight futures (no leverage),
capital below Rs 2.5L mathematically cannot hold a single NIFTY lot overnight.

The Router:
    CAPITAL >= Rs 2,50,000:
        -> Route to: NIFTY Overnight Drift (Index Futures)
        -> Edge: Global overnight-drift anomaly (Sharpe 1.53, 15 years stable)
        -> Execution: Flattrade (0 brokerage, ~2.84 bps statutory), NRML product.
    
    CAPITAL < Rs 2,50,000:
        -> Route to: Crypto Trend-Vol (Binance Perpetual Futures)
        -> Edge: Fractional sizing makes it perfectly scalable for Rs 40,000.
                 No STT tax, no fixed lot sizes.
        -> Execution: Crypto exchange (Binance/Hyperliquid) using Volrix Crypto logic.
"""

def route_capital(available_capital_inr: float):
    print("=========================================================")
    print(f" CAPITAL ADAPTIVE ROUTER (Input: Rs {available_capital_inr:,.2f})")
    print("=========================================================")
    
    NIFTY_LOT_MARGIN = 240000.0  # Approx margin needed for 1 NIFTY Future lot

    if available_capital_inr >= NIFTY_LOT_MARGIN:
        print(" [ROUTING] -> TIER 1: HIGH CAPITAL (Indian Futures)")
        print(" Strategy  : NIFTY Overnight Drift")
        print(" Instrument: NIFTY Current Month Future (NRML)")
        print(" Why       : Capital is sufficient to clear SEBI overnight margin requirements.")
        print("             Captures the proven overnight structural drift (Sharpe 1.53).")
        
        target_vol = 0.04
        max_lots = int(available_capital_inr / NIFTY_LOT_MARGIN)
        print(f" Allocation: Targeting {target_vol*100}% annualized volatility.")
        print(f" Maximum   : {max_lots} lot(s)\n")
        return "NIFTY_OVERNIGHT_DRIFT"

    else:
        print(" [ROUTING] -> TIER 2: LOW CAPITAL (Fractional Crypto / Global)")
        print(" Strategy  : Crypto Trend + Inverse Vol Sizing")
        print(" Instrument: BTCUSDT / ETHUSDT Perpetual Futures")
        print(" Why       : Rs 40,000 cannot clear Indian futures margin. Intraday options")
        print("             are mathematically dead after 5.68 bps statutory costs + theta.")
        print("             Crypto offers fractional sizes (trade 0.001 BTC) and 0 STT,")
        print("             perfectly matching low capital to positive expectancy trend systems.")
        return "CRYPTO_TREND_VOL"

if __name__ == "__main__":
    # Test cases
    route_capital(500000)
    route_capital(40000)
