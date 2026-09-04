import time
import pytest
from core.data_quality_guard import DataQualityGuard

def get_valid_mock_market_data():
    """Generates standard market data matching Nado DEX structures."""
    return {
        "price_data": {
            "current_price": 2500.50,
            "candles_20": [
                {
                    "open": 2495.0,
                    "high": 2505.0,
                    "low": 2490.0,
                    "close": 2500.50,
                    "volume": 12.5
                    # No timestamp, matching Nado SDK behavior
                }
                for _ in range(20)
            ]
        },
        "order_book_data": {
            "best_bid": 2500.40,
            "best_ask": 2500.60,
            "spread": 0.20,
            "bid_volume": 100.0,
            "ask_volume": 90.0
        },
        "derivatives_data": {
            "open_interest": 0.0, # Zero OI on testnet
            "funding_rate": 0.0001,
            "open_interest_trend": "neutral"
        }
    }

def test_valid_nado_data_passes():
    guard = DataQualityGuard()
    data = get_valid_mock_market_data()
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is True
    assert reason == "DATA_VALID"

def test_string_numbers_handled_safely():
    guard = DataQualityGuard()
    data = get_valid_mock_market_data()
    data["price_data"]["current_price"] = "2500.50"
    data["order_book_data"]["best_bid"] = "2500.40"
    data["derivatives_data"]["funding_rate"] = "0.0002"
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is True
    assert reason == "DATA_VALID"

def test_float_rounding_epsilon_in_candles():
    guard = DataQualityGuard()
    data = get_valid_mock_market_data()
    # High is slightly smaller than open by float rounding epsilon (1e-12)
    data["price_data"]["candles_20"][0]["open"] = 2500.000000000001
    data["price_data"]["candles_20"][0]["high"] = 2500.0
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is True

def test_crossed_order_book_rejected():
    guard = DataQualityGuard()
    data = get_valid_mock_market_data()
    data["order_book_data"]["best_bid"] = 2510.0
    data["order_book_data"]["best_ask"] = 2500.0
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is False
    assert "CROSSED_ORDER_BOOK" in reason

def test_negative_open_interest_rejected():
    guard = DataQualityGuard()
    data = get_valid_mock_market_data()
    data["derivatives_data"]["open_interest"] = -5.0
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is False
    assert "INVALID_OPEN_INTEREST" in reason

def test_funding_rate_absurd_anomaly_rejected():
    guard = DataQualityGuard()
    data = get_valid_mock_market_data()
    data["derivatives_data"]["funding_rate"] = 5.5 # 550% API corruption
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is False
    assert "FUNDING_RATE_ANOMALY" in reason

def test_timestamps_in_seconds_and_ms():
    guard = DataQualityGuard()
    now_sec = time.time()
    data = get_valid_mock_market_data()
    # Candle with seconds timestamp
    data["price_data"]["candles_20"][-1]["timestamp"] = now_sec - 10
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is True
    
    # Candle with ms timestamp
    data["price_data"]["candles_20"][-1]["timestamp"] = (now_sec - 10) * 1000
    valid, reason = guard.validate("ETH-USD", data)
    assert valid is True

def test_empty_or_corrupt_data_does_not_crash():
    guard = DataQualityGuard()
    assert guard.validate("ETH-USD", {}) == (False, "EMPTY_MARKET_DATA")
    assert guard.validate("ETH-USD", None) == (False, "EMPTY_MARKET_DATA")
    assert guard.validate("ETH-USD", {"price_data": "corrupt"}) == (False, "MISSING_PRICE_DATA")
