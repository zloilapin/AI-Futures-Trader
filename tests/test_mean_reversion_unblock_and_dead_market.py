import pytest
from unittest.mock import MagicMock
from agents.ceo_agent import CEOAgent
from core.strategy_router import StrategyRouter

def test_ceo_agent_unblocks_mean_reversion_long():
    """
    Verifies that when StrategyRouter proposes MEAN_REVERSION LONG from the lower Bollinger band
    in a range-bound market (NEUTRAL_CHOP), CEOAgent no longer collapses directional confidence to 36%
    or forces trade_action to HOLD.
    """
    logger = MagicMock()
    primary_llm = MagicMock()
    escalation_llm = MagicMock()
    ceo = CEOAgent(logger, primary_llm, escalation_llm)

    breakdown = {
        "bull_argument": 42.0,
        "bear_argument": 15.0,
        "mtf_trend": 20.0
    }
    market_context = {
        "strategy_mode": "MEAN_REVERSION",
        "multi_timeframe_context": {
            "tf_15m": {"trend": "NEUTRAL"},
            "tf_1h": {"trend": "NEUTRAL"},
            "tf_4h": {"trend": "NEUTRAL"}
        }
    }

    res = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)

    assert res["decision"] == "LONG"
    assert res["directional_confidence"] >= 70, f"Expected dir_conf >= 70, got {res['directional_confidence']}"
    assert res["trade_action"] in ["ENTER", "REDUCE_SIZE"], f"Expected actionable trade, got {res['trade_action']}"
    assert res["conviction"] >= 70

def test_ceo_agent_unblocks_mean_reversion_short():
    """
    Verifies that when StrategyRouter proposes MEAN_REVERSION SHORT from the upper Bollinger band
    in a range-bound market (NEUTRAL_CHOP), CEOAgent calculates valid directional confidence >= 70%
    and approves the short reversion.
    """
    logger = MagicMock()
    primary_llm = MagicMock()
    escalation_llm = MagicMock()
    ceo = CEOAgent(logger, primary_llm, escalation_llm)

    breakdown = {
        "bull_argument": 14.0,
        "bear_argument": 45.0,
        "mtf_trend": 20.0
    }
    market_context = {
        "strategy_mode": "MEAN_REVERSION",
        "multi_timeframe_context": {
            "tf_15m": {"trend": "NEUTRAL"},
            "tf_1h": {"trend": "NEUTRAL"},
            "tf_4h": {"trend": "NEUTRAL"}
        }
    }

    res = ceo._validate_and_compute_score("SHORT", breakdown, market_context=market_context)

    assert res["decision"] == "SHORT"
    assert res["directional_confidence"] >= 70, f"Expected dir_conf >= 70, got {res['directional_confidence']}"
    assert res["trade_action"] in ["ENTER", "REDUCE_SIZE"], f"Expected actionable trade, got {res['trade_action']}"
    assert res["conviction"] >= 70

def test_strategy_router_blocks_breakout_in_dead_market():
    """
    Verifies that when macro volatility is dead (vol_score < 7.0),
    StrategyRouter strictly blocks BREAKOUT traps at the edge of the range.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "NEUTRAL"
        },
        "indicators": {
            "volume_spike_pct": 210.0,
            "donchian_high": 100.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 101.5,
            "bb_position_pct": 94.0,
            "bb_width_pct": 3.0
        },
        "price_data": {"current_price": 102.0},
        "derivatives_data": {"open_interest_trend": "stable"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]
    # Dead weekend market with volatility score 3.5 (identical to 42-cycle session log)
    macro_cache = {
        "regime_verdict": {
            "regime": "RANGE_CHOPPY",
            "range_score": 54.4,
            "vol_score": 3.5
        }
    }

    profile = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="AGGRESSIVE"
    )

    # BREAKOUT MUST be rejected due to dead market volatility (vol_score=3.5 < 7.0)
    assert profile.has_directional_signal is False
    assert profile.direction_bias == "NEUTRAL"
    assert "Запрет BREAKOUT" in profile.reasoning
    assert "Vol=3.5" in profile.reasoning
