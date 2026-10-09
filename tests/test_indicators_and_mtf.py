import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from services.market_data_service import MarketDataService
from agents.indicator_agent import IndicatorAgent
from core.logger import TradeLogger

@pytest.fixture
def dummy_market_service():
    service = MarketDataService(exchange_name="NADO", logger=None, nado_client=MagicMock())
    service.product_map = {"BTC": 1, "ETH": 2}
    return service

# =========================================================================
# 1. INDICATORS CALCULATIONS & MATHEMATICAL GUARDS
# =========================================================================

@pytest.mark.asyncio
async def test_rsi_calculation_with_flat_market(dummy_market_service):
    """Verifies that a completely flat price series (0 gain, 0 loss) returns RSI=50.0 instead of 0.0 or error."""
    # 40 candles with identical prices
    candles = [{"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 10.0} for _ in range(40)]
    dummy_market_service._fetch_nado_candles = AsyncMock(return_value=candles)

    indicators = await dummy_market_service.fetch_indicators("BTC-USD")
    assert indicators["rsi_14"] == 50.0
    assert indicators["ema_20"] == 100.0
    assert indicators["atr_14"] == 0.0
    assert indicators["atr_pct"] == 0.0
    assert indicators["bb_position_pct"] == 50.0

@pytest.mark.asyncio
async def test_bollinger_bands_variance_complex_number_guard(dummy_market_service):
    """Verifies that tiny floating point variances cannot produce complex numbers."""
    candles = [{"open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0, "volume": 5.0} for _ in range(40)]
    dummy_market_service._fetch_nado_candles = AsyncMock(return_value=candles)

    indicators = await dummy_market_service.fetch_indicators("ETH-USD")
    assert isinstance(indicators["bb_upper"], float)
    assert isinstance(indicators["bb_lower"], float)
    assert indicators["bb_upper"] == indicators["bb_lower"] == 50.0

@pytest.mark.asyncio
async def test_adx_and_vwap_calculation(dummy_market_service):
    """Verifies that ADX-14 and intraday VWAP are accurately computed on synthetic candles."""
    candles = []
    base_price = 100.0
    for i in range(50):
        # Bullish upward trend
        p = base_price + i * 2.0
        candles.append({
            "open": p - 1.0,
            "high": p + 2.0,
            "low": p - 1.5,
            "close": p + 1.0,
            "volume": 100.0 + i * 5.0
        })
    dummy_market_service._fetch_nado_candles = AsyncMock(return_value=candles)

    indicators = await dummy_market_service.fetch_indicators("BTC-USD")
    assert "adx_14" in indicators
    assert indicators["adx_14"] > 0.0
    assert "vwap" in indicators
    assert indicators["vwap"] > 100.0
    assert indicators["ema_50"] > 0.0
    assert indicators["ema_200"] > 0.0

@pytest.mark.asyncio
async def test_indicators_fallback_schema_completeness(dummy_market_service):
    """Verifies that short history (<35 candles) returns a full schema-compliant fallback dictionary."""
    candles = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0} for _ in range(10)]
    dummy_market_service._fetch_nado_candles = AsyncMock(return_value=candles)

    indicators = await dummy_market_service.fetch_indicators("BTC-USD")
    required_keys = [
        "rsi_14", "ema_20", "ema_50", "ema_200", "ema_trend", "macd", "macd_val",
        "macd_signal", "macd_label", "macd_histogram", "atr_14", "atr_pct", "adx_14",
        "vwap", "er_14", "bb_upper", "bb_lower", "bb_middle", "sma_20", "bb_width_pct",
        "bb_position_pct", "donchian_high", "donchian_low", "algo_signals"
    ]
    for key in required_keys:
        assert key in indicators, f"Missing key in indicators fallback: {key}"

# =========================================================================
# 2. CANDLE CACHING & PERFORMANCE AUDIT
# =========================================================================

@pytest.mark.asyncio
async def test_candle_cache_reuses_fetched_data(dummy_market_service):
    """Verifies that subsequent candle calls within TTL hit cache without calling Nado API."""
    raw_candles = [{"open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 1.0} for _ in range(200)]
    
    mock_indexer_candlesticks = MagicMock()
    mock_item = MagicMock()
    mock_item.open_x18 = 100 * 10**18
    mock_item.high_x18 = 105 * 10**18
    mock_item.low_x18 = 95 * 10**18
    mock_item.close_x18 = 102 * 10**18
    mock_item.volume = 1 * 10**18
    mock_indexer_candlesticks.candlesticks = [mock_item] * 200
    
    dummy_market_service.nado_client.market.get_candlesticks = MagicMock(return_value=mock_indexer_candlesticks)

    # First fetch: 200 candles (triggers API call)
    c1 = await dummy_market_service._fetch_nado_candles("BTC-USD", 15, 200)
    assert len(c1) == 200
    assert dummy_market_service.nado_client.market.get_candlesticks.call_count == 1

    # Second fetch: 50 candles (served from cache)
    c2 = await dummy_market_service._fetch_nado_candles("BTC-USD", 15, 50)
    assert len(c2) == 50
    # Must NOT call API again!
    assert dummy_market_service.nado_client.market.get_candlesticks.call_count == 1

# =========================================================================
# 3. MULTI-TIMEFRAME (MTF) RESILIENCE & LOGIC
# =========================================================================

@pytest.mark.asyncio
async def test_fetch_multi_timeframe_resilience_to_4h_failure(dummy_market_service):
    """Verifies that a failure or timeout on 4H candles does not crash fetch_multi_timeframe."""
    async def mock_fetch_ohlc(symbol, interval_min):
        if interval_min == 240:
            raise ValueError("Nado 4H Indexer Timeout")
        return {"trend": "BULLISH", "current_price": 60000.0, "volume": 100000.0}

    dummy_market_service._fetch_ohlc_interval = AsyncMock(side_effect=mock_fetch_ohlc)

    mtf = await dummy_market_service.fetch_multi_timeframe("BTC-USD")
    assert mtf["trend_15m"] == "BULLISH"
    assert mtf["trend_1h"] == "BULLISH"
    assert mtf["trend_4h"] == "NEUTRAL"
    assert mtf["mtf_alignment"] == "TRANSITION"

@pytest.mark.asyncio
async def test_mtf_trend_requires_21_candles(dummy_market_service):
    """Verifies that having fewer than 21 candles returns NEUTRAL trend (no false signals)."""
    short_candles = [{"open": 100.0, "high": 105.0, "low": 95.0, "close": 104.0, "volume": 10.0} for _ in range(10)]
    dummy_market_service._fetch_nado_candles = AsyncMock(return_value=short_candles)

    ohlc = await dummy_market_service._fetch_ohlc_interval("BTC-USD", 15)
    assert ohlc["trend"] == "NEUTRAL"

# =========================================================================
# 4. INDICATOR AGENT QUANTITATIVE SIGNAL CONSUMPTION
# =========================================================================

@pytest.mark.asyncio
async def test_indicator_agent_responds_to_macd_crossover_and_sweeps():
    """Verifies that IndicatorAgent correctly interprets algorithmic crossover and sweep signals."""
    logger = TradeLogger()
    agent = IndicatorAgent(logger=logger, llm_client=None)

    market_data = {
        "price_data": {"current_price": 2500.0},
        "indicators": {
            "rsi_14": 48.0,
            "ema_20": 2490.0,
            "macd": 10.0,
            "macd_signal": 8.0,
            "algo_signals": {
                "rsi_divergence": "none",
                "macd_crossover": "bullish_cross",
                "liquidity_sweeps": [
                    {"type": "lower_sweep", "direction": "bullish"}
                ]
            },
            "vwap": 2480.0,  # Price > VWAP -> Bullish
            "adx_14": 28.0,  # ADX >= 25 -> Strong trend
            "plus_di_14": 25.0,
            "minus_di_14": 15.0
        },
        "multi_timeframe": {
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        }
    }

    res = await agent.analyze(market_data)
    assert res["signal"] == "BULLISH"
    assert res["confidence"] >= 75
    assert "Недавний бычий MACD кроссовер" in res["reasoning"]
    assert "сбор ликвидности снизу" in res["reasoning"]
    assert "VWAP" in res["reasoning"]
    assert "ADX" in res["reasoning"]
