import pytest
from unittest.mock import MagicMock
from core.strategy_router import StrategyRouter, StrategyProfile
from agents.risk_manager import RiskManager
from core.logger import TradeLogger


@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


@pytest.fixture
def risk_manager(mock_logger):
    return RiskManager(logger=mock_logger, llm_client=None)


# ==============================================================================
# LEVEL 2: SCENARIO MATRIX TESTS
# ==============================================================================

@pytest.mark.parametrize("regime,vol_score,volume_spike,expected_allowed,expected_reason_snippet", [
    # 1. RANGE_CHOPPY with low volatility (< 7.0) -> BREAKOUT MUST BE VETOED (Dead Market Protection)
    ("RANGE_CHOPPY", 4.2, 130.0, False, "Запрет BREAKOUT"),
    ("RANGE_CHOPPY", 6.9, 130.0, False, "Запрет BREAKOUT"),
    # 2. RANGE_CHOPPY with volume explosion (>= 300%) -> BREAKOUT ALLOWED (Cohort B exception)
    ("RANGE_CHOPPY", 5.0, 350.0, True, "BREAKOUT LONG"),
    # 3. TRENDING: dead market (< 7.0) vetoes breakout, but healthy volatility (>= 7.0) allows it
    ("TRENDING", 5.0, 130.0, False, "Запрет BREAKOUT"),
    ("TRENDING", 10.0, 130.0, True, "BREAKOUT LONG"),
    ("TRENDING", 12.0, 150.0, True, "BREAKOUT LONG"),
    # 4. HIGH_VOLATILITY -> BREAKOUT ALLOWED
    ("HIGH_VOLATILITY", 25.0, 200.0, True, "BREAKOUT LONG"),
])
def test_matrix_breakout_across_regimes(regime, vol_score, volume_spike, expected_allowed, expected_reason_snippet):
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP" if "CHOP" in regime else "FULL_ALIGNMENT",
            "trend_1h": "BULLISH"
        },
        "indicators": {
            "volume_spike_pct": volume_spike,
            "bb_width_pct": 5.0,
            "donchian_high": 100.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 101.0
        },
        "price_data": {"current_price": 101.5},
        "derivatives_data": {"open_interest_trend": "rising"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {"regime_info": {"vol_score": vol_score}}

    profile = StrategyRouter.evaluate(
        "BTC-USD", market_data, valid_reports, macro_cache,
        detected_regime=regime, profile="BALANCED"
    )

    assert profile.has_directional_signal is expected_allowed
    assert expected_reason_snippet in profile.reasoning


@pytest.mark.parametrize("regime,bb_width,bb_position,expected_allowed,expected_strategy", [
    # 1. RANGE_CHOPPY with healthy channel width (>= 0.8%) -> MEAN_REVERSION ALLOWED
    ("RANGE_CHOPPY", 2.5, 4.0, True, "MEAN_REVERSION"),
    ("RANGE_CHOPPY", 1.2, 96.0, True, "MEAN_REVERSION"),
    # 2. HIGH_VOLATILITY -> MEAN_REVERSION MUST BE STRICTLY BLOCKED (Knife catching prevention)
    ("HIGH_VOLATILITY", 5.0, 4.0, False, "MEAN_REVERSION"),
    ("HIGH_VOLATILITY", 15.0, 96.0, False, "MEAN_REVERSION"),
])
def test_matrix_mean_reversion_across_regimes(regime, bb_width, bb_position, expected_allowed, expected_strategy):
    is_bottom = bb_position < 50.0
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "NEUTRAL"
        },
        "indicators": {
            "bb_position_pct": bb_position,
            "bb_width_pct": bb_width,
            "volume_spike_pct": 20.0,
            "donchian_high": 110.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 91.0 if is_bottom else 109.0
        },
        "price_data": {"current_price": 91.0 if is_bottom else 109.0},
        "derivatives_data": {"open_interest_trend": "neutral"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH" if is_bottom else "BEARISH"}
    ]
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        "ETH-USD", market_data, valid_reports, macro_cache,
        detected_regime=regime, profile="BALANCED"
    )

    assert profile.strategy_mode == expected_strategy
    assert profile.has_directional_signal is expected_allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,regime,expected_approved,expected_veto_category", [
    # In TRANSITION regime, TREND_FOLLOWING is strictly vetoed due to Beta distribution <= 0.50
    ("TREND_FOLLOWING", "TRANSITION", False, "REGIME_STRATEGY_MISMATCH"),
    # In TRANSITION regime, BREAKOUT is permitted
    ("BREAKOUT", "TRANSITION", True, None),
    # In TRENDING regime, TREND_FOLLOWING is permitted
    ("TREND_FOLLOWING", "TRENDING", True, None),
    # In RANGE_CHOPPY regime, MEAN_REVERSION is permitted when aligned with BB midline
    ("MEAN_REVERSION", "RANGE_CHOPPY", True, None),
])
async def test_matrix_risk_manager_regime_expectancy(risk_manager, strategy, regime, expected_approved, expected_veto_category):
    ceo_decision = {
        "decision": "LONG",
        "symbol": "SOL-USD",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp_price": 103.0,
        "directional_confidence": 85,
        "entry_quality": 80,
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "balance": 1000.0,
        "available_margin": 1000.0,
        "peak_balance": 1000.0,
        "recent_streak": [1, 1, 1],
        "win_rate_pct": 60.0
    }
    market_data = {
        "symbol": "SOL-USD",
        "price_data": {
            "current_price": 100.0,
            "spread_pct": 0.05
        },
        "indicators": {
            "atr_14": 1.5,
            "bb_width_pct": 5.0,
            "bb_middle": 105.0,
            "ema_20": 105.0
        },
        "derivatives_data": {
            "size_increment": 0.01,
            "price_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 0.0,
            "maintenance_margin_pct": 0.025
        }
    }

    result = await risk_manager.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime=regime,
        strategy_mode=strategy
    )

    if expected_approved:
        assert result.get("approved") is True
        assert result.get("veto_category") is None
    else:
        assert result.get("approved") is not True
        assert result.get("veto_category") == expected_veto_category
