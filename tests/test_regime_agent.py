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
    """
    Extreme funding rate (> 0.05% = 0.0005) signals liquidation cascade risk and triggers HIGH_VOLATILITY.
    Isolates funding rate: sets atr_pct = 1.35 (< 1.5 override floor) so HIGH_VOLATILITY is
    strictly proven to be caused by funding pushing vol_score >= 65.0 (37.5 ATR + 30.0 FR = 67.5).
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    # Case A: Extreme funding rate (0.06% = 0.0006) pushes vol_score above 65.0
    btc_high_fr = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 1.35, 50.0, "flat", 0.0006, er_14=0.25)
    eth_high_fr = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 1.35, 50.0, "flat", 0.0006, er_14=0.25)

    res_high = await agent.analyze({"btc_data": btc_high_fr, "eth_data": eth_high_fr})
    assert res_high["regime"] == "HIGH_VOLATILITY"
    assert res_high["recommended_profile"] == "CONSERVATIVE"

    # Case B: Normal funding rate (0.01% = 0.0001) with identical ATR (1.35) does NOT trigger HIGH_VOLATILITY
    btc_normal_fr = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", 1.35, 50.0, "flat", 0.0001, er_14=0.25)
    eth_normal_fr = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 1.35, 50.0, "flat", 0.0001, er_14=0.25)

    res_normal = await agent.analyze({"btc_data": btc_normal_fr, "eth_data": eth_normal_fr})
    assert res_normal["regime"] != "HIGH_VOLATILITY"
    assert res_normal["regime"] in ["RANGE_CHOPPY", "TRANSITION"]

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


# ==============================================================================
# AUDIT DEFECT SUITE: EMPTY DATA, NAN/CORRUPTION, BOUNDARY TESTS & INTEGRATION
# ==============================================================================

@pytest.mark.asyncio
async def test_defect_1_empty_macro_data_fails_closed_unknown(mock_logger, mock_llm):
    """
    Defect 1 Verification:
    When macro data payload is empty (e.g. network outage or missing keys),
    RegimeAgent must NEVER falsely classify it as RANGE_CHOPPY / BALANCED.
    It must fail closed with regime="UNKNOWN", recommended_profile="CONSERVATIVE", data_valid=False.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    # Completely empty dicts
    res1 = await agent.analyze({"btc_data": {}, "eth_data": {}})
    assert res1["regime"] == "UNKNOWN"
    assert res1["recommended_profile"] == "CONSERVATIVE"
    assert res1["data_valid"] is False
    assert "BTC" in res1["invalid_assets"] and "ETH" in res1["invalid_assets"]

    # None / empty input
    res2 = await agent.analyze({})
    assert res2["regime"] == "UNKNOWN"
    assert res2["recommended_profile"] == "CONSERVATIVE"
    assert res2["data_valid"] is False


@pytest.mark.asyncio
async def test_defect_4_nan_and_corrupt_macro_data_fails_closed(mock_logger, mock_llm):
    """
    Defect 4 Verification:
    NaN, Inf, or corrupted numeric values in critical fields (atr_pct, er_14, price)
    must NOT corrupt scores into artificial RANGE_CHOPPY.
    They must be rejected immediately with regime="UNKNOWN" and CONSERVATIVE profile.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    # BTC contains NaN in ATR and ER
    btc_nan = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", float("nan"), 50.0, "flat", 0.0, er_14=float("nan"))
    eth_valid = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 0.5, 50.0, "flat", 0.0, er_14=0.1)

    res = await agent.analyze({"btc_data": btc_nan, "eth_data": eth_valid})
    assert res["regime"] == "UNKNOWN"
    assert res["recommended_profile"] == "CONSERVATIVE"
    assert res["data_valid"] is False
    assert res["invalid_assets"] == ["BTC"]


@pytest.mark.asyncio
async def test_defect_4_missing_or_non_positive_price_fails_closed(mock_logger, mock_llm):
    """
    Missing price or price <= 0 indicates corrupt feed and must fail closed.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    btc_zero_price = make_raw_asset_data("BTC-USD", 0.0, "MIXED_CHOP", 0.5, 50.0, "flat", 0.0, er_14=0.1)
    eth_valid = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 0.5, 50.0, "flat", 0.0, er_14=0.1)

    res = await agent.analyze({"btc_data": btc_zero_price, "eth_data": eth_valid})
    assert res["regime"] == "UNKNOWN"
    assert res["recommended_profile"] == "CONSERVATIVE"
    assert res["data_valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("btc_atr, expected_regime", [
    (1.49, "NON_OVERRIDE"), # 1.49 does not trigger btc_atr >= 1.5 override
    (1.50, "HIGH_VOLATILITY"), # 1.50 triggers btc_atr >= 1.5 override exactly
    (1.51, "HIGH_VOLATILITY")  # 1.51 triggers btc_atr >= 1.5 override
])
async def test_defect_5_boundary_btc_atr_override(mock_logger, mock_llm, btc_atr, expected_regime):
    """
    Defect 5 Boundary Test:
    Tests exact boundary of btc_atr >= 1.5:
    At 1.49 (with calm indicators) it stays TRANSITION or RANGE_CHOPPY.
    At 1.50 and 1.51 it immediately snaps to HIGH_VOLATILITY.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    btc = make_raw_asset_data("BTC-USD", 94000.0, "MIXED_CHOP", btc_atr, 50.0, "flat", 0.0, er_14=0.15)
    eth = make_raw_asset_data("ETH-USD", 2600.0, "MIXED_CHOP", 0.5, 50.0, "flat", 0.0, er_14=0.15)

    res = await agent.analyze({"btc_data": btc, "eth_data": eth})
    if expected_regime == "HIGH_VOLATILITY":
        assert res["regime"] == "HIGH_VOLATILITY"
        assert res["recommended_profile"] == "CONSERVATIVE"
    else:
        assert res["regime"] != "HIGH_VOLATILITY"


@pytest.mark.asyncio
async def test_defect_5_trend_range_score_delta_exact_boundary(mock_logger, mock_llm):
    """
    Defect 5 Boundary Test:
    Classification uses strict comparison:
      trend_score > range_score + 15.0
    When trend_score is exactly equal to range_score + 15.0 (difference == 15.0),
    it must NOT qualify as TRENDING, but fall through to TRANSITION.
    Only when difference > 15.0 does it become TRENDING.
    """
    agent = RegimeAgent(mock_logger, mock_llm)

    # Synthetic summary with controlled scores:
    # Trend = 60.0, Range = 45.0 (difference == 15.0) -> TRANSITION
    summary_boundary = {
        "BTC": {
            "price": 95000.0, "atr_pct": 0.5, "funding_rate": 0.0, "er_14": 0.35, "rsi_14": 50.0,
            "mtf_trends": {"15m": "BULLISH", "1h": "BULLISH", "4h": "NEUTRAL"},
            "mtf_alignment": "PARTIAL_ALIGNMENT", "ema_trend": "neutral", "macd_label": "neutral"
        },
        "ETH": {
            "price": 2700.0, "atr_pct": 0.5, "funding_rate": 0.0, "er_14": 0.35, "rsi_14": 50.0,
            "mtf_trends": {"15m": "BULLISH", "1h": "BULLISH", "4h": "NEUTRAL"},
            "mtf_alignment": "PARTIAL_ALIGNMENT", "ema_trend": "neutral", "macd_label": "neutral"
        }
    }
    res = await agent.analyze(summary_boundary)
    # At exact boundary where trend is not strictly > range + 15, must be TRANSITION
    assert res["regime"] in ["TRANSITION", "RANGE_CHOPPY"]


def test_defect_2_strategy_router_blocks_mean_reversion_in_unknown_regime():
    """
    Defect 2 Integration Verification:
    StrategyRouter must strictly block MEAN_REVERSION when macro regime is UNKNOWN or DATA_INSUFFICIENT,
    preventing catching falling knives during telemetry failures.
    """
    from core.strategy_router import StrategyRouter

    market_data = {
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP", "trend_1h": "NEUTRAL"},
        "indicators": {"bb_width_pct": 2.0, "bb_position_pct": 8.0, "volume_spike_pct": 150.0, "donchian_high": 100.0, "donchian_low": 80.0},
        "price_data": {"current_price": 85.0}
    }
    valid_reports = [{"agent_name": "Tech_Agent", "signal": "BULLISH"}]
    macro_cache = {}

    # Under UNKNOWN regime:
    profile_unknown = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="UNKNOWN", profile="CONSERVATIVE"
    )
    assert profile_unknown.has_directional_signal is False
    assert profile_unknown.strategy_mode == "MEAN_REVERSION"
    assert "ОТКЛОНЕН" in profile_unknown.reasoning
    assert "UNKNOWN" in profile_unknown.reasoning


@pytest.mark.asyncio
async def test_risk_manager_directional_cluster_cap_enforces_limit_in_unknown_regime(mock_logger, mock_llm):
    """
    Architectural Risk Verification:
    RiskManager must treat UNKNOWN macro regime with strict limit of 2 concurrent directional positions,
    NEVER falling into the 3-position branch intended for verified TRENDING markets.
    """
    from agents.risk_manager import RiskManager

    rm = RiskManager(mock_logger, mock_llm)
    ceo_decision = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 80, "trade_action": "ENTER"}
    portfolio = {
        "total_usd": 1000.0,
        "available_margin": 800.0,
        "active_positions": {
            "ETH-USD": {"symbol": "ETH-USD", "direction": "LONG", "contracts": 0.1, "entry_price": 2500.0, "sl_price": 2400.0},
            "SOL-USD": {"symbol": "SOL-USD", "direction": "LONG", "contracts": 1.0, "entry_price": 150.0, "sl_price": 140.0}
        }
    }
    market_data = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 2.0, "atr_pct": 1.0, "er_14": 0.4, "rsi_14": 50.0},
        "market_parameters": {"min_size": 0.001, "price_increment": 1.0, "size_increment": 0.001}
    }

    # Under UNKNOWN regime with 2 existing LONGs, 3rd LONG must be VETOED by DIRECTIONAL_CLUSTER_CAP
    verdict = await rm.analyze(
        ceo_decision, portfolio, market_data,
        effective_profile="BALANCED", macro_regime="UNKNOWN"
    )
    assert verdict["approved"] is False
    assert verdict["veto_category"] == "DIRECTIONAL_CLUSTER_CAP"


