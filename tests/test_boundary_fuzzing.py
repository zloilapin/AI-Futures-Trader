import pytest
import math
from unittest.mock import MagicMock
from core.strategy_router import StrategyRouter
from core.deterministic_guard import DeterministicGuard
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
# LEVEL 3: BOUNDARY FUZZING TESTS
# ==============================================================================

@pytest.mark.parametrize("rsi_val", [0.0, 5.0, 15.0, 24.9, 25.0, 50.0, 75.0, 76.0, 76.1, 95.0, 100.0])
def test_boundary_rsi_exhaustion_guard(mock_logger, risk_manager, rsi_val):
    """Verifies DeterministicGuard handles exact boundary RSI transitions without errors."""
    strategy_profile = MagicMock()
    strategy_profile.strategy_mode = "TREND_FOLLOWING"
    strategy_profile.direction_bias = "LONG"

    ceo_proposal = {
        "decision": "LONG",
        "symbol": "BTC-USD",
        "trade_action": "ENTER",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 80,
    }
    market_data = {
        "symbol": "BTC-USD",
        "current_price": 50000.0,
        "indicators": {
            "rsi_14": rsi_val,
            "rsi_15m": rsi_val,
            "atr_14": 500.0,
            "bb_position_pct": 50.0
        }
    }

    decision = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=risk_manager,
        market_data=market_data,
        symbol="BTC-USD",
        logger=mock_logger
    )

    assert decision is not None
    assert decision.decision in ["LONG", "HOLD"]
    if rsi_val > 76.0:
        # Long into extreme overbought (> 76.0) must be BLOCKED by trend exhaustion guard
        assert decision.guard_status == "BLOCKED"
        assert decision.is_actionable is False
        assert "TREND_EXHAUSTION_VETO" in decision.guard_reason


@pytest.mark.parametrize("volume_spike", [0.0, 50.0, 99.9, 100.0, 124.9, 125.0, 249.9, 250.0, 299.9, 300.0, 1500.0])
def test_boundary_volume_spikes(volume_spike):
    """Verifies StrategyRouter handles all volume spike boundary thresholds smoothly."""
    market_data = {
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BULLISH"},
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
    macro_cache = {"regime_verdict": {"vol_score": 10.0}}

    profile = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="TRENDING", profile="BALANCED"
    )

    assert profile is not None
    if volume_spike >= 300.0:
        assert profile.strategy_mode == "BREAKOUT"
        assert profile.has_directional_signal is True


@pytest.mark.parametrize("spread_pct,expected_approved,expected_veto", [
    (0.05, True, None),     # Clean tight spread
    (0.20, True, None),     # Normal spread
    (0.50, True, None),     # Above penalty threshold (0.4%), notional penalized by 20%, but trade still approved
    (1.20, False, "SPREAD"), # Above veto threshold (1.0%), trade strictly blocked
    (2.50, False, "SPREAD"), # Illiquid book blocked
])
@pytest.mark.asyncio
async def test_boundary_spread_limits(risk_manager, spread_pct, expected_approved, expected_veto):
    """Verifies RiskManager gracefully applies spread penalty or spread veto at boundary thresholds."""
    ceo_decision = {
        "decision": "LONG",
        "symbol": "BTC-USD",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_price": 50000.0,
        "sl_price": 49000.0,
        "tp_price": 52000.0,
        "directional_confidence": 85,
        "entry_quality": 80,
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "balance": 1000.0,
        "available_margin": 1000.0,
        "peak_balance": 1000.0,
        "recent_streak": [1],
        "win_rate_pct": 50.0
    }
    market_data = {
        "symbol": "BTC-USD",
        "price_data": {"current_price": 50000.0},
        "order_book_data": {"spread_pct": spread_pct},
        "indicators": {"atr_14": 500.0},
        "derivatives_data": {
            "size_increment": 0.001,
            "price_increment": 0.1,
            "min_size": 0.001,
            "min_notional": 0.0,
            "maintenance_margin_pct": 0.025
        }
    }

    result = await risk_manager.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING",
        strategy_mode="TREND_FOLLOWING"
    )

    assert result is not None
    if expected_approved:
        assert result.get("approved") is True
        assert result.get("veto_category") is None
    else:
        assert result.get("approved") is not True
        assert result.get("veto_category") in ["SPREAD", "POOR_RISK_REWARD"]


@pytest.mark.parametrize("bad_val", [0.0, -100.0, math.nan, math.inf, -math.inf, None])
@pytest.mark.asyncio
async def test_fuzzing_non_finite_and_negative_inputs(risk_manager, bad_val):
    """Verifies RiskManager fail-closed security when bad numeric inputs (NaN, Inf, negative, None) are passed."""
    ceo_decision = {
        "decision": "LONG",
        "symbol": "ETH-USD",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_price": bad_val if bad_val is not None else 0.0,
        "sl_price": 2000.0,
        "tp_price": 2200.0,
        "directional_confidence": 85,
        "entry_quality": 80,
    }
    portfolio_data = {
        "total_usd": 1000.0,
        "balance": 1000.0,
        "available_margin": 1000.0,
        "peak_balance": 1000.0
    }
    market_data = {
        "symbol": "ETH-USD",
        "price_data": {"current_price": bad_val},
        "indicators": {"atr_14": 20.0},
        "derivatives_data": {
            "size_increment": 0.01,
            "price_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 0.0
        }
    }

    # Must never raise an uncaught exception! Must fail-closed safely.
    result = await risk_manager.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING",
        strategy_mode="TREND_FOLLOWING"
    )

    assert result is not None
    assert result.get("approved") is not True
    assert result.get("veto_category") in ["INVALID_PRICE", "INVALID_INPUT", "ZERO_PRICE", "MARKET_METADATA_UNAVAILABLE"]
