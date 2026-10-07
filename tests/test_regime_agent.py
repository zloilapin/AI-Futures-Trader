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

def make_raw_asset_data(symbol, price, mtf_alignment, atr_pct, rsi_14, ema_trend, funding_rate, trend="BULLISH", er_14=0.0):
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
            "er_14": er_14,
            "ema_trend": ema_trend,
            "macd_label": "bullish" if trend == "BULLISH" else "bearish"
        },
        "news_data": {
            "overall_sentiment": "neutral"
        }
    }

def test_extract_macro_summary_cleanliness(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)
    raw_btc = make_raw_asset_data("BTC-USD", 92000.0, "FULL_ALIGNMENT", 0.8, 55.0, "up", 0.005, er_14=0.4)
    raw_eth = make_raw_asset_data("ETH-USD", 2600.0, "FULL_ALIGNMENT", 1.1, 52.0, "up", 0.008, er_14=0.4)

    summary = agent._extract_macro_summary({"btc_data": raw_btc, "eth_data": raw_eth})

    assert "BTC" in summary and "ETH" in summary
    btc = summary["BTC"]

    # Verify essential fields are extracted
    assert btc["price"] == 92000.0
    assert btc["mtf_alignment"] == "FULL_ALIGNMENT"
    assert btc["atr_pct"] == 0.8
    assert btc["rsi_14"] == 55.0
    assert btc["er_14"] == 0.4
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

    btc = make_raw_asset_data("BTC-USD", 95000.0, "FULL_ALIGNMENT", 0.75, 68.0, "up", 0.005, er_14=0.6)
    eth = make_raw_asset_data("ETH-USD", 2700.0, "FULL_ALIGNMENT", 0.90, 67.0, "up", 0.005, er_14=0.6)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "TRENDING"
    assert res["recommended_profile"] == "AGGRESSIVE"

@pytest.mark.asyncio
async def test_scenario_2_btc_mixed_chop_range_choppy_balanced(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 0.55, 48.0, "flat", 0.001, trend="NEUTRAL", er_14=0.05)
    eth = make_raw_asset_data("ETH-USD", 2650.0, "MIXED_CHOP", 0.65, 52.0, "flat", 0.001, trend="NEUTRAL", er_14=0.05)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "RANGE_CHOPPY"
    assert res["recommended_profile"] == "BALANCED"

@pytest.mark.asyncio
async def test_scenario_3_btc_high_atr_high_volatility_conservative(mock_logger, mock_llm):
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 93000.0, "FULL_ALIGNMENT", 2.1, 78.0, "up", 0.06, er_14=0.5)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "FULL_ALIGNMENT", 1.8, 75.0, "up", 0.04, er_14=0.5)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "HIGH_VOLATILITY"
    assert res["recommended_profile"] == "CONSERVATIVE"

@pytest.mark.asyncio
async def test_scenario_4_btc_risk_override_btc_high_vol_eth_trending(mock_logger, mock_llm):
    """
    BTC High ATR Override:
    Even if ETH is trending, BTC has severe volatility (ATR = 2.1% >= 1.5%)
    which forces HIGH_VOLATILITY and CONSERVATIVE profile.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 90000.0, "FULL_ALIGNMENT", 2.1, 78.0, "up", 0.02, er_14=0.1)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "FULL_ALIGNMENT", 0.8, 55.0, "up", 0.005, er_14=0.6)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "HIGH_VOLATILITY"
    assert res["recommended_profile"] == "CONSERVATIVE"

@pytest.mark.asyncio
async def test_scenario_5_high_funding_rate_volatility(mock_logger, mock_llm):
    """Extreme funding rate (> 0.05%) signals liquidation cascades and triggers HIGH_VOLATILITY."""
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 1.6, 50.0, "flat", 0.06, er_14=0.1)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 1.6, 50.0, "flat", 0.06, er_14=0.1)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "HIGH_VOLATILITY"
    assert res["recommended_profile"] == "CONSERVATIVE"

@pytest.mark.asyncio
async def test_scenario_6_transition_regime(mock_logger, mock_llm):
    """When scores are balanced between trend and range, TRANSITION regime is detected."""
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 94000.0, "TRANSITION", 0.9, 52.0, "flat", 0.001, er_14=0.25)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "TRANSITION", 0.9, 52.0, "flat", 0.001, er_14=0.25)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert res["regime"] == "TRANSITION"
    assert res["recommended_profile"] == "BALANCED"

@pytest.mark.asyncio
async def test_scenario_7_reasoning_format(mock_logger, mock_llm):
    """Verify that reasoning includes detailed multi-factor scores."""
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 0.6, 50.0, "flat", 0.0, er_14=0.05)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 0.6, 50.0, "flat", 0.0, er_14=0.05)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    assert "Multi-Factor Regime Scores" in res["reasoning_en"]
    assert "TREND:" in res["reasoning_en"]
    assert "RANGE:" in res["reasoning_en"]
    assert "VOLATILITY:" in res["reasoning_en"]

@pytest.mark.asyncio
async def test_scenario_8_extreme_rsi_overbought_exhaustion_guard(mock_logger, mock_llm):
    """
    Overheated Impulse Guard:
    An extreme RSI (e.g. 85.0 overbought blow-off) must NOT artificially pump the trend score
    into AGGRESSIVE if momentum is exhausted. It receives an exhaustion penalty and avoids false AGGRESSIVE.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    # Moderate trend with blow-off top RSI 84 (overbought exhaustion)
    btc_overbought = make_raw_asset_data("BTC-USD", 95000.0, "PARTIAL_ALIGNMENT", 0.75, 84.0, "up", 0.005, trend="BULLISH", er_14=0.35)
    eth_overbought = make_raw_asset_data("ETH-USD", 2700.0, "PARTIAL_ALIGNMENT", 0.85, 82.0, "up", 0.005, trend="BULLISH", er_14=0.35)

    res = await agent.analyze({"btc_data": btc_overbought, "eth_data": eth_overbought})
    # Due to exhaustion guard (-10 pts penalty for RSI > 75), it does not reach the 60+ threshold for AGGRESSIVE
    assert res["recommended_profile"] != "AGGRESSIVE"
    assert res["regime"] in ["TRANSITION", "RANGE_CHOPPY"]

