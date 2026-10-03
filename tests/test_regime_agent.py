import pytest
from unittest.mock import AsyncMock, MagicMock
from agents.regime_agent import RegimeAgent
from core.logger import TradeLogger

@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.generate = AsyncMock()
    return llm

def make_raw_asset_data(symbol, price, mtf_alignment, atr_pct, rsi_14, ema_trend, funding_rate, trend="BULLISH"):
    return {
        "exchange": "Nado DEX",
        "symbol": symbol,
        "multi_timeframe": {
            "trend_15m": trend,
            "trend_1h": trend,
            "trend_4h": trend,
            "mtf_alignment": mtf_alignment,
            "tf_15m": {"trend": trend, "candles_20": [{"open": 100, "high": 105, "low": 99, "close": 104}] * 20},
            "tf_1h": {"trend": trend, "candles_20": [{"open": 100, "high": 105, "low": 99, "close": 104}] * 20},
            "tf_4h": {"trend": trend, "candles_20": [{"open": 100, "high": 105, "low": 99, "close": 104}] * 20}
        },
        "price_data": {
            "current_price": price,
            "candles_20": [{"open": 100, "high": 105, "low": 99, "close": 104}] * 20
        },
        "order_book_data": {
            "bids": [{"price": price - 1, "size": 10.0}] * 50,
            "asks": [{"price": price + 1, "size": 10.0}] * 50
        },
        "derivatives_data": {
            "funding_rate": funding_rate,
            "open_interest_usd": 150000000.0
        },
        "indicators": {
            "atr_pct": atr_pct,
            "rsi_14": rsi_14,
            "ema_trend": ema_trend,
            "macd_label": "bullish" if trend == "BULLISH" else "bearish"
        },
        "news_data": {
            "overall_sentiment": "neutral"
        }
    }

def test_extract_macro_summary_cleanliness(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    raw_btc = make_raw_asset_data("BTC-USD", 92000.0, "FULL_ALIGNMENT", 0.8, 55.0, "up", 0.005)
    raw_eth = make_raw_asset_data("ETH-USD", 2600.0, "FULL_ALIGNMENT", 1.1, 52.0, "up", 0.008)

    summary = agent._extract_macro_summary({"btc_data": raw_btc, "eth_data": raw_eth})

    assert "BTC" in summary and "ETH" in summary
    btc = summary["BTC"]

    # Verify essential fields are extracted
    assert btc["price"] == 92000.0
    assert btc["mtf_alignment"] == "FULL_ALIGNMENT"
    assert btc["atr_pct"] == 0.8
    assert btc["rsi_14"] == 55.0
    assert btc["ema_trend"] == "up"
    assert btc["funding_rate"] == 0.005
    assert btc["open_interest_usd"] == 150000000.0
    assert btc["news_sentiment"] == "neutral"

    # Verify raw data arrays are strictly stripped
    assert "order_book_data" not in btc
    assert "candles_20" not in btc
    assert "bids" not in btc
    assert "asks" not in btc

@pytest.mark.asyncio
async def test_scenario_1_btc_full_alignment_low_atr_trending_aggressive(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    agent.generate_json = AsyncMock(return_value={
        "regime": "TRENDING",
        "recommended_profile": "AGGRESSIVE",
        "reasoning_en": "BTC and ETH are in FULL_ALIGNMENT with controlled ATR < 1.5%."
    })

    btc = make_raw_asset_data("BTC-USD", 95000.0, "FULL_ALIGNMENT", 0.75, 58.0, "up", 0.005)
    eth = make_raw_asset_data("ETH-USD", 2700.0, "FULL_ALIGNMENT", 0.90, 56.0, "up", 0.005)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "TRENDING"
    assert res["recommended_profile"] == "AGGRESSIVE"

@pytest.mark.asyncio
async def test_scenario_2_btc_mixed_chop_range_choppy_balanced(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    agent.generate_json = AsyncMock(return_value={
        "regime": "RANGE_CHOPPY",
        "recommended_profile": "BALANCED",
        "reasoning_en": "Market in mixed chop, RSI between 45 and 55."
    })

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 0.55, 48.0, "flat", 0.001, trend="NEUTRAL")
    eth = make_raw_asset_data("ETH-USD", 2650.0, "MIXED_CHOP", 0.65, 52.0, "flat", 0.001, trend="NEUTRAL")

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "RANGE_CHOPPY"
    assert res["recommended_profile"] == "BALANCED"

@pytest.mark.asyncio
async def test_scenario_3_btc_high_atr_high_volatility_conservative(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    agent.generate_json = AsyncMock(return_value={
        "regime": "HIGH_VOLATILITY",
        "recommended_profile": "CONSERVATIVE",
        "reasoning_en": "Severe ATR spike (2.1%) and extreme RSI (78)."
    })

    btc = make_raw_asset_data("BTC-USD", 93000.0, "FULL_ALIGNMENT", 2.1, 78.0, "up", 0.06)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "FULL_ALIGNMENT", 1.8, 75.0, "up", 0.04)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "HIGH_VOLATILITY"
    assert res["recommended_profile"] == "CONSERVATIVE"

@pytest.mark.asyncio
async def test_scenario_4_btc_risk_override_btc_high_vol_eth_trending(mock_logger, mock_llm):
    """
    CRITICAL TEST: Even if LLM erroneously outputs AGGRESSIVE because ETH is trending,
    the deterministic BTC Risk Override catches BTC's HIGH_VOLATILITY and forces CONSERVATIVE.
    """
    agent = RegimeAgent(mock_logger, mock_llm)
    # LLM hallucinating AGGRESSIVE because of ETH
    agent.generate_json = AsyncMock(return_value={
        "regime": "TRENDING",
        "recommended_profile": "AGGRESSIVE",
        "reasoning_en": "ETH is trending strongly, recommending aggressive."
    })

    # BTC has severe volatility (ATR = 2.1% >= 1.5%)
    btc = make_raw_asset_data("BTC-USD", 90000.0, "FULL_ALIGNMENT", 2.1, 78.0, "up", 0.02)
    # ETH looks clean
    eth = make_raw_asset_data("ETH-USD", 2600.0, "FULL_ALIGNMENT", 0.8, 55.0, "up", 0.005)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "HIGH_VOLATILITY"
    assert res["recommended_profile"] == "CONSERVATIVE"
    assert "BTC Risk Override" in res["reasoning_en"]

@pytest.mark.asyncio
async def test_scenario_5_llm_failure_or_invalid_json_safe_fallback(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    # LLM returns error dict
    agent.generate_json = AsyncMock(return_value={"signal": "ERROR", "decision": "ERROR"})

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 0.6, 50.0, "flat", 0.0)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 0.6, 50.0, "flat", 0.0)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "RANGE_CHOPPY"
    assert res["recommended_profile"] == "BALANCED"
    assert "Fallback" in res["reasoning_en"]

@pytest.mark.asyncio
async def test_scenario_6_llm_exception_fallback(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    agent.generate_json = AsyncMock(side_effect=RuntimeError("OpenRouter 503 Service Unavailable"))

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 0.6, 50.0, "flat", 0.0)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 0.6, 50.0, "flat", 0.0)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "RANGE_CHOPPY"
    assert res["recommended_profile"] == "BALANCED"
    assert "Fallback" in res["reasoning_en"]

@pytest.mark.asyncio
async def test_scenario_7_risk_hierarchy_enforced(mock_logger, mock_llm):
    """If LLM says HIGH_VOLATILITY but recommends AGGRESSIVE, profile must be CONSERVATIVE."""
    agent = RegimeAgent(mock_logger, mock_llm)
    agent.generate_json = AsyncMock(return_value={
        "regime": "HIGH_VOLATILITY",
        "recommended_profile": "AGGRESSIVE",
        "reasoning_en": "High volatility detected."
    })

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 1.0, 50.0, "flat", 0.0)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 1.0, 50.0, "flat", 0.0)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "HIGH_VOLATILITY"
    assert res["recommended_profile"] == "CONSERVATIVE"
