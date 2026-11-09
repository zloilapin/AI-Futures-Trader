import pytest
from unittest.mock import MagicMock
from core.deterministic_guard import DeterministicGuard
from core.strategy_router import StrategyProfile
from agents.ceo_agent import CEOEscalationPatch

class MockRiskManager:
    def __init__(self, min_conv=75):
        self._min_conv = min_conv

    def _get_profile_rules(self, profile, strategy_mode):
        return {"min_conviction": self._min_conv}

@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger

def test_has_macro_trend_name_error_fixed(mock_logger):
    """
    Audit Fix: Ensure that evaluating extreme short/long extension never raises
    NameError for 'has_strong_trend_edge' and uses 'has_macro_trend'.
    """
    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="Downtrend"
    )
    ceo_short = {
        "decision": "SHORT",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 85,
        "trade_action": "ENTER"
    }
    # Extreme short RSI = 22.0 (with FULL_ALIGNMENT macro trend)
    mdata_short = {
        "indicators": {"rsi_14": 22.0, "bb_position_pct": 5.0, "volume_spike_pct": 50.0},
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BEARISH"}
    }
    rm = MockRiskManager(min_conv=75)
    
    # Must execute cleanly without NameError
    res_short = DeterministicGuard.evaluate(profile, ceo_short, "BALANCED", rm, mdata_short, "BTC-USD", logger=mock_logger)
    assert res_short is not None
    assert res_short.symbol == "BTC-USD"

    # Extreme long RSI = 78.0
    profile_long = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        reasoning="Uptrend"
    )
    ceo_long = {
        "decision": "LONG",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 85,
        "trade_action": "ENTER"
    }
    mdata_long = {
        "indicators": {"rsi_14": 78.0, "bb_position_pct": 95.0, "volume_spike_pct": 50.0},
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BULLISH"}
    }
    res_long = DeterministicGuard.evaluate(profile_long, ceo_long, "BALANCED", rm, mdata_long, "BTC-USD", logger=mock_logger)
    assert res_long is not None
    assert res_long.symbol == "BTC-USD"

def test_scale_down_not_vetoed_by_duplicate_extreme_extension(mock_logger):
    """
    Audit Fix: Ensure that when evaluate_trend_exhaustion decides SCALE_DOWN,
    it is not immediately killed by section 5.2 EXTREME_EXTENSION_VETO.
    """
    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="Mature Downtrend"
    )
    ceo = {
        "decision": "SHORT",
        "conviction": 78,
        "directional_confidence": 78,
        "entry_quality": 78,
        "trade_action": "ENTER"
    }
    # RSI 15m = 28.0 (triggers SCALE_DOWN in evaluate_trend_exhaustion because RSI_1h = 32.0 < 36.0)
    # But RSI < 35.0 (is_extended), mtf_alignment = PARTIAL, vol_spike = 40% (not continuation)
    mdata = {
        "indicators": {
            "rsi_14": 28.0,
            "bb_position_pct": 12.0,
            "volume_spike_pct": 40.0,
            "ema_20": 100.0,
            "atr_14": 2.0
        },
        "multi_timeframe": {
            "mtf_alignment": "PARTIAL",
            "trend_1h": "BEARISH",
            "rsi_1h": 32.0
        },
        "price_data": {"current_price": 95.0}
    }
    rm = MockRiskManager(min_conv=75)
    decision = DeterministicGuard.evaluate(profile, ceo, "BALANCED", rm, mdata, "ETH-USD", logger=mock_logger)
    
    # Must NOT be blocked by EXTREME_EXTENSION_VETO! Must be actionable with REDUCE_SIZE
    assert decision.is_actionable is True
    assert decision.trade_action == "REDUCE_SIZE"
    assert decision.decision == "SHORT"
    assert decision.guard_status == "PASSED"

def test_borderline_conviction_demoted_to_reduce_size(mock_logger):
    """
    Audit Fix: When trade_action is ENTER but conviction is below min_conv (e.g. 72% < 75%)
    and >= min_reduce_size_conv (71.0%), it should be clamped to REDUCE_SIZE instead of vetoed with LOW_CONFIDENCE.
    """
    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="Trend"
    )
    ceo = {
        "decision": "SHORT",
        "conviction": 72,
        "directional_confidence": 82,
        "entry_quality": 72,
        "trade_action": "ENTER"
    }
    mdata = {
        "indicators": {"rsi_14": 45.0, "bb_position_pct": 40.0},
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT"}
    }
    rm = MockRiskManager(min_conv=75) # min_conv is 75, conviction is 72
    decision = DeterministicGuard.evaluate(profile, ceo, "BALANCED", rm, mdata, "SOL-USD", logger=mock_logger)

    assert decision.is_actionable is True
    assert decision.trade_action == "REDUCE_SIZE"
    assert decision.decision == "SHORT"
    assert decision.guard_status == "PASSED"

def test_pullback_retest_promotion_respects_min_conv(mock_logger):
    """
    Audit Fix: Pullback retest promotion promotes to ENTER only if EQ >= min_conv,
    otherwise safely promotes to REDUCE_SIZE.
    """
    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="Retest"
    )
    # Case A: EQ = 72 in BALANCED (min_conv = 75) -> promotes to REDUCE_SIZE
    ceo_a = {
        "decision": "SHORT",
        "conviction": 62,
        "directional_confidence": 82,
        "entry_quality": 72,
        "trade_action": "WAIT_FOR_PULLBACK",
        "is_pullback_retest": True
    }
    rm_balanced = MockRiskManager(min_conv=75)
    mdata = {"indicators": {"rsi_14": 50.0, "bb_position_pct": 50.0}}
    dec_a = DeterministicGuard.evaluate(profile, ceo_a, "BALANCED", rm_balanced, mdata, "BTC-USD", logger=mock_logger)
    assert dec_a.is_actionable is True
    assert dec_a.trade_action == "REDUCE_SIZE"
    assert dec_a.conviction >= 75

    # Case B: EQ = 72 in AGGRESSIVE (min_conv = 70) -> promotes to ENTER
    rm_aggressive = MockRiskManager(min_conv=70)
    ceo_b = {
        "decision": "SHORT",
        "conviction": 62,
        "directional_confidence": 82,
        "entry_quality": 72,
        "trade_action": "WAIT_FOR_PULLBACK",
        "is_pullback_retest": True
    }
    dec_b = DeterministicGuard.evaluate(profile, ceo_b, "AGGRESSIVE", rm_aggressive, mdata, "BTC-USD", logger=mock_logger)
    assert dec_b.is_actionable is True
    assert dec_b.trade_action == "ENTER"
    assert dec_b.conviction >= 70

def test_arbiter_math_validation_hold_support():
    """
    Audit Fix: validate_arbiter_math allows valid HOLD responses with penalty
    without false positive rejection.
    """
    # HOLD response with dir_conf=40, risk_pen=-10, entry_quality=0
    valid_hold = {
        "decision": "HOLD",
        "directional_confidence": 40.0,
        "entry_quality": 0.0,
        "risk_penalty": -10.0
    }
    assert CEOEscalationPatch.validate_arbiter_math(valid_hold) is True

    # Directional trade with arithmetic hallucination: Qual 90 != 80 - 10
    invalid_trade = {
        "decision": "LONG",
        "directional_confidence": 80.0,
        "entry_quality": 90.0,
        "risk_penalty": -10.0
    }
    assert CEOEscalationPatch.validate_arbiter_math(invalid_trade) is False

    # Valid directional trade: Qual 70 == 80 - 10
    valid_trade = {
        "decision": "LONG",
        "directional_confidence": 80.0,
        "entry_quality": 70.0,
        "risk_penalty": -10.0
    }
    assert CEOEscalationPatch.validate_arbiter_math(valid_trade) is True

def test_state_sync_entry_qual_and_dir_conf(mock_logger):
    """
    Audit Fix: Ensure that DeterministicGuard synchronizes entry_quality and
    directional_confidence back into ceo_proposal.
    """
    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning=""
    )
    ceo = {
        "decision": "SHORT",
        "conviction": 72,
        "directional_confidence": 85,
        "entry_quality": 65,
        "trade_action": "REDUCE_SIZE"
    }
    mdata = {"indicators": {"rsi_14": 50.0, "bb_position_pct": 50.0}}
    rm = MockRiskManager(min_conv=75)
    DeterministicGuard.evaluate(profile, ceo, "BALANCED", rm, mdata, "SOL-USD", logger=mock_logger)

    assert ceo["entry_quality"] == 65
    assert ceo["directional_confidence"] == 85
    assert ceo["trade_action"] == "REDUCE_SIZE"
    assert ceo["is_actionable"] is True
