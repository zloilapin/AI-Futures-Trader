import time
import math
from typing import Dict, Any, Tuple
from core.logger import TradeLogger

class DataQualityGuard:
    """
    Deterministic system component that validates raw market data before it reaches the ScannerAgent or AI.
    Ensures that the LLMs never hallucinate on stale, broken, or mathematically impossible data.
    """
    def __init__(self, logger: TradeLogger):
        self.logger = logger
        
        # TTL Thresholds in seconds
        self.MAX_PRICE_AGE = 60
        self.MAX_ORDER_BOOK_AGE = 60
        self.MAX_OHLCV_AGE = 300
        self.MAX_FUNDING_AGE = 600
        self.MAX_OI_AGE = 600
        
        self.MAX_FUNDING_RATE = 0.10 # 10% max boundary for API sanity

    def validate(self, symbol: str, market_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Runs all Data Quality Checks.
        Returns: (is_valid, reason)
        """
        if not market_data:
            return False, "EMPTY_MARKET_DATA"
            
        current_time_ms = time.time() * 1000

        # 1. Price Freshness & Finite
        price_data = market_data.get("price_data", {})
        if not price_data:
            return False, "MISSING_PRICE_DATA"
            
        current_price = price_data.get("current_price")
        price_ts = price_data.get("timestamp")
        if current_price is None or not math.isfinite(current_price) or current_price <= 0:
            return False, "INVALID_CURRENT_PRICE"
            
        if price_ts:
            age_sec = (current_time_ms - price_ts) / 1000
            if age_sec > self.MAX_PRICE_AGE:
                return False, f"STALE_PRICE_DATA (Age: {age_sec:.1f}s)"

        # 2. Order Book Integrity
        orderbook = market_data.get("orderbook", {})
        if not orderbook:
            return False, "MISSING_ORDER_BOOK"
            
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return False, "EMPTY_ORDER_BOOK_SIDE"
            
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        
        if not math.isfinite(best_bid) or best_bid <= 0:
            return False, "INVALID_BEST_BID"
        if not math.isfinite(best_ask) or best_ask <= 0:
            return False, "INVALID_BEST_ASK"
            
        if best_ask < best_bid: # Allow == if extreme low liquidity, but strictly < is crossed book anomaly
            return False, "CROSSED_ORDER_BOOK"
            
        ob_ts = orderbook.get("timestamp")
        if ob_ts:
            age_sec = (current_time_ms - ob_ts) / 1000
            if age_sec > self.MAX_ORDER_BOOK_AGE:
                return False, f"STALE_ORDER_BOOK (Age: {age_sec:.1f}s)"

        # 3. OHLCV Integrity & Gaps
        candles_15m = market_data.get("candles_15m", [])
        if not candles_15m:
            return False, "MISSING_OHLCV_DATA"
            
        prev_ts = None
        interval_ms = 15 * 60 * 1000 # 15 minutes
        
        for c in candles_15m:
            o, h, l, cl, v, ts = c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume"), c.get("timestamp")
            
            # Finite checks
            if any(val is None or not math.isfinite(val) for val in [o, h, l, cl, v, ts]):
                return False, "NON_FINITE_OHLCV_DATA"
                
            # Logic checks
            if h < l or h < o or h < cl or l > o or l > cl or v < 0:
                return False, "CORRUPTED_OHLCV_CANDLE_LOGIC"
                
            # Gap detection and monotonicity
            if prev_ts is not None:
                if ts <= prev_ts:
                    return False, "NON_MONOTONIC_OHLCV_TIMESTAMP"
                    
                gap = ts - prev_ts
                if gap > interval_ms * 1.5:
                    return False, "OHLCV_GAP_DETECTED"
                    
            prev_ts = ts
            
        last_candle_ts = candles_15m[-1].get("timestamp")
        if last_candle_ts:
            age_sec = (current_time_ms - last_candle_ts) / 1000
            if age_sec > self.MAX_OHLCV_AGE + (15 * 60): # Give 15m buffer for the candle window itself
                return False, f"STALE_OHLCV_DATA (Age: {age_sec:.1f}s)"

        # 4. Derivatives Sanity (Funding & OI)
        derivs = market_data.get("derivatives_data", {})
        if not derivs:
            return False, "MISSING_DERIVATIVES_DATA"
            
        oi = derivs.get("open_interest")
        funding = derivs.get("funding_rate")
        deriv_ts = derivs.get("timestamp")
        
        # Open Interest
        if oi is not None:
            if not math.isfinite(oi) or oi <= 0:
                return False, "INVALID_OPEN_INTEREST"
                
        # Funding Rate Normalize & Sanity
        if funding is not None:
            if not math.isfinite(funding):
                return False, "NON_FINITE_FUNDING_RATE"
            if abs(funding) > self.MAX_FUNDING_RATE:
                return False, f"FUNDING_RATE_ANOMALY (Value: {funding})"
                
        if deriv_ts:
            age_sec = (current_time_ms - deriv_ts) / 1000
            if age_sec > self.MAX_FUNDING_AGE:
                return False, f"STALE_DERIVATIVES_DATA (Age: {age_sec:.1f}s)"

        return True, "DATA_VALID"
