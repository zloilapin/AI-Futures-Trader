import time
import math
from typing import Dict, Any, Tuple, Optional
from core.logger import TradeLogger

class DataQualityGuard:
    """
    Deterministic system component that validates raw market data before it reaches the ScannerAgent or AI.
    Ensures that the LLMs never hallucinate on stale, broken, or mathematically impossible data.
    """
    def __init__(self, logger: Optional[TradeLogger] = None):
        self.logger = logger
        
        # TTL Thresholds in seconds
        self.MAX_PRICE_AGE = 60
        self.MAX_ORDER_BOOK_AGE = 60
        self.MAX_OHLCV_AGE = 300
        self.MAX_FUNDING_AGE = 600
        self.MAX_OI_AGE = 600
        
        # Upper sanity bound for funding rate (allow up to 200% for extreme crypto/testnet conditions, reject true API corruptions)
        self.MAX_FUNDING_RATE = 2.0 
        self.EPSILON = 1e-8

    def _safe_float(self, val: Any) -> Optional[float]:
        """Safely parses a value to a finite float, returning None if invalid."""
        if val is None:
            return None
        try:
            f = float(val)
            return f if math.isfinite(f) else None
        except (ValueError, TypeError):
            return None

    def _to_timestamp_sec(self, ts: Any) -> Optional[float]:
        """Converts timestamp in seconds or milliseconds to Unix seconds."""
        ts_f = self._safe_float(ts)
        if ts_f is None or ts_f <= 0:
            return None
        # If timestamp is > 1e11, it's in milliseconds (e.g. 1.7e12)
        if ts_f > 1e11:
            return ts_f / 1000.0
        return ts_f

    def validate(self, symbol: str, market_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Runs all Data Quality Checks.
        Returns: (is_valid, reason)
        Guaranteed never to raise unhandled exceptions.
        """
        try:
            if not market_data or not isinstance(market_data, dict):
                return False, "EMPTY_MARKET_DATA"
                
            current_time_sec = time.time()

            # 1. Price Freshness & Finite
            price_data = market_data.get("price_data")
            if not isinstance(price_data, dict) or not price_data:
                return False, "MISSING_PRICE_DATA"
                
            current_price = self._safe_float(price_data.get("current_price"))
            if current_price is None or current_price <= 0:
                return False, "INVALID_CURRENT_PRICE"
                
            # Optional price timestamp freshness check
            price_ts = self._to_timestamp_sec(price_data.get("timestamp"))
            if not price_ts:
                candles_20 = price_data.get("candles_20")
                if isinstance(candles_20, list) and candles_20:
                    price_ts = self._to_timestamp_sec(candles_20[-1].get("timestamp"))
            
            if price_ts:
                age_sec = current_time_sec - price_ts
                if age_sec > self.MAX_PRICE_AGE or age_sec < -60:
                    return False, f"STALE_PRICE_DATA (Age: {age_sec:.1f}s)"

            # 2. Order Book Integrity
            orderbook = market_data.get("order_book_data")
            if not isinstance(orderbook, dict) or not orderbook:
                return False, "MISSING_ORDER_BOOK"
                
            best_bid = self._safe_float(orderbook.get("best_bid"))
            best_ask = self._safe_float(orderbook.get("best_ask"))
            
            if best_bid is None or best_bid <= 0:
                return False, "INVALID_BEST_BID"
            if best_ask is None or best_ask <= 0:
                return False, "INVALID_BEST_ASK"
                
            # Allow equal or tiny float difference; strictly ask < bid minus epsilon is crossed
            if best_ask < best_bid - self.EPSILON:
                return False, f"CROSSED_ORDER_BOOK (Bid: {best_bid}, Ask: {best_ask})"

            # 3. OHLCV Integrity & Gaps
            candles_15m = price_data.get("candles_20")
            if not isinstance(candles_15m, list) or not candles_15m:
                return False, "MISSING_OHLCV_DATA"
                
            prev_ts_sec = None
            interval_sec = 15 * 60 # 15 minutes
            
            for c in candles_15m:
                if not isinstance(c, dict):
                    return False, "NON_DICT_CANDLE_DATA"
                    
                o = self._safe_float(c.get("open"))
                h = self._safe_float(c.get("high"))
                l = self._safe_float(c.get("low"))
                cl = self._safe_float(c.get("close"))
                v = self._safe_float(c.get("volume"))
                ts_sec = self._to_timestamp_sec(c.get("timestamp"))
                
                # Finite & non-null checks
                if None in (o, h, l, cl, v):
                    return False, "NON_FINITE_OHLCV_DATA"
                    
                if o <= 0 or h <= 0 or l <= 0 or cl <= 0 or v < 0:
                    return False, "NEGATIVE_OR_ZERO_OHLCV_DATA"
                    
                # Logic checks with dynamic epsilon tolerance for IEEE-754 float rounding
                cand_eps = max(self.EPSILON, h * 1e-6)
                if (h < l - cand_eps or 
                    h < o - cand_eps or 
                    h < cl - cand_eps or 
                    l > o + cand_eps or 
                    l > cl + cand_eps):
                    return False, f"CORRUPTED_OHLCV_CANDLE_LOGIC (O:{o} H:{h} L:{l} C:{cl})"
                    
                # Gap detection and monotonicity (only if timestamps are provided by exchange)
                if ts_sec is not None:
                    if prev_ts_sec is not None:
                        if ts_sec <= prev_ts_sec:
                            return False, "NON_MONOTONIC_OHLCV_TIMESTAMP"
                            
                        gap_sec = ts_sec - prev_ts_sec
                        if gap_sec > interval_sec * 2.0:
                            return False, f"OHLCV_GAP_DETECTED ({gap_sec:.0f}s)"
                            
                    prev_ts_sec = ts_sec
                
            last_candle_ts = self._to_timestamp_sec(candles_15m[-1].get("timestamp"))
            if last_candle_ts:
                age_sec = current_time_sec - last_candle_ts
                if age_sec > self.MAX_OHLCV_AGE + interval_sec or age_sec < -60:
                    return False, f"STALE_OHLCV_DATA (Age: {age_sec:.1f}s)"

            # 4. Derivatives Sanity (Funding & OI)
            derivs = market_data.get("derivatives_data")
            if not isinstance(derivs, dict) or not derivs:
                return False, "MISSING_DERIVATIVES_DATA"
                
            oi = self._safe_float(derivs.get("open_interest"))
            funding = self._safe_float(derivs.get("funding_rate", derivs.get("funding_rate_decimal")))
            deriv_ts = self._to_timestamp_sec(derivs.get("timestamp"))
            
            # Open Interest: allow 0.0 on testnet / low activity DEX, reject negative
            if oi is not None and oi < 0:
                return False, f"INVALID_OPEN_INTEREST ({oi})"
                    
            # Funding Rate Sanity: reject truly absurd corruptions
            if funding is not None:
                if abs(funding) > self.MAX_FUNDING_RATE:
                    return False, f"FUNDING_RATE_ANOMALY (Value: {funding})"
                    
            if deriv_ts:
                age_sec = current_time_sec - deriv_ts
                if age_sec > self.MAX_FUNDING_AGE or age_sec < -60:
                    return False, f"STALE_DERIVATIVES_DATA (Age: {age_sec:.1f}s)"

            return True, "DATA_VALID"

        except Exception as e:
            if self.logger:
                self.logger.error(f"[DataQualityGuard] Unexpected validation exception on {symbol}: {e}")
            return False, f"VALIDATION_EXCEPTION: {type(e).__name__}: {e}"
